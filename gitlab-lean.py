#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "click>=8.0.0",
#     "python-gitlab>=4.0.0",
#     "rich>=13.0.0",
# ]
# ///
"""
GitLab Lean MR Approval & Merge + Force-Push Management

Subcommands:
  merge      Approve and merge an MR (temporarily disabling author-approval restriction)
  unprotect  Remove stage branch protection to allow force-push
  protect    Restore stage branch protection with safe defaults

Usage:
    ./gitlab-lean.py merge https://gitlab.com/group/project/-/merge_requests/123
    ./gitlab-lean.py unprotect --project-url https://gitlab.com/group/project
    git push -f
    ./gitlab-lean.py protect --project-url https://gitlab.com/group/project
"""

import logging
import os
import re
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import click
from gitlab import Gitlab, GitlabAuthenticationError, GitlabGetError
from rich.console import Console
from rich.logging import RichHandler

# Setup rich console and logging
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)
logger = logging.getLogger(__name__)

# Constants
DEFAULT_GITLAB_URL = "https://gitlab.com"
MR_URL_PATTERN = re.compile(r"https://gitlab\.com/(.+?)/-/merge_requests/(\d+)")
PROJECT_URL_PATTERN = re.compile(r"https://gitlab\.com/(.+?)/?$")
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gitlab-lean"
STATE_PATH = STATE_DIR / "force-push.toml"
FORCE_PUSH_BRANCH = "stage"


def parse_mr_url(url: str) -> tuple[str, int] | None:
    """Parse a GitLab MR URL to extract project path and MR IID."""
    match = MR_URL_PATTERN.match(url)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def parse_project_url(url: str) -> str | None:
    """Parse a GitLab project URL to extract the project path."""
    match = PROJECT_URL_PATTERN.match(url)
    if not match:
        return None
    return match.group(1)


def get_gitlab_token(cli_token: str | None = None) -> str:
    """Get GitLab token from environment variable or CLI argument."""
    env_token = os.getenv("GITLAB_TOKEN")

    if env_token:
        logger.info("Using GitLab token from GITLAB_TOKEN environment variable")
        return env_token

    if cli_token:
        logger.info("Using GitLab token from --token argument")
        return cli_token

    raise click.ClickException(
        "No GitLab token provided. Set GITLAB_TOKEN environment variable or use --token argument"
    )


def connect_gitlab(token: str) -> Gitlab:
    """Authenticate and return a connected Gitlab instance."""
    logger.info(f"Connecting to GitLab at {DEFAULT_GITLAB_URL}")
    gl = Gitlab(DEFAULT_GITLAB_URL, private_token=token)
    gl.auth()
    logger.info("Authentication successful")
    return gl


def read_force_push_config() -> dict | None:
    """Read the force-push state file. Returns None if missing."""
    if not STATE_PATH.exists():
        return None
    content = STATE_PATH.read_text().strip()
    if not content:
        return None
    return tomllib.loads(content)


def write_force_push_config(project_path: str) -> None:
    """Write force-push state to the TOML state file (tracks that protection was removed)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    content = f'project_path = "{project_path}"\nenabled_at = "{now}"\n'
    STATE_PATH.write_text(content)


def clear_force_push_config() -> None:
    """Remove the force-push state file."""
    STATE_PATH.unlink(missing_ok=True)


@click.group()
@click.option(
    "--token",
    type=str,
    default=None,
    help="GitLab authentication token (or set GITLAB_TOKEN env var)",
)
@click.pass_context
def cli(ctx: click.Context, token: str | None) -> None:
    """GitLab Lean - MR approval/merge and force-push management."""
    ctx.ensure_object(dict)
    ctx.obj["token"] = token


@cli.command("merge")
@click.argument("mr_url")
@click.pass_context
def merge_command(ctx: click.Context, mr_url: str) -> None:
    """Approve and merge an MR, temporarily removing branch protection and author-approval restriction.

    MR_URL is the GitLab merge request URL
    (e.g., https://gitlab.com/group/project/-/merge_requests/123).
    """
    parsed = parse_mr_url(mr_url)
    if not parsed:
        raise click.ClickException(
            "Invalid GitLab MR URL format. Expected: https://gitlab.com/{project}/-/merge_requests/{id}"
        )
    project_path, mr_iid = parsed

    try:
        token = get_gitlab_token(ctx.obj["token"])
        gl = connect_gitlab(token)

        # Get project and MR
        project = gl.projects.get(project_path)
        logger.info(f"Project: {project.name_with_namespace}")
        mr = project.mergerequests.get(mr_iid)
        logger.info(f"Merge Request: !{mr_iid} - {mr.title}")

        # Check if target branch is protected
        was_protected = False
        target_branch = mr.target_branch
        try:
            protected_branch = project.protectedbranches.get(target_branch)
            was_protected = True
            logger.info(f"Target branch '{target_branch}' is protected")
        except GitlabGetError:
            logger.info(f"Target branch '{target_branch}' is not protected, skipping unprotect")

        # Save current approval setting
        approvals = project.approvals.get()
        original_setting = approvals.merge_requests_author_approval
        logger.info(f"Current merge_requests_author_approval: {original_setting}")

        try:
            # Remove branch protection if present
            if was_protected:
                console.print(f"[bold yellow]Removing protection from '{target_branch}'...[/bold yellow]")
                protected_branch.delete()
                logger.info(f"Branch '{target_branch}' unprotected")

            # Disable "Prevent approval by MR creator"
            console.print("[bold yellow]Disabling author-approval restriction...[/bold yellow]")
            gl.http_post(
                f"/projects/{project.id}/approvals",
                post_data={"merge_requests_author_approval": True},
            )
            logger.info("Author-approval restriction disabled")

            # Check if MR is draft and mark as ready
            if mr.draft:
                console.print("[bold yellow]MR is draft, marking as ready...[/bold yellow]")
                gl.http_put(
                    f"/projects/{project.id}/merge_requests/{mr_iid}",
                    post_data={"title": mr.title.removeprefix("Draft: ").removeprefix("Draft:")},
                )
                logger.info("MR marked as ready")

            # Approve the MR
            console.print("[bold blue]Approving MR...[/bold blue]")
            try:
                gl.http_post(f"/projects/{project.id}/merge_requests/{mr_iid}/approve")
                logger.info("MR approved")
            except GitlabAuthenticationError:
                logger.info("MR already approved (or author-approval blocked), continuing")

            # Rebase before merge (required for fast-forward merge strategy)
            console.print("[bold yellow]Rebasing MR...[/bold yellow]")
            gl.http_put(f"/projects/{project.id}/merge_requests/{mr_iid}/rebase")
            for _ in range(60):
                time.sleep(2)
                mr_data = gl.http_get(
                    f"/projects/{project.id}/merge_requests/{mr_iid}",
                    query_data={"include_rebase_in_progress": True},
                )
                if not mr_data.get("rebase_in_progress", False):
                    break
            else:
                raise click.ClickException("Rebase timed out after 120s")
            if mr_data.get("merge_error"):
                raise click.ClickException(f"Rebase failed: {mr_data['merge_error']}")
            logger.info("MR rebased")

            # Merge the MR
            console.print("[bold blue]Merging MR...[/bold blue]")
            gl.http_put(
                f"/projects/{project.id}/merge_requests/{mr_iid}/merge",
                post_data={"should_remove_source_branch": True},
            )
            logger.info("MR merged (source branch deleted)")

        finally:
            # Restore branch protection if it was removed
            if was_protected:
                console.print(f"[bold yellow]Restoring protection on '{target_branch}'...[/bold yellow]")
                project.protectedbranches.create(
                    {
                        "name": target_branch,
                        "push_access_level": 0,
                        "merge_access_level": 30,
                        "allow_force_push": False,
                        "code_owner_approval_required": False,
                    }
                )
                logger.info(f"Branch '{target_branch}' re-protected")

            # Re-enable "Prevent approval by MR creator"
            console.print("[bold yellow]Restoring author-approval restriction...[/bold yellow]")
            gl.http_post(
                f"/projects/{project.id}/approvals",
                post_data={"merge_requests_author_approval": original_setting},
            )
            logger.info(f"Restored merge_requests_author_approval to {original_setting}")

        console.print(f"[bold green]Successfully merged !{mr_iid} - {mr.title}[/bold green]")

    except GitlabAuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)
    except GitlabGetError as e:
        logger.error(f"Failed to fetch data from GitLab: {e}")
        sys.exit(1)
    except click.ClickException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


@cli.command("unprotect")
@click.option(
    "--project-url",
    type=str,
    required=True,
    help="GitLab project URL (e.g., https://gitlab.com/group/project)",
)
@click.pass_context
def enable_force_push(ctx: click.Context, project_url: str) -> None:
    """Temporarily allow force-push by removing stage from protected branches."""
    project_path = parse_project_url(project_url)
    if not project_path:
        raise click.ClickException("Invalid GitLab project URL format. Expected: https://gitlab.com/{group}/{project}")

    # Check existing state
    existing = read_force_push_config()
    if existing:
        raise click.ClickException(
            f"Force-push is already enabled for {existing['project_path']} "
            f"(since {existing.get('enabled_at', 'unknown')}). "
            f"Run 'protect' first to restore protection."
        )

    try:
        token = get_gitlab_token(ctx.obj["token"])
        gl = connect_gitlab(token)

        project = gl.projects.get(project_path)
        logger.info(f"Project: {project.name_with_namespace}")

        # Get the protected branch
        try:
            branch = project.protectedbranches.get(FORCE_PUSH_BRANCH)
        except GitlabGetError as err:
            raise click.ClickException(
                f"Branch '{FORCE_PUSH_BRANCH}' is not protected in {project_path}. Already unprotected."
            ) from err

        # Save state BEFORE removing protection (safe on failure)
        write_force_push_config(project_path)
        logger.info(f"Saved state to {STATE_PATH}")

        # Remove branch protection entirely to allow force-push
        console.print(f"[bold yellow]Removing protection from '{FORCE_PUSH_BRANCH}'...[/bold yellow]")
        branch.delete()
        logger.info(f"Branch '{FORCE_PUSH_BRANCH}' unprotected")

        console.print(
            f"[bold green]Protection removed from '{FORCE_PUSH_BRANCH}' in {project_path}[/bold green]\n"
            f"[dim]State saved to {STATE_PATH}. Run 'protect' to restore protection.[/dim]"
        )

    except GitlabAuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)
    except click.ClickException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


@cli.command("protect")
@click.option(
    "--project-url",
    type=str,
    required=True,
    help="GitLab project URL (e.g., https://gitlab.com/group/project)",
)
@click.pass_context
def disable_force_push(ctx: click.Context, project_url: str) -> None:
    """Restore stage branch protection with hardcoded safe defaults."""
    project_path = parse_project_url(project_url)
    if not project_path:
        raise click.ClickException("Invalid GitLab project URL format. Expected: https://gitlab.com/{group}/{project}")

    # Read saved state
    saved = read_force_push_config()
    if not saved:
        raise click.ClickException("No saved force-push state found. Nothing to restore.")

    if saved["project_path"] != project_path:
        raise click.ClickException(
            f"Project mismatch: saved state is for '{saved['project_path']}', but you specified '{project_path}'."
        )

    try:
        token = get_gitlab_token(ctx.obj["token"])
        gl = connect_gitlab(token)

        project = gl.projects.get(project_path)
        logger.info(f"Project: {project.name_with_namespace}")

        # Verify branch is currently unprotected
        try:
            project.protectedbranches.get(FORCE_PUSH_BRANCH)
            raise click.ClickException(
                f"Branch '{FORCE_PUSH_BRANCH}' is already protected in {project_path}. "
                f"Clear state with 'unprotect' first if needed."
            )
        except GitlabGetError:
            pass  # Expected: branch is unprotected

        # Re-protect with hardcoded safe defaults:
        #   Allowed to push and merge: No one (0)
        #   Allowed to merge: Developers + Maintainers (30)
        #   Allowed to force push: disabled
        #   Code owner approval: disabled
        console.print(f"[bold yellow]Restoring protection on '{FORCE_PUSH_BRANCH}'...[/bold yellow]")
        project.protectedbranches.create(
            {
                "name": FORCE_PUSH_BRANCH,
                "push_access_level": 0,
                "merge_access_level": 30,
                "allow_force_push": False,
                "code_owner_approval_required": False,
            }
        )
        logger.info(f"Branch '{FORCE_PUSH_BRANCH}' re-protected")

        # Clear config AFTER successful API call
        clear_force_push_config()
        logger.info(f"Cleared state from {STATE_PATH}")

        console.print(
            f"[bold green]Protection restored on '{FORCE_PUSH_BRANCH}' in {project_path}[/bold green]\n"
            f"[dim]push=none, merge=dev+maintainer, force_push=off, code_owner=off[/dim]"
        )

    except GitlabAuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)
    except click.ClickException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    cli()

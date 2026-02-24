"""devtool protect/unprotect — GitLab branch protection management."""

import logging
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console

from devtool.gitlab import connect_gitlab, detect_project_path, get_gitlab_token, parse_project_url

logger = logging.getLogger(__name__)
console = Console()

# Constants
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "devtool"
STATE_PATH = STATE_DIR / "force-push.toml"
DEFAULT_BRANCH = "stage"


def read_force_push_config() -> dict | None:
    """Read the force-push state file. Returns None if missing."""
    if not STATE_PATH.exists():
        return None
    content = STATE_PATH.read_text().strip()
    if not content:
        return None
    return tomllib.loads(content)


def write_force_push_config(project_path: str, branch: str) -> None:
    """Write force-push state to the TOML state file (tracks that protection was removed)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    content = f'project_path = "{project_path}"\nbranch = "{branch}"\nenabled_at = "{now}"\n'
    STATE_PATH.write_text(content)


def clear_force_push_config() -> None:
    """Remove the force-push state file."""
    STATE_PATH.unlink(missing_ok=True)


@click.command()
@click.option(
    "--project-url",
    type=str,
    default=None,
    help="GitLab project URL (e.g., https://gitlab.com/group/project). Auto-detected from git remote if omitted.",
)
@click.option("--branch", type=str, default=DEFAULT_BRANCH, help=f"Branch to protect (default: {DEFAULT_BRANCH}).")
@click.option("--token", type=str, default=None, help="GitLab token (or set GITLAB_TOKEN env var)")
def protect(project_url: str | None, branch: str, token: str | None) -> None:
    """Restore branch protection with hardcoded safe defaults."""
    from gitlab import GitlabAuthenticationError, GitlabGetError

    if project_url:
        project_path = parse_project_url(project_url)
        if not project_path:
            raise click.ClickException(
                "Invalid GitLab project URL format. Expected: https://gitlab.com/{group}/{project}"
            )
    else:
        project_path = detect_project_path()
        console.print(f"[bold]Auto-detected project:[/bold] {project_path}")

    # Read saved state
    saved = read_force_push_config()
    if not saved:
        raise click.ClickException("No saved force-push state found. Nothing to restore.")

    if saved["project_path"] != project_path:
        raise click.ClickException(
            f"Project mismatch: saved state is for '{saved['project_path']}', but you specified '{project_path}'."
        )

    saved_branch = saved.get("branch", DEFAULT_BRANCH)
    if saved_branch != branch:
        raise click.ClickException(
            f"Branch mismatch: saved state is for '{saved_branch}', but you specified '{branch}'."
        )

    try:
        resolved_token = get_gitlab_token(token)
        gl = connect_gitlab(resolved_token)

        project = gl.projects.get(project_path)
        logger.info(f"Project: {project.name_with_namespace}")

        # Verify branch is currently unprotected
        try:
            project.protectedbranches.get(branch)
            raise click.ClickException(
                f"Branch '{branch}' is already protected in {project_path}. "
                f"Clear state with 'unprotect' first if needed."
            )
        except GitlabGetError:
            pass  # Expected: branch is unprotected

        # Re-protect with hardcoded safe defaults:
        #   Allowed to push and merge: No one (0)
        #   Allowed to merge: Developers + Maintainers (30)
        #   Allowed to force push: disabled
        #   Code owner approval: disabled
        console.print(f"[bold yellow]Restoring protection on '{branch}'...[/bold yellow]")
        project.protectedbranches.create(
            {
                "name": branch,
                "push_access_level": 0,
                "merge_access_level": 30,
                "allow_force_push": False,
                "code_owner_approval_required": False,
            }
        )
        logger.info(f"Branch '{branch}' re-protected")

        # Clear config AFTER successful API call
        clear_force_push_config()
        logger.info(f"Cleared state from {STATE_PATH}")

        console.print(
            f"[bold green]Protection restored on '{branch}' in {project_path}[/bold green]\n"
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


@click.command()
@click.option(
    "--project-url",
    type=str,
    default=None,
    help="GitLab project URL (e.g., https://gitlab.com/group/project). Auto-detected from git remote if omitted.",
)
@click.option("--branch", type=str, default=DEFAULT_BRANCH, help=f"Branch to unprotect (default: {DEFAULT_BRANCH}).")
@click.option("--token", type=str, default=None, help="GitLab token (or set GITLAB_TOKEN env var)")
def unprotect(project_url: str | None, branch: str, token: str | None) -> None:
    """Temporarily allow force-push by removing a branch from protected branches."""
    from gitlab import GitlabAuthenticationError, GitlabGetError

    if project_url:
        project_path = parse_project_url(project_url)
        if not project_path:
            raise click.ClickException(
                "Invalid GitLab project URL format. Expected: https://gitlab.com/{group}/{project}"
            )
    else:
        project_path = detect_project_path()
        console.print(f"[bold]Auto-detected project:[/bold] {project_path}")

    # Check existing state
    existing = read_force_push_config()
    if existing:
        raise click.ClickException(
            f"Force-push is already enabled for {existing['project_path']} "
            f"(since {existing.get('enabled_at', 'unknown')}). "
            f"Run 'protect' first to restore protection."
        )

    try:
        resolved_token = get_gitlab_token(token)
        gl = connect_gitlab(resolved_token)

        project = gl.projects.get(project_path)
        logger.info(f"Project: {project.name_with_namespace}")

        # Get the protected branch
        try:
            protected = project.protectedbranches.get(branch)
        except GitlabGetError as err:
            raise click.ClickException(
                f"Branch '{branch}' is not protected in {project_path}. Already unprotected."
            ) from err

        # Save state BEFORE removing protection (safe on failure)
        write_force_push_config(project_path, branch)
        logger.info(f"Saved state to {STATE_PATH}")

        # Remove branch protection entirely to allow force-push
        console.print(f"[bold yellow]Removing protection from '{branch}'...[/bold yellow]")
        protected.delete()
        logger.info(f"Branch '{branch}' unprotected")

        console.print(
            f"[bold green]Protection removed from '{branch}' in {project_path}[/bold green]\n"
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

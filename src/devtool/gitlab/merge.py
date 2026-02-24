"""devtool merge — GitLab MR approval and merge."""

import logging
import sys
import time

import click
from rich.console import Console

from devtool.gitlab import connect_gitlab, get_gitlab_token, parse_mr_url

logger = logging.getLogger(__name__)
console = Console()


def _detect_mr_url() -> str:
    """Auto-detect the MR URL for the current branch via glab."""
    import git

    from devtool.jira.remote_links import find_mr_url_for_branch

    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        console.print("[red]Error:[/red] Not in a git repository")
        sys.exit(1)

    try:
        branch_name = repo.active_branch.name
    except TypeError:
        console.print("[red]Error:[/red] Not on a branch (detached HEAD state)")
        sys.exit(1)

    mr_url = find_mr_url_for_branch(branch_name, str(repo.working_dir))
    if not mr_url:
        console.print(f"[red]Error:[/red] No open MR found for branch '{branch_name}'")
        sys.exit(1)

    console.print(f"[bold]Auto-detected MR:[/bold] {mr_url}")
    return mr_url


@click.command()
@click.argument("mr_url", required=False, default=None)
@click.option("--token", type=str, default=None, help="GitLab token (or set GITLAB_TOKEN env var)")
@click.option(
    "--force-rebase", is_flag=True, default=False, help="Rebase even when detailed_merge_status says it is not needed"
)
def merge(mr_url: str | None, token: str | None, force_rebase: bool) -> None:
    """Approve and merge a GitLab merge request.

    MR_URL is the full GitLab merge request URL
    (e.g., https://gitlab.com/group/project/-/merge_requests/123).
    If omitted, auto-detects the MR for the current branch.
    """
    from gitlab import GitlabAuthenticationError, GitlabGetError

    if mr_url is None:
        mr_url = _detect_mr_url()

    parsed = parse_mr_url(mr_url)
    if not parsed:
        raise click.ClickException(
            "Invalid GitLab MR URL format. Expected: https://gitlab.com/{project}/-/merge_requests/{id}"
        )
    project_path, mr_iid = parsed

    try:
        resolved_token = get_gitlab_token(token)
        gl = connect_gitlab(resolved_token)

        # Get project and MR
        project = gl.projects.get(project_path)
        logger.info(f"Project: {project.name_with_namespace}")
        mr = project.mergerequests.get(mr_iid)
        logger.info(f"Merge Request: !{mr_iid} - {mr.title}")

        if mr.state != "opened":
            raise click.ClickException(f"MR is '{mr.state}', not open — nothing to do.")

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

            # Determine whether a rebase is needed by checking detailed_merge_status.
            # Poll until the status settles out of transient states.
            console.print("[bold yellow]Checking merge status...[/bold yellow]")
            for attempt in range(30):
                mr_data = gl.http_get(f"/projects/{project.id}/merge_requests/{mr_iid}")
                merge_status = mr_data.get("detailed_merge_status", "unknown")
                if merge_status not in ("checking", "approvals_syncing"):
                    logger.info(f"Merge status: {merge_status}")
                    break
                if attempt > 0 and attempt % 5 == 0:
                    logger.info(f"Still waiting for merge status (currently: {merge_status}, {attempt * 2}s)...")
                time.sleep(2)
            else:
                raise click.ClickException("Merge status check timed out after 60s")

            if merge_status == "conflict":
                raise click.ClickException(
                    f"MR has conflicts (detailed_merge_status={merge_status!r}) - resolve conflicts before merging"
                )

            needs_rebase = merge_status not in ("mergeable", "ci_must_pass", "ci_still_running")
            if needs_rebase or force_rebase:
                if force_rebase and not needs_rebase:
                    logger.info(f"--force-rebase set; rebasing despite status={merge_status!r}")
                elif merge_status != "need_rebase":
                    logger.info(f"Unrecognised merge status {merge_status!r}, rebasing as safe default")

                # Rebase before merge (required for semi-linear history or when forced)
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

                # Re-approve after rebase (rebase resets approvals if "reset on push" is enabled)
                console.print("[bold blue]Re-approving MR after rebase...[/bold blue]")
                try:
                    gl.http_post(f"/projects/{project.id}/merge_requests/{mr_iid}/approve")
                    logger.info("MR re-approved")
                except GitlabAuthenticationError:
                    logger.info("MR already approved, continuing")

                # Restore branch protection BEFORE merge so the pipeline sees a protected branch
                # (CI rules like $CI_COMMIT_REF_PROTECTED depend on protection status at pipeline start)
                console.print(f"[bold yellow]Ensuring protection on '{target_branch}'...[/bold yellow]")
                project.protectedbranches.create(
                    {
                        "name": target_branch,
                        "push_access_level": 0,
                        "merge_access_level": 30,
                        "allow_force_push": False,
                        "code_owner_approval_required": False,
                    }
                )
                logger.info(f"Branch '{target_branch}' protected")
                console.print(
                    f"[bold green]Protection ensured on '{target_branch}' in {project_path}[/bold green]\n"
                    f"[dim]push=none, merge=dev+maintainer, force_push=off, code_owner=off[/dim]"
                )

                # Wait for head pipeline to be created after rebase
                console.print("[bold yellow]Waiting for pipeline after rebase...[/bold yellow]")
                for attempt in range(30):
                    mr_data = gl.http_get(f"/projects/{project.id}/merge_requests/{mr_iid}")
                    head_pipeline = mr_data.get("head_pipeline")
                    if head_pipeline:
                        logger.info(f"Pipeline #{head_pipeline['id']} found (status: {head_pipeline['status']})")
                        break
                    if attempt > 0 and attempt % 5 == 0:
                        logger.info(f"Still waiting for pipeline ({attempt * 2}s)...")
                    time.sleep(2)
                else:
                    raise click.ClickException("No pipeline created after rebase (timed out after 60s)")

                # Wait for MR merge status to be computed after rebase
                console.print("[bold yellow]Waiting for merge status check after rebase...[/bold yellow]")
                for attempt in range(30):
                    mr_data = gl.http_get(f"/projects/{project.id}/merge_requests/{mr_iid}")
                    merge_status = mr_data.get("detailed_merge_status", "unknown")
                    if merge_status not in ("checking", "approvals_syncing"):
                        logger.info(f"Merge status after rebase: {merge_status}")
                        break
                    if attempt > 0 and attempt % 5 == 0:
                        logger.info(f"Still waiting for merge status (currently: {merge_status}, {attempt * 2}s)...")
                    time.sleep(2)
                else:
                    raise click.ClickException("Merge status check timed out after 60s")

            else:
                console.print(f"[bold green]Rebase not needed ({merge_status!r}), skipping[/bold green]")

                # Ensure branch protection BEFORE merge so the pipeline sees a protected branch
                # (CI rules like $CI_COMMIT_REF_PROTECTED depend on protection status at pipeline start)
                console.print(f"[bold yellow]Ensuring protection on '{target_branch}'...[/bold yellow]")
                project.protectedbranches.create(
                    {
                        "name": target_branch,
                        "push_access_level": 0,
                        "merge_access_level": 30,
                        "allow_force_push": False,
                        "code_owner_approval_required": False,
                    }
                )
                logger.info(f"Branch '{target_branch}' protected")
                console.print(
                    f"[bold green]Protection ensured on '{target_branch}' in {project_path}[/bold green]\n"
                    f"[dim]push=none, merge=dev+maintainer, force_push=off, code_owner=off[/dim]"
                )

            # Merge the MR (use merge_when_pipeline_succeeds if immediate merge fails)
            console.print("[bold blue]Merging MR...[/bold blue]")
            try:
                gl.http_put(
                    f"/projects/{project.id}/merge_requests/{mr_iid}/merge",
                    post_data={"should_remove_source_branch": True},
                )
                logger.info("MR merged (source branch deleted)")
            except Exception as merge_err:
                err_str = str(merge_err)
                if "405" in err_str or "422" in err_str:
                    console.print(
                        "[bold yellow]Immediate merge not possible (pipeline pending),"
                        " setting to merge when pipeline succeeds...[/bold yellow]"
                    )
                    # Retry MWPS on transient 422 errors
                    last_err = None
                    for mwps_attempt in range(3):
                        try:
                            gl.http_put(
                                f"/projects/{project.id}/merge_requests/{mr_iid}/merge",
                                post_data={
                                    "should_remove_source_branch": True,
                                    "merge_when_pipeline_succeeds": True,
                                },
                            )
                            logger.info("MR set to merge when pipeline succeeds")
                            last_err = None
                            break
                        except Exception as mwps_err:
                            last_err = mwps_err
                            if "422" in str(mwps_err) and mwps_attempt < 2:
                                logger.info(f"MWPS attempt {mwps_attempt + 1} failed (422), retrying in 3s...")
                                time.sleep(3)
                            else:
                                raise
                    if last_err:
                        raise last_err from None
                else:
                    raise

        finally:
            # Re-enable "Prevent approval by MR creator"
            try:
                console.print("[bold yellow]Restoring author-approval restriction...[/bold yellow]")
                gl.http_post(
                    f"/projects/{project.id}/approvals",
                    post_data={"merge_requests_author_approval": original_setting},
                )
                logger.info(f"Restored merge_requests_author_approval to {original_setting}")
            except Exception as e:
                logger.error(f"Failed to restore author-approval setting: {e}")

            # Ensure branch protection is always present (even if we failed before the merge step)
            try:
                project.protectedbranches.get(target_branch)
            except GitlabGetError:
                console.print(f"[bold yellow]Ensuring protection on '{target_branch}' (cleanup)...[/bold yellow]")
                project.protectedbranches.create(
                    {
                        "name": target_branch,
                        "push_access_level": 0,
                        "merge_access_level": 30,
                        "allow_force_push": False,
                        "code_owner_approval_required": False,
                    }
                )
                logger.info(f"Branch '{target_branch}' protected (cleanup)")
                console.print(
                    f"[bold green]Protection ensured on '{target_branch}' in {project_path}[/bold green]\n"
                    f"[dim]push=none, merge=dev+maintainer, force_push=off, code_owner=off[/dim]"
                )

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

"""Link GitLab MR URLs to Jira issues as remote links."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys

logger = logging.getLogger(__name__)

# Pattern: https://<host>/<project>/-/merge_requests/<iid>
_MR_URL_PATTERN = re.compile(r"(.+)/-/merge_requests/(\d+)$")


def _parse_mr_url(mr_url: str) -> tuple[str, str]:
    """Extract project path and MR IID from a GitLab MR URL.

    Returns:
        Tuple of (project_path, mr_iid).

    Raises:
        ValueError: If URL doesn't match expected pattern.
    """
    match = _MR_URL_PATTERN.search(mr_url.rstrip("/"))
    if not match:
        raise ValueError(f"Not a valid GitLab MR URL: {mr_url}")
    # project_path: strip scheme+host, e.g. "https://gitlab.com/group/project" -> "group/project"
    project_url = match.group(1)
    # Remove scheme and host
    project_path = re.sub(r"^https?://[^/]+/", "", project_url)
    mr_iid = match.group(2)
    return project_path, mr_iid


def _make_global_id(mr_url: str) -> str:
    """Build a stable global_id for Jira remote link idempotency."""
    project_path, mr_iid = _parse_mr_url(mr_url)
    return f"devtool-mr-{project_path}-{mr_iid}"


def find_existing_mr_link(jira_client, issue_key: str, mr_url: str, global_id: str):
    """Check for existing MR remote links on a Jira issue.

    Uses dual detection: global_id lookup AND URL pattern scan.

    Returns:
        None - no existing MR link found
        "same" - exact same URL already linked
        dict - existing link object with a different MR URL
    """
    all_links = jira_client.get_issue_remote_links(issue_key) or []

    for link in all_links:
        link_url = link.get("object", {}).get("url", "")
        link_gid = link.get("globalId", "")

        # Check by global_id match
        if link_gid == global_id:
            if link_url == mr_url:
                return "same"
            return link

        # Check by URL pattern scan for any merge_requests link
        if "/merge_requests/" in link_url:
            if link_url == mr_url:
                return "same"
            return link

    return None


def link_mr_to_jira(issue_key: str, mr_url: str, branch_name: str, console) -> None:
    """Link a GitLab MR to a Jira issue as a remote link.

    Non-fatal: prints yellow warning on any failure and returns.
    """
    try:
        _link_mr_to_jira_impl(issue_key, mr_url, branch_name, console)
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to link MR to {issue_key}: {e}[/yellow]")


def _link_mr_to_jira_impl(issue_key: str, mr_url: str, branch_name: str, console) -> None:
    """Internal implementation - may raise on failure."""
    from devtool.jira.client import connect_jira

    _, mr_iid = _parse_mr_url(mr_url)
    global_id = _make_global_id(mr_url)

    jira_client = connect_jira()

    existing = find_existing_mr_link(jira_client, issue_key, mr_url, global_id)

    if existing == "same":
        logger.debug(f"MR link already exists on {issue_key}, skipping")
        return

    if existing is not None:
        # Different MR URL found - prompt user
        existing_url = existing.get("object", {}).get("url", "<unknown>")
        console.print(f"[yellow]{issue_key} already has an MR link: {existing_url}[/yellow]")

        if not sys.stdin.isatty():
            console.print("[yellow]Non-interactive mode, skipping overwrite[/yellow]")
            return

        try:
            choice = input("Overwrite with new MR link? [y/N]: ").strip().lower()
        except EOFError, KeyboardInterrupt:
            console.print("\nSkipping.")
            return

        if choice not in ("y", "yes"):
            return

    title = f"MR !{mr_iid}: {branch_name}"
    jira_client.create_or_update_issue_remote_links(
        issue_key,
        mr_url,
        title,
        global_id=global_id,
    )
    console.print(f"[green]Linked MR !{mr_iid} to {issue_key}[/green]")


def find_mr_url_for_branch(branch_name: str, cwd: str) -> str | None:
    """Discover the most recent MR URL for a branch via glab.

    Returns the web_url of the most recent MR, or None if not found.
    Silently returns None on any glab failure.
    """
    try:
        result = subprocess.run(
            ["glab", "mr", "list", f"--source-branch={branch_name}", "-F", "json"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if result.returncode != 0:
            return None

        mrs = json.loads(result.stdout)
        if not mrs:
            return None

        # Select most recent by created_at
        best = max(mrs, key=lambda mr: mr.get("created_at", ""))
        return best.get("web_url")
    except Exception:
        return None

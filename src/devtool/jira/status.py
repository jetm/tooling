"""Jira status transitions and pipeline validation."""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

STATUS_MAP: dict[str, str] = {
    "in-progress": "In Progress",
    "peer-review": "Peer Review",
    "review-staging": "Review Staging",
    "resolved": "Resolved",
}


def transition_jira_issue(issue_key: str, status_name: str) -> bool:
    """Transition a Jira issue to the given status.

    Args:
        issue_key: The Jira issue key (e.g., "IOTIL-1234").
        status_name: The target Jira status name (e.g., "In Progress").

    Returns:
        True if the transition succeeded, False otherwise.
    """
    from devtool.jira.client import connect_jira

    jira_client = connect_jira()

    current_status = jira_client.get_issue_status(issue_key)
    if current_status == status_name:
        logger.debug(f"{issue_key} is already in '{status_name}'")
        return True

    try:
        jira_client.set_issue_status(issue_key, status_name)
        logger.debug(f"Transitioned {issue_key} to '{status_name}'")
        return True
    except Exception as e:
        logger.error(f"Failed to transition {issue_key} to '{status_name}': {e}")
        raise


def check_merge_pipeline(branch_name: str) -> tuple[bool, str]:
    """Check the merge result pipeline status for a branch's MR.

    Args:
        branch_name: The git branch name to look up.

    Returns:
        A tuple of (passed, message) where passed is True if the
        merge result pipeline succeeded.

    Raises:
        RuntimeError: If no MR is found or glab fails.
    """
    try:
        result = subprocess.run(
            ["glab", "mr", "view", branch_name, "--json", "pipeline,state"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise RuntimeError("glab CLI not found. Install it from https://gitlab.com/gitlab-org/cli") from e

    if result.returncode != 0:
        raise RuntimeError(f"No merge request found for branch '{branch_name}'")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse glab output: {result.stdout}") from e

    pipeline = data.get("pipeline", {})
    if not pipeline:
        raise RuntimeError(f"No pipeline found for MR on branch '{branch_name}'")

    pipeline_status = pipeline.get("status", "unknown")

    if pipeline_status == "success":
        return (True, "Pipeline passed")
    elif pipeline_status == "failed":
        return (False, "Pipeline failed")
    elif pipeline_status in ("running", "pending", "created"):
        return (False, f"Pipeline is still {pipeline_status}")
    else:
        return (False, f"Pipeline status: {pipeline_status}")

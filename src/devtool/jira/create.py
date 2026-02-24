"""Jira issue creation -- Story under Epic, Sub-task under Story."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Parent type -> child issue type mapping
_CHILD_TYPE_MAP = {
    "Epic": "Story",
    "Story": "Sub-task",
}


def create_child_issue(jira_client, project_key: str, parent_key: str, parent_type: str) -> str:
    """Create a child issue under a parent with placeholder fields.

    Args:
        jira_client: An authenticated Jira client instance.
        project_key: The Jira project key (e.g., "IOTIL").
        parent_key: The parent issue key (e.g., "IOTIL-100").
        parent_type: The parent issue type name ("Epic" or "Story").

    Returns:
        The new issue key (e.g., "IOTIL-201").

    Raises:
        ValueError: If the parent type is not supported.
        Exception: If the Jira API call fails.
    """
    child_type = _CHILD_TYPE_MAP.get(parent_type)
    if child_type is None:
        raise ValueError(f"Cannot create child issue under '{parent_type}' (expected Epic or Story)")

    me = jira_client.myself()
    account_id = me["accountId"]

    fields = {
        "project": {"key": project_key},
        "summary": "TBD",
        "description": "TBD",
        "issuetype": {"name": child_type},
        "parent": {"key": parent_key},
        "assignee": {"accountId": account_id},
    }

    logger.debug(f"Creating {child_type} under {parent_key} in project {project_key}")
    result = jira_client.create_issue(fields=fields)

    new_key = result["key"]
    logger.debug(f"Created {child_type}: {new_key}")
    return new_key

"""Shared Jira client -- connection, credentials, and URL parsing."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

JIRA_BASE_URL = "https://linaro.atlassian.net"

_ISSUE_KEY_RE = re.compile(r"^([A-Z]+-\d+)$")


def load_credentials() -> tuple[str | None, str | None]:
    """Load Jira credentials from environment variables.

    Returns:
        A tuple of (email, token) where either value may be None if the
        corresponding environment variable is not set.
    """
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_TOKEN")
    return (email, token)


def connect_jira():
    """Initialize Jira client with credentials.

    Returns:
        A configured Jira client instance.

    Raises:
        ValueError: If credentials are missing.
        ApiError: If authentication fails.
    """
    from devtool._deps import require

    atlassian = require("atlassian", "jira")
    Jira = atlassian.Jira
    ApiError = atlassian.errors.ApiError

    logger.debug(f"Connecting to Jira at {JIRA_BASE_URL}")

    email, token = load_credentials()
    if not email or not token:
        raise ValueError(
            "Missing required environment variables: JIRA_EMAIL and/or JIRA_TOKEN\n"
            "Set them with:\n"
            '  export JIRA_EMAIL="your-email@example.com"\n'
            '  export JIRA_TOKEN="your-api-token"'
        )

    try:
        jira = Jira(url=JIRA_BASE_URL, username=email, password=token, cloud=True)
        logger.debug("Successfully connected to Jira")
        return jira
    except ApiError as e:
        raise ApiError(
            f"Jira authentication failed: {e}\n"
            "Verify your credentials at: https://id.atlassian.com/manage-profile/security/api-tokens"
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Failed to connect to Jira at {JIRA_BASE_URL}: {e}\n"
            "Check your network connectivity to linaro.atlassian.net"
        ) from e


def parse_jira_url(url: str) -> tuple[str, str]:
    """Extract project key and issue key from a Jira browse URL.

    Args:
        url: A Jira browse URL like https://linaro.atlassian.net/browse/IOTIL-100

    Returns:
        A tuple of (project_key, issue_key), e.g. ("IOTIL", "IOTIL-100").

    Raises:
        ValueError: If the URL is not a valid Jira browse URL.
    """
    parsed = urlparse(url)

    expected_host = urlparse(JIRA_BASE_URL).hostname
    if parsed.hostname != expected_host:
        raise ValueError(f"Not a valid Jira URL: expected host '{expected_host}', got '{parsed.hostname}'")

    path = parsed.path.rstrip("/")
    if not path.startswith("/browse/"):
        raise ValueError(f"Not a Jira browse URL: {url}")

    issue_key = path.removeprefix("/browse/")
    if not issue_key:
        raise ValueError(f"No issue key found in URL: {url}")

    issue_key = issue_key.upper()
    if not _ISSUE_KEY_RE.match(issue_key):
        raise ValueError(f"Invalid issue key format: '{issue_key}' (expected PROJECT-NUMBER)")

    project_key = issue_key.split("-")[0]
    return (project_key, issue_key)

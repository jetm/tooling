#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "atlassian-python-api",
#     "python-dateutil",
#     "click",
#     "rich",
#     "requests",
# ]
# ///
"""weekly_status.py - Jira Weekly Status Automation Script.

Queries Jira for weekly task data and creates a static Confluence page with
the snapshot. Uses the atlassian-python-api library for both Jira and Confluence
operations with cloud=True for optimized Atlassian Cloud API access.

Usage Examples
--------------
Normal mode - creates weekly status page:
    $ ./weekly_status.py                 # Current week
    $ ./weekly_status.py --week 4        # Week 4 of current year
    $ ./weekly_status.py -w 52           # Week 52 of current year
    $ ./weekly_status.py -v              # With verbose logging

Diagnose mode - validates configuration and permissions:
    $ ./weekly_status.py diagnose
    $ ./weekly_status.py -v diagnose     # With verbose logging

Dry-run mode - shows what would be created without creating:
    $ ./weekly_status.py dry-run
    $ ./weekly_status.py --week 3 dry-run  # Preview week 3
    $ ./weekly_status.py -v dry-run      # With verbose logging

Week Numbers
------------
Uses ISO week calendar where:
- Week 1 is the week containing the first Thursday of the year
- Weeks run Monday to Sunday
- Week numbers range from 1 to 52 (or 53 in some years)
- Week 1 may start in the previous calendar year

Features
--------
- Automatic current week date calculation (Monday-Friday)
- Optional week number selection via --week/-w option
- Duplicate page detection
- Three Jira query types (completed, created, open items)
- HTML table generation with clickable Jira links
- Confluence page creation under specified parent

Configuration
-------------
Environment variables:
    JIRA_EMAIL: Your Jira account email address
    JIRA_TOKEN: Your Jira API token (generate at id.atlassian.com)
    APPFOX_API_KEY: (Optional) API key for AppFox Compliance classification

Setting Up Environment Variables:
    1. Generate API token at: https://id.atlassian.com/manage-profile/security/api-tokens
    2. Set environment variables:
        export JIRA_EMAIL="your-email@linaro.org"
        export JIRA_TOKEN="your-api-token"
    3. (Optional) For automatic page classification, create an AppFox API key:
       - Go to Confluence: Apps → Compliance → Administration → API Keys
       - Create key with scope: classification:read, classification:write
       - export APPFOX_API_KEY="your-appfox-api-key"
    4. For persistence, add to ~/.bashrc or ~/.zshrc
    5. Verify with: ./weekly_status.py diagnose

    Security: Never commit API tokens to version control. Use environment
    variables or a secrets manager to store credentials securely.

Jira base URL: https://linaro.atlassian.net
Confluence base URL: https://linaro.atlassian.net/wiki
Projects: IOTIL, IS
Done statuses: Closed, Resolved, Ready For Release

Requirements
------------
- Python 3.14+
- uv installed (https://github.com/astral-sh/uv)
- Network access to linaro.atlassian.net
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import quote

import click
from atlassian import Confluence, Jira
from atlassian.errors import ApiError
from rich.console import Console

logger = logging.getLogger(__name__)

# =============================================================================
# Jira/Confluence Configuration
# =============================================================================
JIRA_BASE_URL = "https://linaro.atlassian.net"
CONFLUENCE_BASE_URL = "https://linaro.atlassian.net/wiki"
CONFLUENCE_SPACE_KEY = "~631a07203e578bb3b500554a"
CONFLUENCE_PARENT_PAGE_ID = "30666293285"
ATLASSIAN_CLOUD_ID = "f413e5f8-3a52-4d3e-9228-50ba90fdd427"

# AppFox Compliance API Configuration
# https://docs.appfox.io/confluence-compliance/rest-api
APPFOX_API_URL = "https://ac-cloud.com/compliance/api/v1"

# Jira Query Configuration
JIRA_PROJECTS = ["IOTIL", "IS"]
JIRA_DONE_STATUSES = ["Closed", "Resolved", "Ready For Release"]


# =============================================================================
# Credential Loading
# =============================================================================
def load_credentials() -> tuple[str | None, str | None]:
    """Load Jira credentials from environment variables.

    Reads JIRA_EMAIL and JIRA_TOKEN from the environment. Does not raise
    exceptions if credentials are missing; validation should be performed
    by the caller.

    Returns:
        A tuple of (email, token) where either value may be None if the
        corresponding environment variable is not set.

    Example:
        >>> email, token = load_credentials()
        >>> if not email or not token:
        ...     print("Missing credentials")
    """
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_TOKEN")
    return (email, token)


def load_appfox_api_key() -> str | None:
    """Load AppFox Compliance API key from environment variable.

    Reads APPFOX_API_KEY from the environment. This key is optional and
    used for automatic page classification via the AppFox Compliance app.

    Returns:
        The API key string, or None if not set.

    Example:
        >>> api_key = load_appfox_api_key()
        >>> if api_key:
        ...     print("AppFox classification enabled")
    """
    return os.environ.get("APPFOX_API_KEY")


# =============================================================================
# Date Calculation Utilities
# =============================================================================
def get_previous_week_range() -> tuple[datetime, datetime]:
    """Calculate the previous week's Monday-Friday date range.

    .. deprecated::
        Use ``get_week_range(get_current_week_num() - 1)`` instead for more
        flexibility with week selection.

    Determines the Monday and Friday of the week immediately preceding the
    current week, regardless of what day it currently is.

    Returns:
        A tuple of (start_date, end_date) where:
        - start_date: Previous Monday at 00:00:00
        - end_date: Previous Friday at 23:59:59

    Example:
        If today is 2025-01-25 (Saturday), returns:
        (datetime(2025, 1, 20, 0, 0, 0), datetime(2025, 1, 24, 23, 59, 59))

        If today is 2025-01-22 (Wednesday), returns:
        (datetime(2025, 1, 13, 0, 0, 0), datetime(2025, 1, 17, 23, 59, 59))
    """
    today = datetime.now()

    # Find Monday of current week (weekday() returns 0 for Monday)
    days_since_monday = today.weekday()
    current_monday = today - timedelta(days=days_since_monday)

    # Previous Monday is 7 days before current Monday
    previous_monday = current_monday - timedelta(days=7)

    # Previous Friday is 4 days after previous Monday
    previous_friday = previous_monday + timedelta(days=4)

    # Set times to start and end of day
    start_date = previous_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = previous_friday.replace(hour=23, minute=59, second=59, microsecond=0)

    return (start_date, end_date)


def get_current_week_num() -> int:
    """Get the current ISO week number.

    The ISO week calendar assigns week numbers where:
    - Week 1 is the week containing the first Thursday of the year
    - Weeks run Monday to Sunday
    - Week numbers range from 1 to 52 (or 53 in some years)

    Returns:
        The current ISO week number (1-53).

    Example:
        If today is 2025-01-25, returns 4 (week 4 of 2025).
    """
    return datetime.now().isocalendar().week


def get_week_range(week_num: int, year: int | None = None) -> tuple[datetime, datetime]:
    """Calculate the Monday-Friday date range for a given ISO week number.

    Uses the ISO week calendar to determine the dates. Week 1 is the week
    containing the first Thursday of the year.

    Args:
        week_num: The ISO week number (1-53).
        year: The year (defaults to current year if not specified).

    Returns:
        A tuple of (start_date, end_date) where:
        - start_date: Monday of the specified week at 00:00:00
        - end_date: Friday of the specified week at 23:59:59

    Examples:
        Current week (if week 4, 2025):
            get_week_range(4) -> (2025-01-20 00:00:00, 2025-01-24 23:59:59)

        Past week:
            get_week_range(1, 2025) -> (2024-12-30 00:00:00, 2025-01-03 23:59:59)

        Cross-year (week 1 of 2025 starts in 2024):
            get_week_range(1, 2025) -> Monday Dec 30, 2024 to Friday Jan 3, 2025

    Note:
        Week 1 may start in the previous year (as in the 2025 example above).
        Week 52/53 may extend into the next year.
    """
    if year is None:
        year = datetime.now().year

    # ISO week: use %G (ISO year) and %V (ISO week) with %u (weekday, 1=Monday)
    # This correctly handles week 1 which may start in the previous calendar year
    monday = datetime.strptime(f"{year}-W{week_num:02d}-1", "%G-W%V-%u")
    friday = monday + timedelta(days=4)

    # Set times to start and end of day
    start_date = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = friday.replace(hour=23, minute=59, second=59, microsecond=0)

    return (start_date, end_date)


def format_week_title(start_date: datetime, end_date: datetime) -> str:
    """Format the date range into a Confluence page title.

    Creates a human-readable title for the weekly status page. Handles
    month boundaries appropriately.

    Args:
        start_date: The Monday of the week.
        end_date: The Friday of the week.

    Returns:
        A formatted string suitable for a page title.

    Examples:
        Same month:
            January 20, 2025 and January 24, 2025 -> "January 20-24, 2025"

        Different months:
            December 30, 2024 and January 3, 2025 -> "December 30 - January 3, 2025"

        Different years:
            December 30, 2024 and January 3, 2025 -> "December 30, 2024 - January 3, 2025"
    """
    if start_date.year != end_date.year:
        # Different years: show full date for both
        return f"{start_date.strftime('%B %d, %Y')} - {end_date.strftime('%B %d, %Y')}"
    elif start_date.month != end_date.month:
        # Same year, different months
        return f"{start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}"
    else:
        # Same month and year
        return f"{start_date.strftime('%B')} {start_date.day}-{end_date.day}, {end_date.year}"


# =============================================================================
# Jira Helper Functions
# =============================================================================
def _extract_issue_data(issue: dict) -> dict:
    """Extract standardized data from a Jira issue dictionary.

    Args:
        issue: Jira issue dictionary from enhanced_jql results.

    Returns:
        Dictionary with Type, Key, Summary, and Status fields.

    Raises:
        KeyError: If required fields are missing.
    """
    fields = issue.get("fields", {})
    return {
        "Type": fields.get("issuetype", {}).get("name", "Unknown"),
        "Key": issue.get("key", ""),
        "Summary": fields.get("summary", ""),
        "Status": fields.get("status", {}).get("name", "Unknown"),
    }


# =============================================================================
# Jira Integration Functions
# =============================================================================
def connect_jira() -> Jira:
    """Initialize Jira client with credentials.

    Creates an authenticated connection to the Jira Cloud instance using
    credentials from environment variables. Uses the atlassian-python-api
    library with cloud=True for optimized cloud API access.

    Returns:
        A configured Jira client instance.

    Raises:
        ValueError: If credentials are missing.
        ApiError: If authentication fails.
    """
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


def connect_confluence() -> Confluence:
    """Initialize Confluence client with credentials.

    Creates an authenticated connection to the Confluence Cloud instance using
    credentials from environment variables. Uses the atlassian-python-api
    library with cloud=True for optimized cloud API access.

    Returns:
        A configured Confluence client instance.

    Raises:
        ValueError: If credentials are missing.
        ApiError: If authentication fails.
    """
    logger.debug(f"Connecting to Confluence at {CONFLUENCE_BASE_URL}")

    email, token = load_credentials()
    if not email or not token:
        raise ValueError(
            "Missing required environment variables: JIRA_EMAIL and/or JIRA_TOKEN\n"
            "Set them with:\n"
            '  export JIRA_EMAIL="your-email@example.com"\n'
            '  export JIRA_TOKEN="your-api-token"'
        )

    try:
        confluence = Confluence(url=CONFLUENCE_BASE_URL, username=email, password=token, cloud=True)
        logger.debug("Successfully connected to Confluence")
        return confluence
    except ApiError as e:
        raise ApiError(
            f"Confluence authentication failed: {e}\n"
            "Verify your credentials at: https://id.atlassian.com/manage-profile/security/api-tokens"
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"Failed to connect to Confluence at {CONFLUENCE_BASE_URL}: {e}\n"
            "Check your network connectivity to linaro.atlassian.net"
        ) from e


def get_completed_items(jira: Jira, start_date: datetime, end_date: datetime) -> list[dict]:
    """Query Jira for completed items within the date range.

    Retrieves issues that were resolved/closed during the specified period.
    Uses enhanced_jql for cloud-optimized queries with automatic pagination.

    Args:
        jira: Authenticated Jira client.
        start_date: Start of the date range (inclusive).
        end_date: End of the date range (inclusive).

    Returns:
        A list of issue dictionaries containing Type, Key, Summary, and Status.
    """
    start_str = start_date.strftime("%Y-%m-%d %H:%M")
    end_str = end_date.strftime("%Y-%m-%d %H:%M")
    done_statuses = ", ".join(f'"{s}"' for s in JIRA_DONE_STATUSES)
    projects = ", ".join(f'"{p}"' for p in JIRA_PROJECTS)

    jql_query = (
        f"assignee = currentUser() AND project IN ({projects}) "
        f"AND status changed to ({done_statuses}) "
        f'DURING ("{start_str}", "{end_str}")'
    )
    logger.debug(f"Executing JQL: {jql_query}")

    try:
        response = jira.enhanced_jql(jql_query, fields=["key", "summary", "status", "issuetype"], limit=1000)
        issues = response.get("issues", [])
        logger.debug(f"Found {len(issues)} completed items")

        results = []
        for issue in issues:
            try:
                results.append(_extract_issue_data(issue))
            except KeyError as e:
                logger.warning(f"Skipping malformed issue {issue.get('key', 'unknown')}: {e}")
        return results
    except ApiError as e:
        logger.error(f"Jira query failed: {e}\nJQL: {jql_query}")
        return []
    except Exception as e:
        logger.error(f"Error during Jira query: {e}")
        return []


def get_created_items(jira: Jira, start_date: datetime, end_date: datetime) -> list[dict]:
    """Query Jira for items created within the date range.

    Retrieves issues that were created during the specified period.
    Uses enhanced_jql for cloud-optimized queries with automatic pagination.

    Args:
        jira: Authenticated Jira client.
        start_date: Start of the date range (inclusive).
        end_date: End of the date range (inclusive).

    Returns:
        A list of issue dictionaries containing Type, Key, Summary, and Status.
    """
    start_str = start_date.strftime("%Y-%m-%d %H:%M")
    end_str = end_date.strftime("%Y-%m-%d %H:%M")
    projects = ", ".join(f'"{p}"' for p in JIRA_PROJECTS)

    jql_query = (
        f'assignee = currentUser() AND project IN ({projects}) AND created >= "{start_str}" AND created <= "{end_str}"'
    )
    logger.debug(f"Executing JQL: {jql_query}")

    try:
        response = jira.enhanced_jql(jql_query, fields=["key", "summary", "status", "issuetype"], limit=1000)
        issues = response.get("issues", [])
        logger.debug(f"Found {len(issues)} created items")

        results = []
        for issue in issues:
            try:
                results.append(_extract_issue_data(issue))
            except KeyError as e:
                logger.warning(f"Skipping malformed issue {issue.get('key', 'unknown')}: {e}")
        return results
    except ApiError as e:
        logger.error(f"Jira query failed: {e}\nJQL: {jql_query}")
        return []
    except Exception as e:
        logger.error(f"Error during Jira query: {e}")
        return []


def get_open_items(jira: Jira, end_date: datetime) -> list[dict]:
    """Query Jira for open items as of the end date.

    Retrieves issues that are still open (not in done statuses) as of
    the specified date. Uses enhanced_jql for cloud-optimized queries
    with automatic pagination.

    Args:
        jira: Authenticated Jira client.
        end_date: The date to check open status against.

    Returns:
        A list of issue dictionaries containing Type, Key, Summary, and Status.
    """
    end_str = end_date.strftime("%Y-%m-%d %H:%M")
    done_statuses = ", ".join(f'"{s}"' for s in JIRA_DONE_STATUSES)
    projects = ", ".join(f'"{p}"' for p in JIRA_PROJECTS)

    jql_query = (
        f"assignee = currentUser() AND project IN ({projects}) "
        f"AND status NOT IN ({done_statuses}) "
        f'AND created <= "{end_str}"'
    )
    logger.debug(f"Executing JQL: {jql_query}")

    try:
        response = jira.enhanced_jql(jql_query, fields=["key", "summary", "status", "issuetype"], limit=1000)
        issues = response.get("issues", [])
        logger.debug(f"Found {len(issues)} open items")

        results = []
        for issue in issues:
            try:
                results.append(_extract_issue_data(issue))
            except KeyError as e:
                logger.warning(f"Skipping malformed issue {issue.get('key', 'unknown')}: {e}")
        return results
    except ApiError as e:
        logger.error(f"Jira query failed: {e}\nJQL: {jql_query}")
        return []
    except Exception as e:
        logger.error(f"Error during Jira query: {e}")
        return []


def check_page_exists(title: str) -> bool:
    """Check if a Confluence page with the given title exists under the parent.

    Searches for an existing page under the configured parent page
    with the specified title using CQL to scope the search directly
    to the parent's descendants, avoiding false negatives when pages
    with the same title exist elsewhere in the space.

    Args:
        title: The page title to search for.

    Returns:
        True if a page with this title exists under the parent, False otherwise.
        Returns False on any error (network, auth, parsing) to allow
        the calling code to attempt page creation.

    Example:
        >>> exists = check_page_exists("January 20-24, 2025")
        >>> if exists:
        ...     print("Page already exists, skipping creation")
    """
    logger.debug(f"Checking for existing page with title: {title}")

    try:
        confluence = connect_confluence()

        # Use CQL to search for pages with exact title under the parent page
        # This scopes the search directly to descendants of the parent,
        # avoiding false negatives when titles exist elsewhere in the space
        escaped_title = title.replace('"', '\\"')
        cql = f'ancestor = {CONFLUENCE_PARENT_PAGE_ID} AND title = "{escaped_title}"'
        logger.debug(f"Executing CQL: {cql}")

        results = confluence.cql(cql, limit=1)
        page_count = results.get("size", 0)

        exists = page_count > 0
        logger.debug(f"Page exists check result: {exists} (found {page_count} pages)")
        return exists

    except ValueError as e:
        logger.error(f"Credential error: {e}")
        return False
    except ApiError as e:
        logger.error(f"Confluence API error checking page existence: {e}")
        return False
    except Exception as e:
        logger.error(f"Error checking page existence: {e}")
        return False


def get_public_classification_id() -> str | None:
    """Get the classification level ID for 'Public' classification.

    Queries the AppFox Compliance API for available classification levels
    and returns the UUID for the 'Public' level.

    Returns:
        The classification level UUID string, or None if not found or API unavailable.
    """
    import requests

    api_key = load_appfox_api_key()
    if not api_key:
        logger.debug("No APPFOX_API_KEY set, skipping classification")
        return None

    try:
        response = requests.get(
            f"{APPFOX_API_URL}/level",
            headers={"x-api-key": api_key},
            params={"status": "published"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # AppFox API returns list directly for /level endpoint
        levels = data if isinstance(data, list) else data.get("data", [])
        logger.debug(f"AppFox classification levels: {[lvl.get('name') for lvl in levels]}")

        for level in levels:
            if level.get("name", "").lower() == "public":
                level_id = level.get("id")
                logger.debug(f"Found 'Public' classification level with ID: {level_id}")
                return level_id

        logger.warning("No 'Public' classification level found in AppFox")
        return None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.warning("AppFox API authentication failed - check APPFOX_API_KEY")
        elif e.response.status_code == 403:
            logger.warning("AppFox API access denied - check API key scopes (needs classification:read)")
        else:
            logger.warning(f"AppFox API error: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to get classification levels from AppFox: {e}")
        return None


def set_page_classification(page_id: str, classification_id: str) -> bool:
    """Set the classification level on a Confluence page.

    Uses the AppFox Compliance API to apply a classification level to a page.

    Args:
        page_id: The ID of the page to classify.
        classification_id: The classification level UUID to apply.

    Returns:
        True if classification was set successfully, False otherwise.
    """
    import requests

    api_key = load_appfox_api_key()
    if not api_key:
        logger.warning("No APPFOX_API_KEY set, cannot set classification")
        return False

    try:
        response = requests.post(
            f"{APPFOX_API_URL}/page-level",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={"pageId": page_id, "levelId": classification_id},
            timeout=30,
        )
        response.raise_for_status()
        logger.debug(f"Set classification on page {page_id} via AppFox API")
        return True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.warning("AppFox API authentication failed - check APPFOX_API_KEY")
        elif e.response.status_code == 403:
            logger.warning("AppFox API access denied - check API key scopes (needs classification:write)")
        else:
            logger.warning(f"AppFox API error setting classification: {e}")
        return False
    except Exception as e:
        logger.warning(f"Failed to set page classification via AppFox: {e}")
        return False


def check_classification_api_enabled() -> tuple[bool, list[str], str | None]:
    """Check if the AppFox Compliance API is configured and accessible.

    Verifies APPFOX_API_KEY is set and queries the API for available
    classification levels.

    Returns:
        A tuple of (enabled, level_names, error_message):
        - enabled: True if API is configured and returns classification levels
        - level_names: List of available classification level names
        - error_message: Error description if API unavailable, None otherwise
    """
    import requests

    api_key = load_appfox_api_key()
    if not api_key:
        return (False, [], "APPFOX_API_KEY environment variable not set")

    try:
        response = requests.get(
            f"{APPFOX_API_URL}/level",
            headers={"x-api-key": api_key},
            params={"status": "published"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # AppFox API returns list directly for /level endpoint
        levels = data if isinstance(data, list) else data.get("data", [])
        level_names = [lvl.get("name", "unknown") for lvl in levels]

        if levels:
            return (True, level_names, None)
        else:
            return (False, [], "No classification levels configured (API returned empty list)")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            return (False, [], "Authentication failed - check APPFOX_API_KEY")
        elif e.response.status_code == 403:
            return (False, [], f"Access denied - check API key scopes: {e.response.text}")
        else:
            return (False, [], str(e))
    except Exception as e:
        return (False, [], str(e))


class PageCreationResult:
    """Result of creating a Confluence page."""

    def __init__(self, url: str, classification_status: str, classification_message: str | None = None):
        """Initialize page creation result.

        Args:
            url: The URL of the created page.
            classification_status: One of 'success', 'skipped', 'failed'.
            classification_message: Optional message describing classification outcome.
        """
        self.url = url
        self.classification_status = classification_status
        self.classification_message = classification_message


def create_child_page(title: str, content: str) -> PageCreationResult:
    """Create a Confluence child page under the configured parent.

    Creates a new page with the specified title and HTML content
    as a child of the configured parent page using the atlassian-python-api
    library.

    Pages are created with the v2 editor format to ensure compatibility
    with current Confluence standards and avoid legacy editor deprecation
    warnings. Pages are automatically marked as 'public' through both:
    - Adding the 'public' label for categorization
    - Setting the 'Public' classification level via AppFox Compliance API

    The content should be in Confluence storage format (XHTML-like).
    Common HTML tags are supported, but some may need to be converted
    to Confluence macros for proper rendering.

    Args:
        title: The title for the new page.
        content: The HTML content for the page body in storage format.

    Returns:
        PageCreationResult with URL and classification status ('success', 'skipped', or 'failed').

    Raises:
        ValueError: If credentials are missing or page already exists.
        ApiError: If the API returns an error (auth failed, permission denied,
            parent not found, conflict/duplicate).
        RuntimeError: For connection or other errors.

    Example:
        >>> html_content = "<h2>Notes</h2><p>Weekly summary</p>"
        >>> page_url = create_child_page("January 20-24, 2025", html_content)
        >>> print(f"Created page at: {page_url}")
    """
    # Check if page already exists
    if check_page_exists(title):
        raise ValueError(f"Page with title '{title}' already exists under parent {CONFLUENCE_PARENT_PAGE_ID}")

    logger.debug(f"Creating Confluence page with title: {title}")
    logger.debug(f"Space: {CONFLUENCE_SPACE_KEY}, parent: {CONFLUENCE_PARENT_PAGE_ID}")

    try:
        confluence = connect_confluence()

        # Create the page using atlassian-python-api
        result = confluence.create_page(
            space=CONFLUENCE_SPACE_KEY,
            title=title,
            body=content,
            parent_id=CONFLUENCE_PARENT_PAGE_ID,
            representation="storage",
            editor="v2",
            full_width=True,
        )

        # Extract page ID for label addition
        page_id = result.get("id")

        # Add 'public' label to classify the page
        try:
            confluence.set_page_label(page_id, "public")
            logger.debug(f"Added 'public' label to page {page_id}")
        except Exception as e:
            logger.warning(f"Failed to add 'public' label to page {page_id}: {e}")

        # Set classification level (AppFox Compliance API)
        classification_status = "skipped"
        classification_message = None
        try:
            classification_id = get_public_classification_id()
            if classification_id:
                if set_page_classification(page_id, classification_id):
                    logger.debug(f"Set 'Public' classification on page {page_id}")
                    classification_status = "success"
                    classification_message = "Page classified as 'Public'"
                else:
                    logger.warning(f"Could not set classification on page {page_id}")
                    classification_status = "failed"
                    classification_message = "Failed to set classification level"
            else:
                logger.warning("Skipping classification - no 'Public' level available")
                classification_status = "skipped"
                classification_message = "Classification not configured (set APPFOX_API_KEY)"
        except Exception as e:
            logger.warning(f"Failed to classify page {page_id}: {e}")
            classification_status = "failed"
            classification_message = f"Classification error: {e}"

        # Extract page URL from result
        webui_path = result.get("_links", {}).get("webui")
        if webui_path:
            page_url = f"{CONFLUENCE_BASE_URL}{webui_path}"
        else:
            # Fallback: construct URL from page ID (already extracted above)
            page_url = f"{CONFLUENCE_BASE_URL}/pages/{page_id}"

        logger.debug(f"Successfully created page: {page_url}")
        return PageCreationResult(page_url, classification_status, classification_message)

    except ApiError as e:
        error_msg = str(e)
        if "401" in error_msg:
            raise ApiError("Confluence authentication failed. Check JIRA_EMAIL and JIRA_TOKEN") from e
        elif "403" in error_msg:
            raise ApiError(
                f"Permission denied. Verify access to space {CONFLUENCE_SPACE_KEY} "
                f"and parent page {CONFLUENCE_PARENT_PAGE_ID}"
            ) from e
        elif "404" in error_msg:
            raise ApiError(f"Resource not found. Verify parent page ID {CONFLUENCE_PARENT_PAGE_ID} exists") from e
        elif "409" in error_msg:
            raise ApiError("Conflict - page with this title may already exist") from e
        else:
            raise ApiError(f"Confluence API error during page creation: {e}") from e
    except ValueError as e:
        raise ValueError(f"Error parsing Confluence API response: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Error creating Confluence page: {e}") from e


def build_table(items: list[dict], jira_base_url: str) -> str:
    """Generate a Confluence Smart Link from a list of Jira issues.

    Creates a Jira Smart Link with datasource that renders as a native
    table in Confluence, using a JQL query to display the specified issues.

    Args:
        items: List of issue dictionaries with keys 'Type', 'Key', 'Summary', 'Status'.
        jira_base_url: Base URL for constructing the JQL search URL.

    Returns:
        A Confluence storage format string containing the Smart Link,
        or a "No items" message if the items list is empty.
    """
    if not items:
        return "<p><em>No items</em></p>"

    # Build JQL query with all issue keys
    keys = [item["Key"] for item in items]
    jql_query = f"key IN ({', '.join(keys)})"

    # Build datasource configuration
    datasource = {
        "id": "d8b75300-dfda-4519-b6cd-e49abbd50401",
        "parameters": {
            "cloudId": ATLASSIAN_CLOUD_ID,
            "jql": jql_query,
        },
        "views": [
            {
                "type": "table",
                "properties": {
                    "columns": [
                        {"key": "issuetype"},
                        {"key": "key"},
                        {"key": "summary"},
                        {"key": "status"},
                    ]
                },
            }
        ],
    }

    # Build Smart Link URL and encode datasource
    encoded_jql = quote(jql_query, safe="")
    url = f"{jira_base_url}/issues/?jql={encoded_jql}"
    datasource_json = json.dumps(datasource).replace('"', "&quot;")

    return (
        f'<a href="{url}" data-layout="center" '
        f'data-card-appearance="block" data-datasource="{datasource_json}">'
        f"{url}</a>"
    )


def build_page_content(completed: list[dict], created: list[dict], open_items: list[dict]) -> str:
    """Assemble the full HTML content for the Confluence page.

    Combines the three sections (completed, created, open) into a
    complete page body with headers and tables. Uses Confluence storage
    format (XHTML-like structure with standard HTML elements).

    Args:
        completed: List of completed issues (dictionaries with Type, Key, Summary, Status).
        created: List of newly created issues.
        open_items: List of currently open issues.

    Returns:
        Complete HTML content string for the page body, ready for
        Confluence storage format.
    """
    sections = [
        (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="borderColor">#FFAB00</ac:parameter>'
            '<ac:parameter ac:name="bgColor">#FFFFFF</ac:parameter>'
            '<ac:parameter ac:name="titleBGColor">#FFF7D6</ac:parameter>'
            '<ac:parameter ac:name="title">Task Summary</ac:parameter>'
            "<ac:rich-text-body><p>Summary these tasks here</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        ),
        "<h2>Completed this week</h2>",
        build_table(completed, JIRA_BASE_URL),
        "<h2>Added this week</h2>",
        build_table(created, JIRA_BASE_URL),
        "<h2>Current open items</h2>",
        build_table(open_items, JIRA_BASE_URL),
    ]

    return "\n".join(sections)


# =============================================================================
# Mode Execution Functions
# =============================================================================
def run_diagnostics(console: Console) -> None:
    """Run diagnostic checks to validate configuration and permissions.

    Checks:
    - Environment variables are set
    - Jira credentials are valid
    - Confluence API is accessible
    - Parent page exists and is writable

    Args:
        console: Rich console for output.
    """
    console.print("[bold]Running diagnostic checks...[/bold]\n")
    all_passed = True

    # ==========================================================================
    # Check 1: Environment Variable Validation
    # ==========================================================================
    console.print("[bold]1. Environment Variables[/bold]")
    email, token = load_credentials()

    if email:
        console.print(f"  [green]✓[/green] JIRA_EMAIL is set: {email}")
    else:
        console.print("  [red]✗[/red] JIRA_EMAIL is not set")
        console.print('    [dim]Fix: export JIRA_EMAIL="your-email@example.com"[/dim]')
        all_passed = False

    if token:
        console.print(f"  [green]✓[/green] JIRA_TOKEN is set: {'*' * 8}...")
    else:
        console.print("  [red]✗[/red] JIRA_TOKEN is not set")
        console.print('    [dim]Fix: export JIRA_TOKEN="your-api-token"[/dim]')
        console.print("    [dim]Generate at: https://id.atlassian.com/manage-profile/security/api-tokens[/dim]")
        all_passed = False

    console.print()

    # ==========================================================================
    # Check 2: Jira Authentication Test
    # ==========================================================================
    console.print("[bold]2. Jira Authentication[/bold]")
    jira = None
    if not email or not token:
        console.print("  [red]✗[/red] Cannot test - missing credentials")
        all_passed = False
    else:
        try:
            jira = connect_jira()
            console.print(f"  [green]✓[/green] Successfully authenticated to {JIRA_BASE_URL}")
        except ValueError as e:
            console.print(f"  [red]✗[/red] Missing credentials: {e}")
            all_passed = False
        except ApiError as e:
            console.print(f"  [red]✗[/red] Authentication failed: {e}")
            all_passed = False
        except RuntimeError as e:
            console.print(f"  [red]✗[/red] Connection failed: {e}")
            all_passed = False

    console.print()

    # ==========================================================================
    # Check 3: JQL Query Permission Test
    # ==========================================================================
    console.print("[bold]3. JQL Query Permissions[/bold]")
    if jira is None:
        console.print("  [red]✗[/red] Cannot test - no Jira connection")
        all_passed = False
    else:
        projects = ", ".join(f'"{p}"' for p in JIRA_PROJECTS)
        test_jql = f"assignee = currentUser() AND project IN ({projects})"
        try:
            jira.enhanced_jql(test_jql, limit=1)
            console.print("  [green]✓[/green] JQL query executed successfully")
            console.print(f"    [dim]Test query: {test_jql}[/dim]")
        except ApiError as e:
            console.print(f"  [red]✗[/red] JQL query failed: {e}")
            console.print(f"    [dim]Test query: {test_jql}[/dim]")
            all_passed = False
        except Exception as e:
            console.print(f"  [red]✗[/red] Error during query: {e}")
            all_passed = False

    console.print()

    # ==========================================================================
    # Check 4: Confluence Authentication Test
    # ==========================================================================
    console.print("[bold]4. Confluence Authentication[/bold]")
    confluence = None
    parent_page_data = None
    if not email or not token:
        console.print("  [red]✗[/red] Cannot test - missing credentials")
        all_passed = False
    else:
        try:
            confluence = connect_confluence()
            # Test by fetching the parent page
            parent_page_data = confluence.get_page_by_id(CONFLUENCE_PARENT_PAGE_ID, expand="space")
            if parent_page_data:
                console.print("  [green]✓[/green] Successfully authenticated to Confluence")
            else:
                console.print("  [red]✗[/red] Could not retrieve parent page")
                all_passed = False
        except ValueError as e:
            console.print(f"  [red]✗[/red] Credential error: {e}")
            all_passed = False
        except ApiError as e:
            error_msg = str(e)
            if "401" in error_msg:
                console.print("  [red]✗[/red] Confluence authentication failed (401)")
            elif "403" in error_msg:
                console.print("  [red]✗[/red] Confluence access denied (403)")
            else:
                console.print(f"  [red]✗[/red] Confluence API error: {e}")
            all_passed = False
        except Exception as e:
            console.print(f"  [red]✗[/red] Connection error: {e}")
            all_passed = False

    console.print()

    # ==========================================================================
    # Check 5: Parent Page Access Test
    # ==========================================================================
    console.print("[bold]5. Parent Page Access[/bold]")
    if parent_page_data is None:
        console.print("  [red]✗[/red] Cannot test - no valid Confluence response")
        all_passed = False
    else:
        try:
            page_id = parent_page_data.get("id")
            page_title = parent_page_data.get("title")
            page_type = parent_page_data.get("type")

            if page_id and page_title and page_type:
                console.print(f"  [green]✓[/green] Parent page found: {page_title}")
                console.print(f"    [dim]Page ID: {page_id}, Type: {page_type}[/dim]")
            else:
                console.print("  [red]✗[/red] Parent page response missing required fields")
                all_passed = False
        except (ValueError, KeyError) as e:
            console.print(f"  [red]✗[/red] Error parsing response: {e}")
            all_passed = False

    console.print()

    # ==========================================================================
    # Check 6: Space Permission Test
    # ==========================================================================
    console.print("[bold]6. Space Permissions[/bold]")
    if confluence is None:
        console.print("  [red]✗[/red] Cannot test - no valid Confluence connection")
        all_passed = False
    else:
        try:
            # Check permissions via space endpoint with permissions expanded
            space_data = confluence.get_space(CONFLUENCE_SPACE_KEY, expand="permissions")

            if space_data:
                permissions = space_data.get("permissions", [])

                # Check for create and update page permissions
                can_create = False
                can_update = False
                for perm in permissions:
                    op = perm.get("operation", {})
                    operation_type = op.get("operation", "")
                    target_type = op.get("targetType", "")

                    if operation_type == "create" and target_type == "page":
                        can_create = True
                    if operation_type == "update" and target_type == "page":
                        can_update = True

                if can_create and can_update:
                    console.print("  [green]✓[/green] User has create and update permissions for pages")
                    console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
                elif can_create:
                    console.print("  [yellow]![/yellow] User can create but cannot update pages")
                    console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
                    all_passed = False
                elif can_update:
                    console.print("  [yellow]![/yellow] User can update but cannot create pages")
                    console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
                    all_passed = False
                else:
                    console.print("  [red]✗[/red] User lacks create/update permissions for pages")
                    console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
                    console.print("    [dim]Verify you have write access to this space[/dim]")
                    all_passed = False
            else:
                console.print("  [red]✗[/red] Could not retrieve space information")
                console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
                all_passed = False
        except ApiError as e:
            error_msg = str(e)
            if "403" in error_msg:
                console.print("  [red]✗[/red] Access denied to space permissions (403)")
            elif "404" in error_msg:
                console.print("  [red]✗[/red] Space not found (404)")
            else:
                console.print(f"  [red]✗[/red] Confluence API error: {e}")
            console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
            all_passed = False
        except Exception as e:
            console.print(f"  [red]✗[/red] Could not verify permissions: {e}")
            console.print(f"    [dim]Space key: {CONFLUENCE_SPACE_KEY}[/dim]")
            all_passed = False

    console.print()

    # ==========================================================================
    # Check 7: Page Existence Check and Create Permission Test
    # ==========================================================================
    console.print("[bold]7. Page Existence Check & Create Permission[/bold]")
    if confluence is None:
        console.print("  [red]✗[/red] Cannot test - no valid Confluence connection")
        all_passed = False
    else:
        # Test check_page_exists with a sample title
        sample_title = "Weekly Status Diagnostic Test"
        try:
            exists = check_page_exists(sample_title)
            console.print(f"  [green]✓[/green] Page existence check works (sample title exists: {exists})")
            console.print(f"    [dim]Tested title: {sample_title}[/dim]")
        except Exception as e:
            console.print(f"  [red]✗[/red] Page existence check failed: {e}")
            all_passed = False

        # Test create permission by creating and deleting a temporary page
        import uuid

        test_page_title = f"__diag_test_{uuid.uuid4().hex[:8]}__"
        test_page_content = "<p>Diagnostic test page - safe to delete</p>"
        try:
            # Attempt to create a temporary test page
            result = confluence.create_page(
                space=CONFLUENCE_SPACE_KEY,
                title=test_page_title,
                body=test_page_content,
                parent_id=CONFLUENCE_PARENT_PAGE_ID,
                representation="storage",
            )
            test_page_id = result.get("id")

            if test_page_id:
                # Successfully created, now delete it
                try:
                    confluence.remove_page(test_page_id)
                    console.print("  [green]✓[/green] Create permission verified (test page created and deleted)")
                except Exception as del_e:
                    console.print("  [yellow]![/yellow] Created test page but failed to delete it")
                    console.print(f"    [dim]Please manually delete page: {test_page_title}[/dim]")
                    console.print(f"    [dim]Delete error: {del_e}[/dim]")
            else:
                console.print("  [red]✗[/red] Create returned no page ID")
                all_passed = False
        except ApiError as e:
            error_msg = str(e)
            if "403" in error_msg:
                console.print("  [red]✗[/red] Create permission denied (403)")
            elif "401" in error_msg:
                console.print("  [red]✗[/red] Authentication failed during create (401)")
            else:
                console.print(f"  [red]✗[/red] Create test failed: {e}")
            console.print(f"    [dim]Parent page ID: {CONFLUENCE_PARENT_PAGE_ID}[/dim]")
            all_passed = False
        except Exception as e:
            console.print(f"  [red]✗[/red] Create test error: {e}")
            all_passed = False

    console.print()

    # ==========================================================================
    # Check 8: AppFox Classification API
    # ==========================================================================
    console.print("[bold]8. AppFox Classification API[/bold]")
    appfox_key = load_appfox_api_key()
    if not appfox_key:
        console.print("  [yellow]![/yellow] APPFOX_API_KEY not set (optional)")
        console.print("    [dim]Pages will show 'Pending Classification' until manually set[/dim]")
        console.print("    [dim]To enable: Apps → Compliance → Administration → API Keys[/dim]")
        console.print('    [dim]Then: export APPFOX_API_KEY="your-api-key"[/dim]')
    else:
        console.print(f"  [green]✓[/green] APPFOX_API_KEY is set: {'*' * 8}...")
        enabled, level_names, error_msg = check_classification_api_enabled()
        if enabled:
            console.print("  [green]✓[/green] AppFox API connection successful")
            console.print(f"    [dim]Available levels: {', '.join(level_names)}[/dim]")
            if "public" in [name.lower() for name in level_names]:
                console.print("    [dim]'Public' level available for auto-classification[/dim]")
            else:
                console.print("    [yellow]![/yellow] No 'Public' level found - pages won't be auto-classified")
        else:
            console.print("  [yellow]![/yellow] AppFox API connection failed")
            console.print(f"    [dim]{error_msg}[/dim]")
            console.print("    [dim]Check API key and scopes (needs classification:read, classification:write)[/dim]")
    # Note: Classification is optional, so this doesn't fail the diagnostic

    console.print()

    # ==========================================================================
    # Summary
    # ==========================================================================
    if all_passed:
        console.print("[green]All checks passed![/green]")
        sys.exit(0)
    else:
        console.print("[yellow]Some checks failed. Review the output above.[/yellow]")
        sys.exit(1)


def run_dry_run(console: Console, week: int | None = None) -> None:
    """Show what would be created without actually creating it.

    Performs all calculations and queries but does not create the
    Confluence page. Displays:
    - Date range that would be used
    - Page title that would be created
    - Number of items in each category
    - Preview of the page content

    Args:
        console: Rich console for output.
        week: ISO week number to generate report for (default: current week).
    """
    console.print("[bold]Dry-run mode - showing what would be created[/bold]\n")

    # ==========================================================================
    # Date Range Calculation
    # ==========================================================================
    week_num = week if week is not None else get_current_week_num()
    start_date, end_date = get_week_range(week_num)
    title = format_week_title(start_date, end_date)

    console.print(f"[bold]Week Number:[/bold] {week_num}")
    console.print(f"[bold]Date Range:[/bold] {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    console.print(f"[bold]Page Title:[/bold] {title}")
    console.print()

    # ==========================================================================
    # Jira Connection and Queries
    # ==========================================================================
    try:
        console.print("Connecting to Jira...")
        jira = connect_jira()

        console.print("Querying Jira for completed items...")
        completed = get_completed_items(jira, start_date, end_date)

        console.print("Querying Jira for created items...")
        created = get_created_items(jira, start_date, end_date)

        console.print("Querying Jira for open items...")
        open_items = get_open_items(jira, end_date)

        console.print()
        console.print(f"[bold]Completed items:[/bold] {len(completed)}")
        console.print(f"[bold]Created items:[/bold] {len(created)}")
        console.print(f"[bold]Open items:[/bold] {len(open_items)}")
        console.print()

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except ApiError as e:
        console.print(f"[red]Jira error:[/red] {e}")
        sys.exit(1)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # ==========================================================================
    # Duplicate Check
    # ==========================================================================
    console.print("Checking for existing page...")
    if check_page_exists(title):
        console.print(f"[yellow]Warning:[/yellow] Page with title '{title}' already exists")
    else:
        console.print("[green]Page does not exist yet[/green]")
    console.print()

    # ==========================================================================
    # Content Preview
    # ==========================================================================
    content = build_page_content(completed, created, open_items)

    console.print("[bold]Content Preview:[/bold]")
    preview_length = 500
    if len(content) > preview_length:
        console.print(f"{content[:preview_length]}...")
        console.print(f"[dim]... ({len(content) - preview_length} more characters)[/dim]")
    else:
        console.print(content)

    console.print()
    console.print("[blue]Dry-run complete. No page was created.[/blue]")


def run_normal_mode(console: Console, week: int | None = None) -> None:
    """Execute the normal workflow to create the weekly status page.

    Full workflow:
    1. Calculate week date range (current week by default)
    2. Connect to Jira
    3. Query for completed, created, and open items
    4. Check if page already exists
    5. Build page content
    6. Create Confluence page

    Args:
        console: Rich console for output.
        week: ISO week number to generate report for (default: current week).
    """
    # ==========================================================================
    # Date Range and Title Generation
    # ==========================================================================
    week_num = week if week is not None else get_current_week_num()
    start_date, end_date = get_week_range(week_num)
    title = format_week_title(start_date, end_date)

    console.print(f"[bold]Week Number:[/bold] {week_num}")
    console.print(f"Creating weekly status for: {title}")
    console.print()

    # ==========================================================================
    # Duplicate Page Check
    # ==========================================================================
    console.print("Checking for existing page...")
    if check_page_exists(title):
        console.print(f"[yellow]Page '{title}' already exists. Skipping creation.[/yellow]")
        return

    # ==========================================================================
    # Jira Connection and Queries
    # ==========================================================================
    try:
        console.print("Connecting to Jira...")
        jira = connect_jira()

        console.print("Querying Jira for completed items...")
        completed = get_completed_items(jira, start_date, end_date)

        console.print("Querying Jira for created items...")
        created = get_created_items(jira, start_date, end_date)

        console.print("Querying Jira for open items...")
        open_items = get_open_items(jira, end_date)

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except ApiError as e:
        console.print(f"[red]Jira error:[/red] {e}")
        sys.exit(1)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # ==========================================================================
    # Content Generation
    # ==========================================================================
    console.print("Building page content...")
    content = build_page_content(completed, created, open_items)

    # ==========================================================================
    # Confluence Page Creation
    # ==========================================================================
    try:
        console.print("Creating Confluence page...")
        result = create_child_page(title, content)

        console.print()
        console.print("[green]✓[/green] Page created successfully!")
        console.print(f"[bold]URL:[/bold] {result.url}")

        # Display classification status
        if result.classification_status == "success":
            console.print("[green]✓[/green] Page classified as 'Public'")
        elif result.classification_status == "skipped":
            console.print(f"[yellow]![/yellow] Classification skipped: {result.classification_message}")
        else:  # failed
            console.print(f"[yellow]![/yellow] Classification failed: {result.classification_message}")

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except ApiError as e:
        console.print(f"[red]Confluence error:[/red] {e}")
        sys.exit(1)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("[dim]Check your network connectivity to linaro.atlassian.net[/dim]")
        sys.exit(1)

    # ==========================================================================
    # Summary Output
    # ==========================================================================
    console.print()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  Completed items: {len(completed)}")
    console.print(f"  Created items: {len(created)}")
    console.print(f"  Open items: {len(open_items)}")


# =============================================================================
# Click CLI
# =============================================================================
@click.group(invoke_without_command=True)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose/debug logging.",
)
@click.option(
    "--week",
    "-w",
    type=int,
    default=None,
    help="ISO week number to generate report for (default: current week).",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, week: int | None) -> None:
    """Jira Weekly Status Automation Script.

    Creates Confluence pages with weekly task snapshots from Jira queries.
    Automatically calculates the current week's Monday-Friday range and
    queries for completed, created, and open items.

    \b
    Examples:
        ./weekly_status.py              # Create status for current week
        ./weekly_status.py --week 4     # Create status for week 4
        ./weekly_status.py -w 52        # Create status for week 52
        ./weekly_status.py diagnose     # Validate configuration
        ./weekly_status.py dry-run      # Preview without creating
        ./weekly_status.py -v           # Create with verbose logging

    \b
    Week Numbers:
        Uses ISO week calendar where week 1 contains the first Thursday
        of the year. Week numbers range from 1 to 52 (or 53 in some years).
    """
    # Ensure context object exists
    ctx.ensure_object(dict)

    # Store options in context for subcommands
    ctx.obj["verbose"] = verbose
    ctx.obj["week"] = week

    # Configure logging based on verbosity
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s: %(message)s",
        )

    # Initialize console and store in context
    ctx.obj["console"] = Console()

    # Run normal mode if no subcommand is invoked
    if ctx.invoked_subcommand is None:
        run_normal_mode(ctx.obj["console"], ctx.obj["week"])


@cli.command(name="diagnose")
@click.pass_context
def diagnose_cmd(ctx: click.Context) -> None:
    """Run diagnostic checks to validate configuration and permissions.

    Checks environment variables, API connectivity, and required permissions.
    """
    run_diagnostics(ctx.obj["console"])


@cli.command(name="dry-run")
@click.pass_context
def dry_run_cmd(ctx: click.Context) -> None:
    """Preview what would be created without actually creating the page.

    Shows the calculated date range, Jira queries, and fetched items.
    """
    run_dry_run(ctx.obj["console"], ctx.obj.get("week"))


# =============================================================================
# Entry Point
# =============================================================================
if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter

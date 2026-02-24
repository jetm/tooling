"""devtool gitlab — shared GitLab client utilities."""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    import gitlab

logger = logging.getLogger(__name__)

DEFAULT_GITLAB_URL = "https://gitlab.com"
MR_URL_PATTERN = re.compile(r"https://gitlab\.com/(.+?)/-/merge_requests/(\d+)")
PROJECT_URL_PATTERN = re.compile(r"https://gitlab\.com/(.+?)/?$")
REMOTE_SSH_PATTERN = re.compile(r"git@gitlab\.com:(.+?)(?:\.git)?$")
REMOTE_HTTPS_PATTERN = re.compile(r"https://gitlab\.com/(.+?)(?:\.git)?/?$")


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


def detect_project_path() -> str:
    """Detect GitLab project path from the current repo's origin remote URL.

    Supports both SSH (git@gitlab.com:path) and HTTPS (https://gitlab.com/path) formats.
    Raises ClickException if not in a git repo or remote is not gitlab.com.
    """
    from devtool._deps import require

    git = require("git", "gitlab commands")

    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        raise click.ClickException("Not in a git repository. Use --project-url to specify the project.") from None

    if not repo.remotes or "origin" not in [r.name for r in repo.remotes]:
        raise click.ClickException("No 'origin' remote found. Use --project-url to specify the project.")

    origin_url = repo.remotes.origin.url

    for pattern in (REMOTE_SSH_PATTERN, REMOTE_HTTPS_PATTERN):
        match = pattern.match(origin_url)
        if match:
            project_path = match.group(1)
            logger.info(f"Auto-detected project path: {project_path}")
            return project_path

    raise click.ClickException(
        f"Origin remote '{origin_url}' is not a gitlab.com URL. Use --project-url to specify the project."
    )


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


def connect_gitlab(token: str) -> gitlab.Gitlab:
    """Authenticate and return a connected Gitlab instance."""
    from devtool._deps import require

    gitlab_mod = require("gitlab", "gitlab commands")
    Gitlab = gitlab_mod.Gitlab

    logger.info(f"Connecting to GitLab at {DEFAULT_GITLAB_URL}")
    gl = Gitlab(DEFAULT_GITLAB_URL, private_token=token)
    gl.auth()
    logger.info("Authentication successful")
    return gl

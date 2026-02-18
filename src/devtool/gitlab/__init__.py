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

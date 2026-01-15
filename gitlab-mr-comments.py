#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "python-gitlab>=4.0.0",
#     "rich>=13.0.0",
# ]
# ///
"""
GitLab Merge Request Comments Fetcher

This script fetches unresolved comments from a GitLab merge request and displays
them with code context for LLM consumption. It authenticates using a GitLab token,
retrieves all unresolved discussion threads, extracts relevant code context from
the MR diffs, and outputs formatted plain text.

Usage:
    ./gitlab-mr-comments.py --project-id 12345 --mr-id 67 --token <token>
    ./gitlab-mr-comments.py --project group/project --mr-id 67 --token <token>
    ./gitlab-mr-comments.py --mr-url https://gitlab.com/group/project/-/merge_requests/123
    ./gitlab-mr-comments.py --mr-url <url> --output comments.txt
    GITLAB_TOKEN=<token> ./gitlab-mr-comments.py --project-id 12345 --mr-id 67
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

from gitlab import Gitlab, GitlabAuthenticationError, GitlabGetError

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


def parse_mr_url(url: str) -> tuple[str, int] | None:
    """
    Parse a GitLab merge request URL to extract project path and MR ID.

    Args:
        url: GitLab MR URL (e.g., https://gitlab.com/group/project/-/merge_requests/123)

    Returns:
        Tuple of (project_path, mr_id) if URL matches pattern, None otherwise
    """
    match = MR_URL_PATTERN.match(url)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def get_gitlab_token(cli_token: str | None = None) -> str:
    """
    Get GitLab token from environment variable or CLI argument.

    Args:
        cli_token: Token provided via CLI argument

    Returns:
        GitLab authentication token

    Raises:
        ValueError: If no token is provided
    """
    # Check environment variable first
    env_token = os.getenv("GITLAB_TOKEN")

    if env_token:
        logger.info("Using GitLab token from GITLAB_TOKEN environment variable")
        return env_token

    if cli_token:
        logger.info("Using GitLab token from --token argument")
        return cli_token

    raise ValueError(
        "No GitLab token provided. Set GITLAB_TOKEN environment variable or use --token argument"
    )


def fetch_unresolved_discussions(
    gl: Gitlab, project_id: int, mr_iid: int
) -> tuple[list[Any], str, str]:
    """
    Fetch all unresolved discussion threads from a merge request.

    Args:
        gl: Authenticated GitLab client
        project_id: GitLab project ID
        mr_iid: Merge request IID (internal ID)

    Returns:
        Tuple containing:
        - List of unresolved discussion objects
        - Project name with namespace
        - Merge request title
    """
    logger.info(f"Fetching merge request {mr_iid} from project {project_id}")
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)

    logger.info(f"Project: {project.name_with_namespace}")
    logger.info(f"Merge Request: {mr.title}")
    logger.info("Fetching all discussions")
    discussions = mr.discussions.list(get_all=True)
    logger.debug(f"Total discussions found: {len(discussions)}")

    # Filter for unresolved threads
    unresolved = []
    for discussion in discussions:
        # Skip individual notes (standalone comments, not discussion threads)
        if discussion.attributes.get("individual_note", False):
            continue

        notes = discussion.attributes.get("notes", [])
        if not notes:
            continue

        # Check if any note in the thread is resolvable and unresolved
        # The resolved attribute exists at the note level, not discussion level
        has_unresolved_note = False
        for note in notes:
            if note.get("resolvable", False) and not note.get("resolved", False):
                has_unresolved_note = True
                break

        if has_unresolved_note:
            unresolved.append(discussion)

    logger.info(f"Found {len(unresolved)} unresolved discussion threads")
    return unresolved, project.name_with_namespace, mr.title


def get_code_context(
    gl: Gitlab,
    project_id: int,
    mr_iid: int,
    file_path: str,
    line_number: int | None,
    context_lines: int = 3,
    is_old_side: bool = False,
) -> dict[str, Any]:
    """
    Extract code context from MR diff around a specific line.

    Args:
        gl: Authenticated GitLab client
        project_id: GitLab project ID
        mr_iid: Merge request IID
        file_path: Path to the file in the repository
        line_number: Target line number (new line in diff, or old line if is_old_side=True)
        context_lines: Number of lines to show before and after (default: 3)
        is_old_side: Whether the comment is on the old side (deleted/unchanged lines)

    Returns:
        Dictionary with before_lines, target_line, after_lines, and line_numbers
    """
    if not file_path or line_number is None:
        return {
            "before_lines": [],
            "target_line": None,
            "after_lines": [],
            "line_numbers": [],
        }

    try:
        project = gl.projects.get(project_id)
        mr = project.mergerequests.get(mr_iid)
        changes = mr.changes()

        # Find the file in changes
        target_change = None
        for change in changes.get("changes", []):
            if (
                change.get("new_path") == file_path
                or change.get("old_path") == file_path
            ):
                target_change = change
                break

        if not target_change:
            logger.debug(f"File {file_path} not found in MR changes")
            return {
                "before_lines": [],
                "target_line": None,
                "after_lines": [],
                "line_numbers": [],
            }

        # Parse the diff to extract context
        diff_content = target_change.get("diff", "")
        if not diff_content:
            return {
                "before_lines": [],
                "target_line": None,
                "after_lines": [],
                "line_numbers": [],
            }

        # Parse unified diff format
        lines = diff_content.split("\n")
        old_line_num = 0
        new_line_num = 0
        collected_old_lines = []
        collected_new_lines = []

        for line in lines:
            if line.startswith("@@"):
                # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
                parts = line.split()
                for part in parts:
                    if part.startswith("-"):
                        old_start = part[1:].split(",")[0]
                        old_line_num = int(old_start) - 1
                    elif part.startswith("+"):
                        new_start = part[1:].split(",")[0]
                        new_line_num = int(new_start) - 1
                continue

            if line.startswith("-"):
                # Removed line, only on old side
                old_line_num += 1
                collected_old_lines.append((old_line_num, line[1:]))
            elif line.startswith("+"):
                # Added line, only on new side
                new_line_num += 1
                collected_new_lines.append((new_line_num, line[1:]))
            elif line.startswith(" "):
                # Context line, on both sides
                old_line_num += 1
                new_line_num += 1
                collected_old_lines.append((old_line_num, line[1:]))
                collected_new_lines.append((new_line_num, line[1:]))

        # Select which side to use based on is_old_side
        collected_lines = collected_old_lines if is_old_side else collected_new_lines

        # Find target line and extract context
        target_idx = None
        for idx, (num, _) in enumerate(collected_lines):
            if num == line_number:
                target_idx = idx
                break

        if target_idx is None:
            logger.debug(f"Line {line_number} not found in diff")
            return {
                "before_lines": [],
                "target_line": None,
                "after_lines": [],
                "line_numbers": [],
            }

        # Extract context
        start_idx = max(0, target_idx - context_lines)
        end_idx = min(len(collected_lines), target_idx + context_lines + 1)

        before_lines = [line for _, line in collected_lines[start_idx:target_idx]]
        target_line = collected_lines[target_idx][1]
        after_lines = [line for _, line in collected_lines[target_idx + 1 : end_idx]]
        line_numbers = [num for num, _ in collected_lines[start_idx:end_idx]]

        return {
            "before_lines": before_lines,
            "target_line": target_line,
            "after_lines": after_lines,
            "line_numbers": line_numbers,
        }

    except Exception as e:
        logger.debug(f"Error extracting code context: {e}")
        return {
            "before_lines": [],
            "target_line": None,
            "after_lines": [],
            "line_numbers": [],
        }


def format_output(
    project_id: int,
    mr_iid: int,
    unresolved_threads: list[Any],
    gl: Gitlab,
    project_name: str,
    mr_title: str,
) -> str:
    """
    Format unresolved discussions into plain text output for LLM consumption.

    Args:
        project_id: GitLab project ID
        mr_iid: Merge request IID
        unresolved_threads: List of unresolved discussion objects
        gl: Authenticated GitLab client
        project_name: Project name with namespace
        mr_title: Merge request title

    Returns:
        Formatted plain text string
    """
    separator = "=" * 80
    output_lines = []

    # Header
    output_lines.append(separator)
    output_lines.append("GITLAB MERGE REQUEST UNRESOLVED COMMENTS")
    output_lines.append(separator)
    output_lines.append(f"Project: {project_name} (ID: {project_id})")
    output_lines.append(f"Merge Request: {mr_title} (IID: {mr_iid})")
    output_lines.append(f"Unresolved Threads: {len(unresolved_threads)}")
    output_lines.append(separator)
    output_lines.append("")

    # Process each thread
    for thread_num, discussion in enumerate(unresolved_threads, 1):
        notes = discussion.attributes.get("notes", [])
        if not notes:
            continue

        first_note = notes[0]
        position = first_note.get("position", {})

        # Try new_line first, fall back to old_line
        new_line = position.get("new_line") if position else None
        old_line = position.get("old_line") if position else None

        # Determine which side the comment is on
        if new_line is not None:
            line_number = new_line
            file_path = position.get("new_path")
            is_old_side = False
        elif old_line is not None:
            line_number = old_line
            file_path = position.get("old_path")
            is_old_side = True
        else:
            line_number = None
            file_path = None
            is_old_side = False

        # Thread header
        output_lines.append(f"THREAD #{thread_num}")
        output_lines.append("-" * 80)

        # File and line info
        if file_path:
            output_lines.append(f"File: {file_path}")
            if line_number:
                output_lines.append(f"Line: {line_number}")
        else:
            output_lines.append("Location: General comment (no specific file/line)")
        output_lines.append("")

        # Code context
        if file_path and line_number:
            context = get_code_context(
                gl, project_id, mr_iid, file_path, line_number, is_old_side=is_old_side
            )
            if context["target_line"] is not None:
                output_lines.append("Code Context:")
                output_lines.append("-" * 80)

                all_lines = (
                    context["before_lines"]
                    + [context["target_line"]]
                    + context["after_lines"]
                )
                line_nums = context["line_numbers"]

                for i, (num, line) in enumerate(zip(line_nums, all_lines)):
                    # Mark the target line
                    marker = ">" if i == len(context["before_lines"]) else " "
                    output_lines.append(f"  {marker} {num:4d} | {line}")

                output_lines.append("-" * 80)
                output_lines.append("")

        # Comments in thread
        output_lines.append("Comments:")
        output_lines.append("-" * 80)
        for note in notes:
            author = note.get("author", {}).get("username", "unknown")
            body = note.get("body", "")
            output_lines.append(f"@{author}:")
            # Indent comment body
            for line in body.split("\n"):
                output_lines.append(f"  {line}")
            output_lines.append("")

        output_lines.append(separator)
        output_lines.append("")

    return "\n".join(output_lines)


def main() -> None:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Fetch unresolved comments from a GitLab merge request with code context"
    )
    project_group = parser.add_mutually_exclusive_group()
    project_group.add_argument(
        "--project-id",
        type=int,
        help="GitLab project ID (numeric)",
    )
    project_group.add_argument(
        "--project",
        type=str,
        help="GitLab project path (e.g., group/project)",
    )
    parser.add_argument(
        "--mr-id",
        type=int,
        help="Merge request IID (internal ID)",
    )
    parser.add_argument(
        "--mr-url",
        type=str,
        help="GitLab merge request URL (e.g., https://gitlab.com/group/project/-/merge_requests/123)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="GitLab authentication token (or set GITLAB_TOKEN env var)",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output file path (default: stdout)",
    )
    args = parser.parse_args()

    # Validate and resolve arguments
    if args.mr_url:
        # --mr-url provided: extract project and MR ID from URL
        if args.project_id or args.project or args.mr_id:
            parser.error(
                "--mr-url cannot be used with --project-id, --project, or --mr-id"
            )
        parsed = parse_mr_url(args.mr_url)
        if not parsed:
            parser.error(
                "Invalid GitLab MR URL format. "
                "Expected: https://gitlab.com/{project}/-/merge_requests/{id}"
            )
        project_identifier, mr_id = parsed
    else:
        # Traditional arguments: require project and MR ID
        if not (args.project_id or args.project):
            parser.error("one of --project-id, --project, or --mr-url is required")
        if not args.mr_id:
            parser.error("--mr-id is required when not using --mr-url")
        project_identifier = args.project_id if args.project_id else args.project
        mr_id = args.mr_id

    try:
        # Get authentication token
        token = get_gitlab_token(args.token)

        # Initialize GitLab client
        logger.info(f"Connecting to GitLab at {DEFAULT_GITLAB_URL}")
        gl = Gitlab(DEFAULT_GITLAB_URL, private_token=token)
        gl.auth()
        logger.info("Authentication successful")

        # Resolve project
        project = gl.projects.get(project_identifier)
        project_id = project.id

        # Fetch unresolved discussions
        unresolved_threads, project_name, mr_title = fetch_unresolved_discussions(
            gl, project_id, mr_id
        )

        if not unresolved_threads:
            console.print("[yellow]No unresolved discussion threads found[/yellow]")
            sys.exit(0)

        # Format and output
        output = format_output(
            project_id, mr_id, unresolved_threads, gl, project_name, mr_title
        )
        if args.output:
            Path(args.output).write_text(output)
            logger.info(f"Output written to {args.output}")
        else:
            print(output)

        sys.exit(0)

    except GitlabAuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        sys.exit(1)
    except GitlabGetError as e:
        logger.error(f"Failed to fetch data from GitLab: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

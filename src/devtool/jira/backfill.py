"""Jira issue backfill -- AI-generate description from code changes."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)

_MARKDOWN_HEADING_RE = re.compile(r"^(#{2,3}) (.+)$", re.MULTILINE)


def _heading_to_jira(match: re.Match) -> str:
    level = len(match.group(1))
    return f"h{level}. {match.group(2)}"


BACKFILL_PROMPT_TEMPLATE = """\
You are analyzing a code diff to write a Jira issue summary and description.

Focus on the PROBLEM that was solved, not the implementation details.
Describe what was wrong, missing, or needed -- not how the code was changed.

Do NOT use em dashes (—) or en dashes (–). Use plain hyphens (-) only.

Output format (no markdown fences, no preamble, use Jira wiki markup):

Title: <one-line summary of the problem, max 80 chars>

h2. Problem
<2-3 sentences describing the problem or need that motivated this change>

h2. Acceptance Criteria
<bulleted list of observable outcomes that confirm the problem is solved>

---

Here is the diff:

{diff}
"""


def backfill_jira_issue(issue_key: str, diff: str, cwd: str, console: Console) -> str | None:
    """Generate a problem-focused description from a diff and update the Jira issue.

    Args:
        issue_key: The Jira issue key to update (e.g., "IOTIL-201").
        diff: The git diff content.
        cwd: The working directory for Claude.
        console: Rich console for output.

    Returns:
        The generated description string, or None on failure.
    """
    from devtool.common.claude import generate_with_progress
    from devtool.common.console import print_error
    from devtool.jira.client import connect_jira

    prompt = BACKFILL_PROMPT_TEMPLATE.format(diff=diff)

    # Generate description via Claude
    try:
        raw = generate_with_progress(
            console,
            prompt,
            cwd,
            message="Generating Jira description...",
        )
    except Exception as e:
        logger.error(f"Claude generation failed: {e}")
        print_error(console, f"Failed to generate description: {e}")
        return None

    if not raw or not raw.strip():
        print_error(console, "Claude returned empty content")
        return None

    # Parse title from output
    lines = raw.strip().split("\n")
    title = None
    description_lines = []
    for i, line in enumerate(lines):
        if line.startswith("Title:"):
            title = line.removeprefix("Title:").strip()
            description_lines = lines[i + 1 :]
            break

    if not title:
        # Use first non-empty line as title
        for line in lines:
            stripped = line.strip()
            if stripped:
                title = stripped[:80]
                break
        description_lines = lines

    description = "\n".join(description_lines).strip()

    # Sanitize: replace em/en dashes and convert stray markdown headings
    title = title.replace("\u2014", "-").replace("\u2013", "-")
    description = description.replace("\u2014", "-").replace("\u2013", "-")
    description = _MARKDOWN_HEADING_RE.sub(_heading_to_jira, description)

    # Print to terminal
    console.print(f"\n[bold]Generated for {issue_key}:[/bold]")
    console.print(f"  Title: {title}")
    if description:
        console.print()
        console.print(description)
    console.print()

    # Update Jira
    try:
        jira_client = connect_jira()
        fields = {"summary": title}
        if description:
            fields["description"] = description
        jira_client.update_issue_field(issue_key, fields)
        console.print(f"[green]Updated {issue_key} in Jira[/green]")
    except Exception as e:
        logger.error(f"Failed to update Jira issue {issue_key}: {e}")
        print_error(console, f"Failed to update Jira: {e}")
        return None

    return raw.strip()

#!/usr/bin/env python3
"""
Auto-update CLAUDE.md with current project state.

Updates sections marked with AUTO-UPDATE comments:
- metadata: Last updated timestamp

Usage:
    uv run python update-claude-md.py
    uv run python update-claude-md.py --check  # Verify without modifying
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_metadata_content() -> str:
    """Generate metadata section content."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"<!-- Last updated: {today} -->\n<!-- Run: uv run python scripts/update_claude_md.py -->"


def update_section(content: str, section: str, new_content: str) -> str:
    """Update a section between AUTO-UPDATE markers."""
    pattern = (
        rf"(<!-- AUTO-UPDATE:{section} -->\n).*?(\n<!-- AUTO-UPDATE:{section}:end -->)"
    )
    replacement = rf"\1{new_content}\2"
    return re.sub(pattern, replacement, content, flags=re.DOTALL)


def main():
    parser = argparse.ArgumentParser(
        description="Update CLAUDE.md with current project state"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if updates are needed without modifying",
    )
    args = parser.parse_args()

    claude_md_path = Path.cwd() / "CLAUDE.md"

    if not claude_md_path.exists():
        print(f"Error: {claude_md_path} not found")
        sys.exit(1)

    original_content = claude_md_path.read_text()
    content = original_content

    # Update each section
    content = update_section(content, "metadata", get_metadata_content())

    if content == original_content:
        print("CLAUDE.md is up to date")
        return

    if args.check:
        print("CLAUDE.md needs updating. Run without --check to update.")
        sys.exit(1)

    claude_md_path.write_text(content)
    print(f"Updated {claude_md_path}")


if __name__ == "__main__":
    main()

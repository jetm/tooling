#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "claude-agent-sdk>=0.1.0",
#     "click>=8.0.0",
#     "rich>=13.0.0",
#     "pyperclip>=1.8.0",
#     "tomli>=2.0.0",
# ]
# ///
"""
lx - Linux command assistant (Claude-powered)

A small CLI that lets you ask a Linux-focused assistant for step-by-step guidance,
including example commands, explanations, best practices, and safety warnings.

This tool follows the repository's established uv-script + Click CLI pattern and
reuses shared utilities from `common_utils.py` (console formatting, config,
logging, Claude SDK wrapper, and consistent error handling).

Usage examples:

  # Basic usage
  ./lx.py "show disk usage"

  # Multi-word instruction
  ./lx.py show me all running processes sorted by memory usage

  # With verbose logging
  ./lx.py -v "configure firewall to allow port 8080"

  # Plain text output (no colors/formatting)
  ./lx.py --plain-text "list all users on the system"

Features:
  - Claude response rendered with Rich Markdown output (unless --plain-text)
  - Extracts bash/sh/shell fenced code blocks and inline prompt-style commands
    for later interactive handling (next phase)
  - Shares configuration via ~/.config/aca/config.toml through common utilities
"""

import logging
import re
import sys
from pathlib import Path

import click

from common_utils import (
    ACAError,
    check_claude_cli,
    generate_with_progress,
    get_config,
    get_console,
    print_error,
    print_output,
    setup_logging,
)

logger = logging.getLogger(__name__)


LINUX_ENGINEER_PROMPT = """I want you to act as a **Linux engineer with deep expertise in command-line operations, system administration, and troubleshooting**. You should be capable of providing detailed explanations and step-by-step guidance for any Linux-related tasks, including shell scripting, process management, networking, package management, permissions, backup, and security configurations. When I ask a question or describe a task, you will respond with **clear examples of commands**, describe what each command does, and include **best practices** and possible pitfalls to avoid. If there are multiple methods, explain their differences and when to use each.

Your tone should be professional, precise, and educational—imagine you are mentoring a junior system administrator. Always confirm important commands that could modify the system and explain any potential risks before execution.
"""


def extract_commands(response: str) -> list[str]:
    """Extract executable commands from a Claude response.

    Extracts:
      - Fenced code blocks with bash/sh/shell language identifiers.
      - Inline prompt-style commands (lines beginning with `$ ` or `# `).

    Returns:
      A list of command strings (may contain multi-line commands and chains).
    """
    commands: list[str] = []

    # Code blocks: ```bash ...```, ```sh ...```, ```shell ...```
    code_block_pattern = re.compile(
        r"```(?:bash|sh|shell)\r?\n(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    for match in code_block_pattern.finditer(response):
        block = match.group(1).strip("\n")
        if not block.strip():
            continue

        # Remove prompt prefixes inside code blocks while preserving structure.
        cleaned_lines: list[str] = []
        for line in block.splitlines():
            cleaned_lines.append(re.sub(r"^\s*[$#]\s+", "", line))
        cleaned = "\n".join(cleaned_lines).strip()
        if cleaned:
            commands.append(cleaned)

    # Inline commands: lines beginning with "$ " or "# "
    lines = response.splitlines()
    inline_pattern = re.compile(r"^\s*[$#]\s+(.+)$")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = inline_pattern.match(line)
        if not m:
            i += 1
            continue

        cmd_lines: list[str] = [m.group(1).rstrip()]
        while i + 1 < len(lines):
            cur = cmd_lines[-1].rstrip()
            if not cur:
                break
            if not (
                cur.endswith("\\")
                or cur.endswith("&&")
                or cur.endswith("|")
                or cur.endswith("(")
            ):
                break

            next_line = lines[i + 1]
            if not next_line.strip():
                break
            if next_line.lstrip().startswith("```"):
                break

            # Allow either continued indentation, or another prompt-prefixed line.
            next_match = inline_pattern.match(next_line)
            if next_match:
                cmd_lines.append(next_match.group(1).rstrip())
                i += 1
                continue

            cmd_lines.append(next_line.strip("\n").lstrip())
            i += 1

        cmd = "\n".join(cmd_lines).strip()
        if cmd:
            commands.append(cmd)

        i += 1

    return commands


@click.command()
@click.argument("instruction", required=True, nargs=-1)
@click.option("--plain-text", is_flag=True, help="Output plain text without formatting")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
def main(instruction: tuple[str, ...], plain_text: bool, verbose: bool) -> None:
    setup_logging(verbose)
    _ = get_config()  # Ensure shared config is loaded (and keeps import used).

    instruction_text = " ".join(instruction)
    if not instruction_text.strip():
        console = get_console(plain_text)
        print_error(console, "Instruction is required")
        sys.exit(1)

    console = get_console(plain_text)
    if not check_claude_cli(console):
        sys.exit(1)

    full_prompt = f"{LINUX_ENGINEER_PROMPT}\n\nUser Request: {instruction_text}"

    try:
        response = generate_with_progress(
            console=console,
            prompt=full_prompt,
            cwd=str(Path.cwd()),
            message="Consulting Linux engineer...",
        )
    except ACAError as e:
        print_error(console, e.format_error())
        sys.exit(1)
    except Exception as e:
        print_error(console, f"Unexpected error: {e}")
        sys.exit(1)

    print_output(console, response, markdown=True)
    console.print("\n" + ("-" * 80) + "\n")

    extracted = extract_commands(response)
    if verbose:
        logger.debug("Extracted %d command block(s) from response", len(extracted))


if __name__ == "__main__":
    main()

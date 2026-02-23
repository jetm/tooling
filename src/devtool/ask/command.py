"""devtool ask — Claude-powered Linux command assistant."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.markdown import Markdown

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)

LINUX_ENGINEER_PROMPT = """You are a Linux engineer. Provide concise, practical answers.

RESPONSE FORMAT:
1. Show 1-3 commands maximum in separate code blocks with no explanations or comments inside code blocks. Show only the commands.
2. Show the commands in the order of their importance.

Example response format:
```bash
command-here
```

```bash
another-command
```
"""


def extract_commands(response: str) -> list[str]:
    """Extract executable commands from a Claude response."""
    commands: list[str] = []
    seen: set[str] = set()

    code_block_pattern = re.compile(
        r"```(?:bash|sh|shell)\r?\n(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    for match in code_block_pattern.finditer(response):
        block = match.group(1).strip()
        if not block:
            continue
        cleaned = re.sub(r"^\s*[$#]\s+", "", block, flags=re.MULTILINE).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            commands.append(cleaned)

    return commands


def is_destructive_command(command: str) -> bool:
    """Best-effort detection of potentially destructive commands."""
    cmd = command.strip()
    if not cmd:
        return False

    lowered = cmd.lower()

    patterns: list[re.Pattern[str]] = [
        re.compile(r"(^|[;&|()]\s*)rm(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)dd(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)mkfs(\.|(\s|$))", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)(fdisk|parted)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)(shred|wipefs)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)(truncate)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)(del|format)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)chmod(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)chown(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)systemctl\s+(stop|disable)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)kill\s+-9(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)(pkill|killall)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)(reboot|shutdown|halt|poweroff)(\s|$)", re.IGNORECASE),
        re.compile(r"(^|[;&|()]\s*)init\s+[06](\s|$)", re.IGNORECASE),
    ]

    if any(p.search(cmd) for p in patterns):
        if re.search(r"(^|[;&|()]\s*)rm(\s|$)", cmd, re.IGNORECASE):
            return True
        if re.search(r"(^|[;&|()]\s*)chmod\s+000\b", lowered):
            return True
        return True

    return " --no-preserve-root" in lowered


def execute_command(command: str, console: Console) -> tuple[bool, int]:
    """Execute a shell command and print stdout/stderr."""
    from devtool.common.console import print_error

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        if result.stdout:
            console.print(result.stdout.rstrip("\n"))
        return True, result.returncode
    except subprocess.TimeoutExpired:
        print_error(console, "Command timed out after 300 seconds")
        return False, 124
    except subprocess.CalledProcessError as e:
        if e.stdout:
            console.print(e.stdout.rstrip("\n"))
        if e.stderr:
            print_error(console, e.stderr.rstrip("\n"))
        else:
            print_error(console, f"Command failed with exit code {e.returncode}")
        return False, e.returncode
    except FileNotFoundError as e:
        print_error(console, f"Command execution failed: {e}")
        return False, 127


def confirm_destructive_command(command: str, console: Console) -> bool:
    """Confirm execution of a potentially destructive command."""
    console.print(
        "[yellow]Warning:[/yellow] This command may delete files, modify system configuration, or cause data loss."
    )
    if console.no_color:
        console.print(f"Command:\n{command}")
    else:
        console.print(Markdown(f"```bash\n{command}\n```"))
    answer = input("Are you sure you want to execute this command? Type 'yes' to confirm: ")
    return answer == "yes"


def handle_commands_interactively(commands: list[str], console: Console, verbose: bool) -> None:
    """Show extracted commands and let user select one to execute."""
    commands = [c.strip() for c in commands if c.strip()]
    if not commands:
        return

    try:
        for i, cmd in enumerate(commands, 1):
            console.print(f"{i}. {cmd}")

        selection = input("Which command to run? (number or Enter to skip): ").strip()
        if not selection:
            return

        try:
            idx = int(selection)
            if idx < 1 or idx > len(commands):
                from devtool.common.console import print_error

                print_error(console, f"Invalid selection. Enter 1-{len(commands)}")
                return
        except ValueError:
            from devtool.common.console import print_error

            print_error(console, "Invalid input. Enter a number.")
            return

        cmd = commands[idx - 1]
        if verbose:
            logger.debug("User selected command %d: %s", idx, cmd[:50].replace("\n", " "))

        if is_destructive_command(cmd):
            if not confirm_destructive_command(cmd, console):
                console.print("Skipped.")
                return

        execute_command(cmd, console)

    except KeyboardInterrupt:
        console.print("\nCancelled.")


@click.command()
@click.argument("instruction", required=True)
@click.option("--markdown", is_flag=True, help="Enable Rich Markdown formatting")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
@click.pass_context
def ask(ctx: click.Context, instruction: str, markdown: bool, verbose: bool) -> None:
    """Ask a Linux question. INSTRUCTION must be quoted.

    Example: devtool ask "show disk usage"
    """
    from devtool.common.claude import generate_with_progress
    from devtool.common.config import get_config
    from devtool.common.console import (
        check_claude_cli,
        get_console,
        print_error,
        setup_logging,
    )
    from devtool.common.errors import ACAError

    setup_logging(verbose=verbose)
    config = get_config()
    console = get_console(plain_text=not markdown)

    if not config.openrouter_api_key and not check_claude_cli(console):
        sys.exit(1)

    full_prompt = f"{LINUX_ENGINEER_PROMPT}\n\nUser Request: {instruction}"

    try:
        response = generate_with_progress(
            console=console,
            prompt=full_prompt,
            cwd=str(Path.cwd()),
            message="Consulting Linux engineer...",
            model=config.default_model,
        )
    except ACAError as e:
        print_error(console, e.format_error())
        sys.exit(1)
    except Exception as e:
        print_error(console, f"Unexpected error: {e}")
        sys.exit(1)

    extracted = extract_commands(response)
    if verbose:
        logger.debug("Extracted %d command block(s) from response", len(extracted))

    handle_commands_interactively(extracted, console, verbose)

"""Rich console helpers, logging setup, and dependency checks."""

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity and config."""
    from devtool.common.config import get_config

    config = get_config()
    level = logging.DEBUG if verbose else getattr(logging, config.log_level, logging.WARNING)

    root_logger = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s:%(funcName)s:%(lineno)d] %(message)s")
    )
    root_logger.setLevel(level)
    root_logger.addHandler(handler)


def get_console(plain_text: bool) -> Console:
    """Get a Console instance configured for plain or rich output."""
    if plain_text:
        return Console(force_terminal=False, no_color=True, highlight=False)
    return Console()


def print_output(console: Console, text: str, markdown: bool = False) -> None:
    """Print output, optionally rendered as markdown."""
    if markdown and not console.no_color:
        console.print(Markdown(text))
    else:
        console.print(text)


def print_error(console: Console, message: str) -> None:
    """Print an error message."""
    if console.no_color:
        console.print(f"Error: {message}")
    else:
        console.print(f"[red]Error: {message}[/red]")


def check_dependency(executable: str, console: Console) -> bool:
    """Check if an executable exists in PATH."""
    if shutil.which(executable) is None:
        console.print(
            f"[red]Error: '{executable}' not found. Please install {executable} and ensure it's in your PATH.[/red]"
        )
        return False
    return True


def check_claude_cli(console: Console) -> str | None:
    """Check if Claude Code CLI is installed and working.

    Returns the version string on success, None on failure.
    """
    if shutil.which("claude") is None:
        console.print(
            "[red]Error: Claude Code CLI not found.[/red]\n[yellow]Install it from https://claude.ai/download[/yellow]"
        )
        return None

    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            console.print(
                "[red]Error: Claude Code CLI failed to execute.[/red]\n"
                "[yellow]Try reinstalling from https://claude.ai/download[/yellow]"
            )
            return None
    except subprocess.TimeoutExpired:
        console.print(
            "[red]Error: Claude Code CLI timed out.[/red]\n"
            "[yellow]The CLI may be hanging. Try running 'claude --version' manually.[/yellow]"
        )
        return None
    except FileNotFoundError:
        console.print(
            "[red]Error: Claude Code CLI not found.[/red]\n[yellow]Install it from https://claude.ai/download[/yellow]"
        )
        return None

    has_api_key = os.environ.get("ANTHROPIC_API_KEY") is not None
    credentials_file = Path.home() / ".claude" / ".credentials.json"
    has_credentials_file = credentials_file.exists()

    if not has_api_key and not has_credentials_file:
        console.print(
            "[red]Error: Claude Code CLI is not authenticated.[/red]\n"
            "[yellow]Run 'claude' and sign in to authenticate, "
            "or set the ANTHROPIC_API_KEY environment variable.[/yellow]"
        )
        return None

    # Extract version string from output
    version_match = re.search(r"(\d+\.\d+\.\d+)", result.stdout.strip())
    return version_match.group(1) if version_match else ""


def check_version_compatibility(console: Console, version: str | None = None) -> None:
    """Check and warn about CLI version compatibility.

    If version is provided, uses it directly instead of re-running claude --version.
    """
    if version is None:
        # Fallback: run claude --version if no cached version provided
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                version_match = re.search(r"(\d+\.\d+\.\d+)", result.stdout.strip())
                if version_match:
                    version = version_match.group(1)
        except Exception:
            pass

    if not version:
        console.print(
            "[yellow]Warning: Could not determine Claude Code CLI version. "
            "Ensure you have the latest version installed.[/yellow]"
        )

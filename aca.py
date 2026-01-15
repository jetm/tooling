#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "claude-agent-sdk>=0.1.0",
#     "rich>=13.0.0",
#     "GitPython>=3.1.0",
#     "click>=8.0.0",
#     "tomli>=2.0.0",
# ]
# ///
"""
ACA - AI Commit Assistant

A CLI tool for generating commit messages and merge request descriptions
using the Claude Agent SDK.

Generated content can be edited using your $EDITOR before committing or
creating MRs. Editor fallback chain: $EDITOR -> $VISUAL -> nano -> vi.
"""

import asyncio
import functools
import logging
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import click
import git
import tomli
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)
from rich.console import Console
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn

# Configure module logger
logger = logging.getLogger(__name__)

# Type variable for retry decorator
F = TypeVar("F", bound=Callable[..., Any])


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class ACAConfig:
    """ACA configuration loaded from config file and environment."""

    retry_attempts: int = 3
    initial_delay: float = 2.0
    backoff_factor: float = 2.0
    max_delay: float = 30.0
    timeout: int = 120
    log_level: str = "WARNING"
    editor: str | None = None

    @classmethod
    def load(cls) -> "ACAConfig":
        """Load configuration from file and environment variables."""
        config = cls()

        # Try to load from config file
        config_path = Path.home() / ".config" / "aca" / "config.toml"
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomli.load(f)

                config.retry_attempts = data.get("retry_attempts", config.retry_attempts)
                config.initial_delay = data.get("initial_delay", config.initial_delay)
                config.backoff_factor = data.get("backoff_factor", config.backoff_factor)
                config.max_delay = data.get("max_delay", config.max_delay)
                config.timeout = data.get("timeout", config.timeout)
                config.log_level = data.get("log_level", config.log_level)
                config.editor = data.get("editor", config.editor)
            except Exception as e:
                logger.warning(f"Failed to load config file {config_path}: {e}")

        # Override with environment variables
        if env_timeout := os.environ.get("ACA_TIMEOUT"):
            try:
                config.timeout = int(env_timeout)
            except ValueError:
                logger.warning(f"Invalid ACA_TIMEOUT value: {env_timeout}")

        if env_retries := os.environ.get("ACA_RETRY_ATTEMPTS"):
            try:
                config.retry_attempts = int(env_retries)
            except ValueError:
                logger.warning(f"Invalid ACA_RETRY_ATTEMPTS value: {env_retries}")

        if env_log_level := os.environ.get("ACA_LOG_LEVEL"):
            config.log_level = env_log_level.upper()

        return config


# Global config instance
_config: ACAConfig | None = None


def get_config() -> ACAConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = ACAConfig.load()
    return _config


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity and config.

    Args:
        verbose: If True, enables DEBUG level logging.
    """
    config = get_config()
    level = logging.DEBUG if verbose else getattr(logging, config.log_level, logging.WARNING)

    # Configure the root logger for the aca module
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(name)s:%(funcName)s:%(lineno)d] %(message)s"
        )
    )
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False


# =============================================================================
# Custom Exceptions
# =============================================================================


@dataclass
class ErrorContext:
    """Context information for debugging errors."""

    command: str | None = None
    exit_code: int | None = None
    stderr: str | None = None
    stdout: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    extra: dict[str, Any] = field(default_factory=dict)


class ACAError(Exception):
    """Base exception for all ACA errors."""

    def __init__(
        self,
        message: str,
        cause: Exception | None = None,
        context: ErrorContext | None = None,
        troubleshooting: list[str] | None = None,
    ):
        self.message = message
        self.cause = cause
        self.context = context or ErrorContext()
        self.troubleshooting = troubleshooting or []
        super().__init__(message)

    def format_error(self) -> str:
        """Format the error with full context and troubleshooting steps."""
        lines = [f"Error: {self.message}"]

        if self.cause:
            lines.append(f"\nCause: {self.cause}")

        if self.troubleshooting:
            lines.append("\nTroubleshooting:")
            for i, step in enumerate(self.troubleshooting, 1):
                lines.append(f"  {i}. {step}")

        # Add debug info
        debug_items = []
        if self.context.command:
            debug_items.append(f"Command: {self.context.command}")
        if self.context.exit_code is not None:
            debug_items.append(f"Exit Code: {self.context.exit_code}")
        if self.context.stderr:
            # Truncate long stderr
            stderr = self.context.stderr[:500]
            if len(self.context.stderr) > 500:
                stderr += "..."
            debug_items.append(f"Error Output: {stderr}")
        debug_items.append(f"Timestamp: {self.context.timestamp}")

        # Add extra context items
        sensitive_keys = {"api_key", "token", "secret", "password", "credential"}
        for key, value in self.context.extra.items():
            # Mask sensitive values
            key_lower = key.lower()
            if any(s in key_lower for s in sensitive_keys):
                if value:
                    debug_items.append(f"{key}: [MASKED]")
                continue
            # Handle boolean values
            if isinstance(value, bool):
                debug_items.append(f"{key}: {value}")
            # Handle None values
            elif value is None:
                continue
            # Handle string values with truncation
            elif isinstance(value, str):
                if len(value) > 500:
                    debug_items.append(f"{key}: {value[:500]}...")
                else:
                    debug_items.append(f"{key}: {value}")
            # Handle numeric values
            elif isinstance(value, (int, float)):
                debug_items.append(f"{key}: {value}")
            # Handle other types by converting to string with truncation
            else:
                str_value = str(value)
                if len(str_value) > 500:
                    debug_items.append(f"{key}: {str_value[:500]}...")
                else:
                    debug_items.append(f"{key}: {str_value}")

        if debug_items:
            lines.append("\nDebug Info:")
            for item in debug_items:
                lines.append(f"  - {item}")

        return "\n".join(lines)


class ClaudeAuthenticationError(ACAError):
    """Raised when Claude authentication fails."""

    def __init__(
        self,
        message: str = "Claude authentication failed",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
    ):
        troubleshooting = [
            "Run 'claude' in your terminal to verify authentication",
            "Check if ANTHROPIC_API_KEY environment variable is set correctly",
            "If using credentials file, check ~/.claude/.credentials.json exists and is valid",
            "Try re-authenticating by running 'claude' and signing in again",
        ]
        super().__init__(message, cause, context, troubleshooting)


class ClaudeNetworkError(ACAError):
    """Raised when network-related errors occur."""

    def __init__(
        self,
        message: str = "Network error while communicating with Claude",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
    ):
        troubleshooting = [
            "Check your internet connection",
            "Verify firewall settings allow connections to api.anthropic.com",
            "Check proxy configuration if behind a proxy (HTTP_PROXY, HTTPS_PROXY)",
            "Try running 'curl -I https://api.anthropic.com' to test connectivity",
        ]
        super().__init__(message, cause, context, troubleshooting)


class ClaudeCLIError(ACAError):
    """Raised when Claude CLI execution fails."""

    def __init__(
        self,
        message: str = "Claude CLI execution failed",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
    ):
        troubleshooting = [
            "Verify Claude CLI is installed: which claude",
            "Reinstall Claude Code CLI from https://claude.ai/download",
            "Check file permissions on the claude executable",
            "Update claude-agent-sdk: pip install --upgrade claude-agent-sdk",
        ]
        super().__init__(message, cause, context, troubleshooting)


class ClaudeTimeoutError(ACAError):
    """Raised when Claude operation times out."""

    def __init__(
        self,
        message: str = "Claude operation timed out",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
        timeout_seconds: int | None = None,
    ):
        troubleshooting = [
            f"Increase timeout (current: {timeout_seconds or 'unknown'}s) via ACA_TIMEOUT env var",
            "Check system resources (CPU/memory usage)",
            "Verify Claude service status at status.anthropic.com",
            "Try again with a simpler prompt",
        ]
        if timeout_seconds:
            context = context or ErrorContext()
            context.extra["timeout_seconds"] = timeout_seconds
        super().__init__(message, cause, context, troubleshooting)


class ClaudeRateLimitError(ACAError):
    """Raised when rate limiting is encountered."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
        retry_after: int | None = None,
    ):
        troubleshooting = [
            f"Wait {retry_after or 'a few'} seconds before retrying",
            "Check your Anthropic subscription tier and usage limits",
            "Reduce request frequency",
            "Contact Anthropic support if rate limiting persists",
        ]
        if retry_after:
            context = context or ErrorContext()
            context.extra["retry_after"] = retry_after
        super().__init__(message, cause, context, troubleshooting)


class ClaudeContentError(ACAError):
    """Raised when response content is invalid or empty."""

    def __init__(
        self,
        message: str = "Invalid or empty response from Claude",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
    ):
        troubleshooting = [
            "Retry the request",
            "Check if the prompt is valid and not too long",
            "Run 'aca doctor' to verify system configuration",
            "Report issue if this persists: https://github.com/anthropics/claude-code/issues",
        ]
        super().__init__(message, cause, context, troubleshooting)


# Exceptions that can be retried
RETRYABLE_EXCEPTIONS = (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError)


# =============================================================================
# Error Context Collection
# =============================================================================


def collect_error_context(
    command: str | None = None,
    exit_code: int | None = None,
    stderr: str | None = None,
    stdout: str | None = None,
) -> ErrorContext:
    """Collect system and environment context for error debugging.

    Args:
        command: The command that was executed
        exit_code: Exit code from the command
        stderr: Standard error output
        stdout: Standard output

    Returns:
        ErrorContext with system information
    """
    context = ErrorContext(
        command=command,
        exit_code=exit_code,
        stderr=stderr,
        stdout=stdout,
    )

    # Add system information
    context.extra["python_version"] = sys.version
    context.extra["platform"] = sys.platform

    # Get SDK version
    try:
        from importlib.metadata import version

        context.extra["sdk_version"] = version("claude-agent-sdk")
    except Exception:
        context.extra["sdk_version"] = "unknown"

    # Get CLI version
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            context.extra["cli_version"] = result.stdout.strip()
    except Exception:
        context.extra["cli_version"] = "unknown"

    # Check environment (sanitized)
    context.extra["has_api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    context.extra["has_credentials_file"] = (
        Path.home() / ".claude" / ".credentials.json"
    ).exists()

    return context


def check_network_connectivity() -> tuple[bool, str | None]:
    """Check network connectivity to Anthropic API.

    Returns:
        Tuple of (is_connected, error_message)
    """
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=5)
        return True, None
    except socket.timeout:
        return False, "Connection timed out"
    except socket.gaierror:
        return False, "DNS resolution failed"
    except OSError as e:
        return False, str(e)


# =============================================================================
# Retry Mechanism
# =============================================================================


def retry_with_backoff(
    max_attempts: int | None = None,
    initial_delay: float | None = None,
    backoff_factor: float | None = None,
    max_delay: float | None = None,
) -> Callable[[F], F]:
    """Decorator to retry async functions with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        initial_delay: Initial delay between retries in seconds
        backoff_factor: Multiplier for delay after each attempt
        max_delay: Maximum delay cap in seconds

    Returns:
        Decorated function with retry logic
    """
    config = get_config()
    _max_attempts = max_attempts if max_attempts is not None else config.retry_attempts
    _initial_delay = initial_delay if initial_delay is not None else config.initial_delay
    _backoff_factor = backoff_factor if backoff_factor is not None else config.backoff_factor
    _max_delay = max_delay if max_delay is not None else config.max_delay

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(_max_attempts):
                try:
                    return await func(*args, **kwargs)
                except RETRYABLE_EXCEPTIONS as e:
                    last_exception = e
                    if attempt < _max_attempts - 1:
                        # Calculate delay with jitter
                        delay = min(
                            _initial_delay * (_backoff_factor**attempt),
                            _max_delay,
                        )
                        # Add jitter (0.5 to 1.5 multiplier)
                        delay *= 0.5 + random.random()

                        logger.warning(
                            f"Attempt {attempt + 1}/{_max_attempts} failed: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"All {_max_attempts} attempts failed. Last error: {e}"
                        )
                except (ClaudeAuthenticationError, ClaudeCLIError, ClaudeContentError):
                    # Don't retry these errors
                    raise

            # Re-raise the last exception if all retries failed
            if last_exception:
                raise last_exception

        return wrapper  # type: ignore

    return decorator


# =============================================================================
# Utility Functions
# =============================================================================


def check_dependency(executable: str, console: Console) -> bool:
    """Check if an executable exists in PATH."""
    if shutil.which(executable) is None:
        console.print(
            f"[red]Error: '{executable}' not found. "
            f"Please install {executable} and ensure it's in your PATH.[/red]"
        )
        return False
    return True


def check_claude_cli(console: Console) -> bool:
    """Check if Claude Code CLI is installed and working.

    Verifies:
    1. The 'claude' command exists in PATH
    2. The CLI is executable and returns version info
    3. The CLI is authenticated

    Returns:
        True if all checks pass, False otherwise.
    """
    # Check if claude command exists
    if shutil.which("claude") is None:
        console.print(
            "[red]Error: Claude Code CLI not found.[/red]\n"
            "[yellow]Install it from https://claude.ai/download[/yellow]"
        )
        return False

    # Verify CLI is executable by checking version
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
            return False
    except subprocess.TimeoutExpired:
        console.print(
            "[red]Error: Claude Code CLI timed out.[/red]\n"
            "[yellow]The CLI may be hanging. Try running 'claude --version' manually.[/yellow]"
        )
        return False
    except FileNotFoundError:
        console.print(
            "[red]Error: Claude Code CLI not found.[/red]\n"
            "[yellow]Install it from https://claude.ai/download[/yellow]"
        )
        return False

    # Check authentication status using multi-layered approach
    # (mirrors claude-agent-sdk behavior: check API key first, then credentials file)
    has_api_key = os.environ.get("ANTHROPIC_API_KEY") is not None
    credentials_file = Path.home() / ".claude" / ".credentials.json"
    has_credentials_file = credentials_file.exists()

    if not has_api_key and not has_credentials_file:
        console.print(
            "[red]Error: Claude Code CLI is not authenticated.[/red]\n"
            "[yellow]Run 'claude' and sign in to authenticate, "
            "or set the ANTHROPIC_API_KEY environment variable.[/yellow]"
        )
        return False

    return True


def check_version_compatibility(console: Console) -> None:
    """Check and warn about SDK and CLI version compatibility.

    Gets installed claude-agent-sdk version and Claude Code CLI version,
    then warns if known incompatible versions are detected.
    """
    sdk_version = None
    cli_version = None

    # Get SDK version
    try:
        from importlib.metadata import version

        sdk_version = version("claude-agent-sdk")
    except Exception:
        pass

    # Get CLI version
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Parse version from output (e.g., "claude 1.0.0" or "1.0.0")
            version_output = result.stdout.strip()
            # Try to extract version number
            version_match = re.search(r"(\d+\.\d+\.\d+)", version_output)
            if version_match:
                cli_version = version_match.group(1)
    except Exception:
        pass

    # Log versions for debugging (only if both available)
    if sdk_version and cli_version:
        # Currently no known incompatibilities to check
        # This function can be extended to add version compatibility checks
        pass
    elif sdk_version and not cli_version:
        console.print(
            "[yellow]Warning: Could not determine Claude Code CLI version. "
            "Ensure you have the latest version installed.[/yellow]"
        )


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


def edit_in_editor(content: str, console: Console, file_suffix: str = ".txt") -> str:
    """Open content in user's editor for editing.

    Uses editor from config.toml first, then environment variables in order:
    $EDITOR, $VISUAL, then falls back to nano, then vi.

    Args:
        content: The content to edit
        console: Rich console for output
        file_suffix: File extension for the temporary file (e.g., ".txt", ".md")

    Returns:
        The edited content, or original content if editing fails
    """

    def try_find_editor(editor_cmd: str | None) -> tuple[list[str], str | None] | None:
        """Parse editor command and verify executable exists.

        Args:
            editor_cmd: Editor command string (may contain arguments)

        Returns:
            Tuple of (command list, resolved path) if valid, None otherwise
        """
        if not editor_cmd:
            return None
        try:
            parts = shlex.split(editor_cmd)
        except ValueError:
            # Invalid shell syntax
            return None
        if not parts:
            return None
        resolved = shutil.which(parts[0])
        if resolved:
            return (parts, resolved)
        return None

    # Try editors in priority order: config first, then env vars, then fallbacks
    config_editor = get_config().editor
    editor_sources = [
        ("config.toml", config_editor),
        ("$EDITOR", os.environ.get("EDITOR")),
        ("$VISUAL", os.environ.get("VISUAL")),
        ("fallback", "nano"),
        ("fallback", "vi"),
    ]

    editor_parts: list[str] | None = None
    for source_name, editor_cmd in editor_sources:
        result = try_find_editor(editor_cmd)
        if result:
            editor_parts, resolved_path = result
            break
        elif editor_cmd and source_name in ("config.toml", "$EDITOR", "$VISUAL"):
            # Warn user about invalid/missing editor in config or env var
            console.print(
                f"[yellow]Warning:[/yellow] {source_name}={editor_cmd!r} "
                f"not found or invalid, trying next option..."
            )

    if not editor_parts:
        print_error(
            console,
            "No editor found. Please set $EDITOR environment variable.",
        )
        return content

    # Create temporary file with content
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=file_suffix,
            delete=False,
            encoding="utf-8",
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name
    except OSError as e:
        print_error(console, f"Failed to create temporary file: {e}")
        return content

    try:
        # Open editor with the temp file appended to command
        cmd = editor_parts + [tmp_path]
        try:
            result = subprocess.run(cmd, check=False)
        except FileNotFoundError:
            # Edge case: executable disappeared between which() and run()
            print_error(
                console,
                f"Editor executable not found: {editor_parts[0]!r}",
            )
            console.print("Using original content.")
            return content

        if result.returncode != 0:
            print_error(console, f"Editor exited with code {result.returncode}")
            console.print("Using original content.")
            return content

        # Read edited content
        try:
            with open(tmp_path, encoding="utf-8") as f:
                edited_content = f.read()
            return edited_content
        except OSError as e:
            print_error(console, f"Failed to read edited file: {e}")
            return content

    finally:
        # Clean up temporary file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Ignore cleanup errors  # Ignore cleanup errors  # Ignore cleanup errors


def extract_ticket_number(branch_name: str) -> str | None:
    """Extract IOTIL ticket number from branch name."""
    match = re.match(r"^[Ii][Oo][Tt][Ii][Ll]-(\d+)", branch_name)
    if match:
        return match.group(1)
    return None


def slugify_branch_name(title: str, max_length: int = 50) -> str:
    """Convert a title into a valid git branch name slug.

    Args:
        title: The title to slugify
        max_length: Maximum length of the resulting slug (default 50)

    Returns:
        A slugified string suitable for use in a git branch name
    """
    if not title:
        return ""

    # Convert to lowercase
    slug = title.lower()

    # Replace spaces and special characters with hyphens
    # Keep only alphanumeric chars and hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", slug)

    # Remove consecutive hyphens
    slug = re.sub(r"-+", "-", slug)

    # Strip leading/trailing hyphens
    slug = slug.strip("-")

    # Truncate to max_length while preserving word boundaries
    if len(slug) > max_length:
        # Try to cut at a hyphen boundary
        truncated = slug[:max_length]
        last_hyphen = truncated.rfind("-")
        if last_hyphen > max_length // 2:
            slug = truncated[:last_hyphen]
        else:
            slug = truncated.rstrip("-")

    return slug


def rename_and_push_branch(
    repo: git.Repo, old_name: str, new_name: str, console: Console
) -> bool:
    """Rename a branch locally and update the remote.

    Args:
        repo: GitPython Repo object
        old_name: Current branch name
        new_name: New branch name
        console: Rich console for output

    Returns:
        True on success, False on failure
    """
    # Check if new branch name already exists locally
    try:
        repo.git.rev_parse("--verify", new_name)
        # Branch exists, ask user
        try:
            confirm = (
                input(
                    f"Branch '{new_name}' already exists locally. Overwrite? [y/N]: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            return False

        if confirm not in ("y", "yes"):
            console.print("Branch rename cancelled.")
            return False

        # Delete the existing branch
        try:
            repo.git.branch("-D", new_name)
        except git.exc.GitCommandError as e:
            print_error(console, f"Failed to delete existing branch '{new_name}': {e}")
            return False
    except git.exc.GitCommandError:
        # Branch doesn't exist, which is what we want
        pass

    # Rename the local branch
    console.print(f"Renaming branch from '{old_name}' to '{new_name}'...")
    try:
        repo.git.branch("-m", old_name, new_name)
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to rename branch: {e}")
        return False

    # Check if old branch exists on remote
    remote_branch_exists = False
    try:
        result = repo.git.ls_remote("--heads", "origin", old_name)
        remote_branch_exists = bool(result.strip())
    except git.exc.GitCommandError:
        # Assume no remote branch if check fails
        pass

    # Handle remote operations
    if remote_branch_exists:
        console.print(f"Deleting old remote branch 'origin/{old_name}'...")
        try:
            repo.git.push("origin", "--delete", old_name)
        except git.exc.GitCommandError as e:
            print_error(
                console,
                f"Failed to delete old remote branch: {e}\n"
                "You may need to delete it manually with: "
                f"git push origin --delete {old_name}",
            )
            # Continue anyway, we can still push the new branch

    console.print(f"Pushing new branch '{new_name}' to origin...")
    try:
        repo.git.push("origin", new_name, "--set-upstream")
    except git.exc.GitCommandError as e:
        print_error(
            console,
            f"Failed to push new branch: {e}\n"
            "You may need to push manually with: "
            f"git push origin {new_name} --set-upstream",
        )
        return False

    console.print(f"[green]Branch successfully renamed to '{new_name}'[/green]")
    return True


def get_target_branch_from_config(repo: git.Repo) -> str | None:
    """Parse git config to find branch with merge-branch = true.

    Handles both regular repos and worktrees by using repo.git_dir
    to resolve the actual git directory location.
    """
    # Use repo.git_dir which correctly resolves the actual git directory,
    # whether this is a regular repo (.git/) or a worktree (.git file pointing elsewhere)
    config_file = Path(repo.git_dir) / "config"

    if not config_file.exists():
        return None

    current_branch = None
    with open(config_file, "r") as f:
        for line in f:
            # Match branch section header
            branch_match = re.match(r'^\[branch "(.+)"\]$', line.strip())
            if branch_match:
                current_branch = branch_match.group(1)
                continue

            # Match merge-branch = true
            if re.match(r"^\s*merge-branch\s*=\s*true\s*$", line):
                if current_branch:
                    return current_branch

    return None


def _classify_error(e: Exception) -> ACAError:
    """Classify an exception into the appropriate ACAError type.

    Analyzes the exception message and type to determine the specific
    error category for appropriate handling and retry logic.

    Args:
        e: The exception to classify

    Returns:
        An appropriate ACAError subclass instance
    """
    error_str = str(e).lower()
    context = collect_error_context()

    # Authentication errors
    auth_patterns = [
        "not authenticated",
        "invalid credentials",
        "unauthorized",
        "403",
        "authentication failed",
        "auth",
        "invalid api key",
        "api key",
    ]
    if any(pattern in error_str for pattern in auth_patterns):
        return ClaudeAuthenticationError(
            message=f"Authentication failed: {e}",
            cause=e,
            context=context,
        )

    # Network errors
    network_patterns = [
        "connection refused",
        "network unreachable",
        "dns",
        "name resolution",
        "connection reset",
        "connection error",
        "socket",
        "network",
        "econnrefused",
        "enotfound",
        "getaddrinfo",
    ]
    if any(pattern in error_str for pattern in network_patterns):
        return ClaudeNetworkError(
            message=f"Network error: {e}",
            cause=e,
            context=context,
        )
    if isinstance(e, (ConnectionError, socket.error)):
        return ClaudeNetworkError(
            message=f"Network connection failed: {e}",
            cause=e,
            context=context,
        )

    # Rate limit errors
    rate_limit_patterns = [
        "rate limit",
        "429",
        "too many requests",
        "quota exceeded",
        "throttl",
    ]
    if any(pattern in error_str for pattern in rate_limit_patterns):
        # Try to extract retry-after value
        retry_after = None
        import re as re_module

        match = re_module.search(r"retry.?after[:\s]*(\d+)", error_str)
        if match:
            retry_after = int(match.group(1))
        return ClaudeRateLimitError(
            message=f"Rate limit exceeded: {e}",
            cause=e,
            context=context,
            retry_after=retry_after,
        )

    # Timeout errors
    if isinstance(e, asyncio.TimeoutError) or "timeout" in error_str:
        config = get_config()
        return ClaudeTimeoutError(
            message=f"Operation timed out: {e}",
            cause=e,
            context=context,
            timeout_seconds=config.timeout,
        )

    # CLI errors
    cli_patterns = [
        "exit code",
        "command failed",
        "permission denied",
        "incompatible version",
        "not found",
    ]
    if any(pattern in error_str for pattern in cli_patterns):
        # Extract exit code if available
        import re as re_module

        match = re_module.search(r"exit code[:\s]*(\d+)", error_str)
        if match:
            context.exit_code = int(match.group(1))
        return ClaudeCLIError(
            message=f"CLI execution failed: {e}",
            cause=e,
            context=context,
        )
    if isinstance(e, FileNotFoundError):
        return ClaudeCLIError(
            message=f"Claude CLI not found: {e}",
            cause=e,
            context=context,
        )

    # Default to CLI error for unrecognized exceptions
    return ClaudeCLIError(
        message=f"Claude operation failed: {e}",
        cause=e,
        context=context,
    )


async def _generate_with_claude_impl(
    prompt: str, cwd: str, timeout: int | None = None
) -> str:
    """Internal implementation of generate_with_claude without retry.

    Args:
        prompt: The prompt to send to Claude.
        cwd: The current working directory for the agent.
        timeout: Timeout in seconds for the operation.

    Returns:
        The accumulated text content from the response.

    Raises:
        ACAError subclasses for specific error types.
    """
    config = get_config()
    _timeout = timeout if timeout is not None else config.timeout

    options = ClaudeAgentOptions(permission_mode="acceptEdits", cwd=cwd)
    accumulated_text = ""
    result_message: ResultMessage | None = None

    logger.debug(f"Starting Claude query with timeout={_timeout}s")

    async def collect_response() -> None:
        nonlocal accumulated_text, result_message
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                # Initialization message, no text content to extract
                continue
            elif isinstance(message, AssistantMessage):
                # Extract text from TextBlock objects in the content array
                for block in message.content:
                    if isinstance(block, TextBlock):
                        accumulated_text += block.text
            elif isinstance(message, ResultMessage):
                # Capture the final result message for validation
                result_message = message

    try:
        await asyncio.wait_for(collect_response(), timeout=_timeout)
    except asyncio.TimeoutError as e:
        logger.error(f"Claude query timed out after {_timeout}s")
        raise ClaudeTimeoutError(
            message=f"Claude operation timed out after {_timeout} seconds",
            cause=e,
            context=collect_error_context(),
            timeout_seconds=_timeout,
        ) from e
    except Exception as e:
        logger.error(f"Claude query failed: {e}")
        raise _classify_error(e) from e

    # Use ResultMessage.result if available and accumulated text is empty
    if result_message is not None:
        if not accumulated_text.strip() and result_message.result:
            logger.debug("Using ResultMessage.result as response")
            return result_message.result.strip()

    # Check for empty response
    if not accumulated_text.strip():
        raise ClaudeContentError(
            message="Claude returned an empty response",
            context=collect_error_context(),
        )

    logger.debug(f"Claude query completed, response length: {len(accumulated_text)}")
    return accumulated_text.strip()


@retry_with_backoff()
async def generate_with_claude(
    prompt: str, cwd: str, timeout: int | None = None
) -> str:
    """Call Claude Agent SDK to generate content.

    Parses the streaming response from Claude Agent SDK, extracting text content
    from AssistantMessage and ResultMessage objects. Authentication is handled
    by the local Claude Code CLI, which enforces subscription requirements.

    Includes automatic retry with exponential backoff for transient errors
    (network issues, timeouts, rate limits).

    The expected message flow is:
        SystemMessage (init) → AssistantMessage(s) → ResultMessage

    Args:
        prompt: The prompt to send to Claude.
        cwd: The current working directory for the agent.
        timeout: Timeout in seconds (default from config/ACA_TIMEOUT env var).

    Returns:
        The accumulated text content from the response.

    Raises:
        ClaudeAuthenticationError: If authentication fails.
        ClaudeNetworkError: If network connectivity issues occur (after retries).
        ClaudeCLIError: If CLI execution fails.
        ClaudeTimeoutError: If operation times out (after retries).
        ClaudeRateLimitError: If rate limited (after retries).
        ClaudeContentError: If response is empty or invalid.
    """
    return await _generate_with_claude_impl(prompt, cwd, timeout)


def strip_markdown_code_blocks(text: str) -> str:
    """Remove markdown code block wrappers from text.

    Handles cases where Claude wraps responses in code blocks like:
    ```
    content
    ```
    or
    ```commit
    content
    ```
    """
    lines = text.strip().split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1] == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def clean_mr_description(description: str) -> str:
    """Clean merge request description by removing markdown wrappers and headers.

    Handles:
    - Markdown code block wrappers (```markdown ... ```)
    - Leading "# Description" or "## Description" headers (case-insensitive)
    - Leading empty lines
    """
    # First strip markdown code blocks
    cleaned = strip_markdown_code_blocks(description)

    # Split into lines and process
    lines = cleaned.split("\n")
    result_lines = []
    skip_header = True  # Skip headers only at the beginning

    for line in lines:
        if skip_header:
            # Skip empty lines at the beginning
            if not line.strip():
                continue
            # Check for "# Description" or "## Description" headers
            if re.match(r"^#{1,2}\s*description\s*$", line.strip(), re.IGNORECASE):
                continue
            # Once we hit non-empty, non-header content, stop skipping
            skip_header = False

        result_lines.append(line)

    return "\n".join(result_lines).strip()


# =============================================================================
# Progress Indicators and Fallback Templates
# =============================================================================


def generate_with_progress(
    console: Console,
    prompt: str,
    cwd: str,
    message: str = "Generating...",
) -> str:
    """Generate content with Claude showing a progress spinner.

    Args:
        console: Rich console for output
        prompt: The prompt to send to Claude
        cwd: Current working directory
        message: Message to display during generation

    Returns:
        Generated content from Claude

    Raises:
        ACAError subclasses for specific error types
    """
    # Don't show spinner if plain text mode
    if console.no_color:
        console.print(message)
        return asyncio.run(generate_with_claude(prompt, cwd))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description=message, total=None)
        return asyncio.run(generate_with_claude(prompt, cwd))


def get_commit_template(branch_name: str, ticket_number: str | None = None) -> str:
    """Get a fallback commit message template.

    Args:
        branch_name: Current git branch name
        ticket_number: Optional ticket number

    Returns:
        A conventional commit template
    """
    related_line = f"\nRelated: IOTIL-{ticket_number}" if ticket_number else ""
    return f"""<type>(<scope>): <subject>

<body - explain the WHY, not the WHAT>

Paragraph 1 - The Problem:
What is broken, missing, or suboptimal? What happens if this isn't fixed?

Paragraph 2 - The Solution:
Describe the approach at a conceptual level, not the code changes.
{related_line}

# Lines starting with '#' will be ignored.
# Branch: {branch_name}
#
# Commit message guidelines:
# - Subject line: imperative mood, max 70-75 chars
# - Body: wrap at 72 chars
# - Types: feat, fix, docs, style, refactor, test, chore
"""


def get_mr_template(
    current_branch: str, target_branch: str, ticket_number: str | None = None
) -> str:
    """Get a fallback MR description template.

    Args:
        current_branch: Source branch name
        target_branch: Target branch name
        ticket_number: Optional ticket number

    Returns:
        A merge request template
    """
    title_prefix = f"[IOTIL-{ticket_number}] " if ticket_number else ""
    return f"""Title: {title_prefix}<Brief description>

## Problem

Brief 1-2 sentence overview of what problem these changes solve.

## Solution

Explain the approach taken and why.

## Key changes

- Change 1
- Change 2
- Change 3

## Reviewer notes

Production impact, risks, dependencies, or required actions.

<!--
Branch: {current_branch} -> {target_branch}
Ticket: {ticket_number or 'Not detected'}
-->
"""


def handle_generation_error(
    console: Console,
    error: Exception,
    fallback_content: str | None = None,
    operation: str = "generation",
) -> str | None:
    """Handle generation errors with appropriate messages and fallback.

    Args:
        console: Rich console for output
        error: The exception that occurred
        fallback_content: Optional fallback template to offer
        operation: Description of the operation for error messages

    Returns:
        Fallback content if user chooses to use it, None otherwise

    Raises:
        SystemExit if user aborts
    """
    # Log full error details at debug level
    logger.debug(f"Full error details for {operation}:", exc_info=True)

    # Display appropriate error message based on error type
    if isinstance(error, ACAError):
        if console.no_color:
            console.print(error.format_error())
        else:
            console.print(f"[red]{error.format_error()}[/red]")
    else:
        print_error(console, f"Failed during {operation}: {error}")

    # Offer fallback if available
    if fallback_content:
        console.print()
        if isinstance(error, RETRYABLE_EXCEPTIONS):
            console.print(
                "[yellow]This appears to be a transient error. "
                "You can retry or use a template.[/yellow]"
            )

        try:
            choice = (
                input(
                    "Would you like to (r)etry, use (t)emplate, or (a)bort? [r/t/a]: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            sys.exit(0)

        if choice in ("r", "retry"):
            return None  # Signal to retry
        elif choice in ("t", "template"):
            console.print("\n[yellow]Opening editor with template...[/yellow]")
            return fallback_content
        elif choice in ("a", "abort"):
            console.print("Operation cancelled.")
            sys.exit(0)
        else:
            console.print("Invalid choice. Aborting.")
            sys.exit(1)
    else:
        sys.exit(1)

    return None


def extract_commit_message(text: str) -> str | None:
    """Extract clean commit message from Claude's response.

    Handles cases where Claude includes preamble text like:
    "Here is the commit message:"

    Returns the first fenced code block if present, otherwise
    the first non-empty block of text after stripping explanatory lines.
    Returns None if no valid commit message can be extracted.
    """
    text = text.strip()
    if not text:
        return None

    # Pattern for fenced code blocks (``` or ```language)
    fence_pattern = re.compile(r"^```[a-zA-Z0-9_ ]*$")

    lines = text.split("\n")

    # First, try to find and extract the first fenced code block
    in_fence = False
    fence_content: list[str] = []
    for line in lines:
        if fence_pattern.match(line.strip()):
            if not in_fence:
                # Start of fence
                in_fence = True
                fence_content = []
            else:
                # End of fence - return this block
                result = "\n".join(fence_content).strip()
                if result:
                    return result
                # Empty fence, continue looking
                in_fence = False
                fence_content = []
        elif in_fence:
            fence_content.append(line)

    # No valid fenced block found, extract first non-empty text block
    # Skip common preamble patterns
    preamble_patterns = [
        re.compile(r"^here\s+(is|are)\s+(the\s+)?(commit\s+)?message", re.IGNORECASE),
        re.compile(r"^(the\s+)?commit\s+message\s*(is|:)", re.IGNORECASE),
        re.compile(r"^i('ve|'ll| have| will| would)", re.IGNORECASE),
        re.compile(r"^(sure|okay|certainly|of course)[,!.]?\s*", re.IGNORECASE),
        re.compile(r"^based on (the |your )?", re.IGNORECASE),
        re.compile(r"^(let me|allow me)", re.IGNORECASE),
    ]

    result_lines: list[str] = []
    found_content = False

    for line in lines:
        stripped = line.strip()

        # Skip empty lines at the beginning
        if not stripped and not found_content:
            continue

        # Check if this line matches a preamble pattern
        is_preamble = False
        for pattern in preamble_patterns:
            if pattern.match(stripped):
                is_preamble = True
                break

        if is_preamble and not found_content:
            # Skip preamble lines before content
            continue

        # Skip lines that end with colon (likely preamble)
        if stripped.endswith(":") and not found_content:
            continue

        # We found content
        if stripped:
            found_content = True

        # Stop at a blank line after we've collected content (end of first block)
        if not stripped and found_content and result_lines:
            # Check if this is just a paragraph break within the commit message
            # Commit messages can have multiple paragraphs
            # Continue collecting until we hit another preamble-like section
            result_lines.append(line)
            continue

        if found_content:
            result_lines.append(line)

    result = "\n".join(result_lines).strip()
    return result if result else None


COMMIT_PROMPT = """## Generate Commit Message

Analyze the diff output and generate a commit message following these rules strictly:

### Format & Style

## Subject Line:
- Start with the correct subsystem prefix based on the file paths (e.g., `iotil/rest:`, `iotil/compliance:`, `drivers/net:`, `mm:`)
- Be imperative (e.g., "Fix memory leak" not "Fixed memory leak")
- Capitalize the first letter after the prefix
- No trailing period
- Limit to 70-75 characters

Body:
- Wrap text strictly at 72 characters
- Separate the subject from the body with a blank line
- Use the imperative mood throughout

### Content & Logic (Focus on WHY, not WHAT)

The commit body exists to explain the *reasoning* behind changes. The diff shows *what* changed - your job is to explain *why*.

Structure: Follow the "Problem -> Solution -> Rationale" pattern:

- Paragraph 1 - The Problem: What is broken, missing, or suboptimal? What happens if this isn't fixed? Be specific about the actual issue. (e.g.,
"Currently, the driver fails to reset the hardware state after suspend, causing data corruption on resume.")

- Paragraph 2 - The Solution: Describe the approach at a conceptual level, not the code changes. Explain *why* this approach solves the problem and *why*
it was chosen over alternatives. (e.g., "Add an explicit hardware reset sequence during resume. This ensures the device returns to a known state before
accepting new commands, matching the vendor's recommended initialization flow.")

### Rules
- NO External Links: Do not include URLs (http/https) in the body. If referencing external discussion or bug report, describe it textually or reference a
Commit ID/CVE ID, but never a raw link.
- NO How: Do not describe the code changes literally (e.g., avoid saying "Change x to y"). The diff shows *how*; you must explain *why*.
- NO "Changes:" section: NEVER create bullet lists of changes. No "Changes:", "What changed:", "Modified:", or similar sections. The diff already shows
what changed - the commit message explains *why*.
- NO `🤖 Generated with Claude Code` footer - NEVER
- NO `Co-Authored-By:` lines - NEVER
- NO emoji or special characters
- NO URLs or http/https links

### Optional Related Line
- If branch name contains a ticket ID (e.g., `IOTIL-1639-...`), add `Related: IOTIL-1639` as the last line before Signed-off-by

### Output
- Return *only* the commit message (Subject + Body + optional Related line)
- Do not include the diff or the list of changed files in the output
- Do not include Signed-off-by (the script adds it automatically)

### Example
```commit message
iotil/rest: Add OEQA results retrieval endpoint

Currently there is no way to fetch OEQA test results through the REST
API. Users must access the database directly or parse raw attachments
to obtain structured test results, making automation and integration
with external tools impractical.

Expose a dedicated endpoint on TestRunViewSet that returns parsed OEQA
JSON results. This enables CI systems and dashboards to consume test
data programmatically without requiring direct database access or
custom parsing logic.

Related: IOTIL-1639
```"""

MR_PROMPT_TEMPLATE = """Create a GitLab merge request for the following commits.

## Branch Information
- Current Branch: {current_branch}
- Target Branch: {target_branch}
- Ticket Number: {ticket_number}

## Commits
{commits}

## Instructions

### Step 1: Analyze Commits
1. Read through all the commit messages carefully
2. Identify the main themes or areas of change across the commits
3. Note any significant features, bug fixes, or improvements mentioned

### Step 2: Generate Title
1. Create a short, descriptive title based on the main theme
2. Format: `[IOTIL-###] <Title>` (max 50 characters total)
3. Use imperative mood (e.g., "Add feature" not "Added feature")
4. If ticket number was not detected, ask the user for it

### Step 3: Generate Description
Format the description in markdown:

```markdown
## Problem

Brief 1-2 sentence overview of what problem these changes solve.

## Solution

Explain the approach taken and why.

## Key changes

- Change 1
- Change 2
- Change 3
(Aim for 3-5 bullet points, concise but informative)

## Reviewer notes

Production impact, risks, dependencies, or required actions.
```

### Step 4: Output Format
Return only the title and description in the following format. Do not ask for confirmation or include any interactive prompts.

Now analyze the commits and generate the MR title and description only."""


@click.group()
@click.option(
    "--plain-text",
    is_flag=True,
    help="Output plain text without formatting",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    help="Enable verbose/debug logging",
)
@click.pass_context
def cli(ctx: click.Context, plain_text: bool, verbose: bool) -> None:
    """ACA - AI Commit Assistant

    Generate commit messages and merge request descriptions using Claude.

    Configuration can be set via:
      - Config file: ~/.config/aca/config.toml
      - Environment variables: ACA_TIMEOUT, ACA_RETRY_ATTEMPTS, ACA_LOG_LEVEL
    """
    ctx.ensure_object(dict)
    ctx.obj["plain_text"] = plain_text
    ctx.obj["verbose"] = verbose

    # Setup logging based on verbosity
    setup_logging(verbose=verbose)


@cli.command()
@click.pass_context
def commit(ctx: click.Context) -> None:
    """Generate a commit message for staged changes."""
    plain_text = ctx.obj.get("plain_text", False)
    console = get_console(plain_text)

    # Check git dependency
    if not check_dependency("git", console):
        sys.exit(1)

    # Check Claude Code CLI
    if not check_claude_cli(console):
        sys.exit(1)
    check_version_compatibility(console)

    # Initialize repository
    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print_error(console, "Not in a git repository")
        sys.exit(1)

    # Check for staged changes
    # Handle repos without an initial commit (HEAD doesn't exist)
    try:
        head_valid = repo.head.is_valid()
    except git.exc.GitCommandError:
        head_valid = False

    if head_valid:
        staged = repo.index.diff("HEAD")
        if not staged:
            # Also check for new files
            staged_new = repo.index.diff("HEAD", R=True)
            if not staged_new:
                print_error(
                    console, "No staged changes found. Use 'git add' to stage changes."
                )
                sys.exit(1)
    else:
        # No HEAD yet (fresh repo) - check if there are any staged files at all
        # diff against None gives us all staged files
        staged = list(repo.index.diff(None))
        if not staged and not repo.index.entries:
            print_error(
                console, "No staged changes found. Use 'git add' to stage changes."
            )
            sys.exit(1)

    # Collect git context
    try:
        branch_name = repo.active_branch.name
    except TypeError:
        print_error(console, "Not on a branch (detached HEAD state)")
        sys.exit(1)

    try:
        diff_output = repo.git.diff("--cached")
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to get staged diff: {e}")
        sys.exit(1)

    if not diff_output.strip():
        print_error(console, "No staged changes found. Use 'git add' to stage changes.")
        sys.exit(1)

    ticket_number = extract_ticket_number(branch_name)

    # Build prompt
    prompt = f"""{COMMIT_PROMPT}

## Git Context
- Branch: {branch_name}
- Ticket: {ticket_number or 'none'}

## Staged Changes Diff
{diff_output}
"""

    # Prepare fallback template for graceful degradation
    fallback_template = get_commit_template(branch_name, ticket_number)

    # Generate commit message with retry and fallback support
    commit_message: str | None = None
    max_generation_attempts = 3  # Allow user to retry generation

    for generation_attempt in range(max_generation_attempts):
        try:
            raw_response = generate_with_progress(
                console,
                prompt,
                str(repo.working_dir),
                message="Generating commit message...",
            )
            commit_message = extract_commit_message(raw_response)
            if commit_message:
                commit_message = strip_markdown_code_blocks(commit_message)
            break  # Success, exit retry loop

        except ClaudeAuthenticationError as e:
            # Auth errors: no retry, immediate failure
            logger.error(f"Authentication error: {e}")
            if console.no_color:
                console.print(e.format_error())
            else:
                console.print(f"[red]{e.format_error()}[/red]")
            sys.exit(1)

        except (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError) as e:
            # Transient errors: offer retry or fallback
            logger.warning(f"Transient error (attempt {generation_attempt + 1}): {e}")
            result = handle_generation_error(
                console,
                e,
                fallback_content=fallback_template,
                operation="commit message generation",
            )
            if result is not None:
                # User chose template, use it as commit message
                commit_message = edit_in_editor(result, console, ".txt")
                # Strip comment lines from template
                commit_message = "\n".join(
                    line for line in commit_message.split("\n")
                    if not line.strip().startswith("#")
                ).strip()
                break
            # User chose retry, continue loop

        except (ClaudeCLIError, ClaudeContentError) as e:
            # Non-transient errors: offer fallback but no automatic retry
            logger.error(f"Non-recoverable error: {e}")
            result = handle_generation_error(
                console,
                e,
                fallback_content=fallback_template,
                operation="commit message generation",
            )
            if result is not None:
                commit_message = edit_in_editor(result, console, ".txt")
                commit_message = "\n".join(
                    line for line in commit_message.split("\n")
                    if not line.strip().startswith("#")
                ).strip()
                break
            # User chose retry, continue loop

        except Exception as e:
            # Unexpected errors: log and offer fallback
            logger.exception(f"Unexpected error during generation: {e}")
            result = handle_generation_error(
                console,
                e,
                fallback_content=fallback_template,
                operation="commit message generation",
            )
            if result is not None:
                commit_message = edit_in_editor(result, console, ".txt")
                commit_message = "\n".join(
                    line for line in commit_message.split("\n")
                    if not line.strip().startswith("#")
                ).strip()
                break

    if not commit_message:
        print_error(console, "Failed to generate a valid commit message")
        console.print(
            "[yellow]Tip: Run 'aca doctor' to check your configuration.[/yellow]"
        )
        sys.exit(1)

    # Display the generated message and prompt for action
    while True:
        console.print("\n[bold]Generated Commit Message:[/bold]\n")
        print_output(console, commit_message, markdown=False)
        console.print()

        try:
            choice = (
                input("Do you want to (e)dit, (c)ommit, or (a)bort? [e/c/a]: ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            sys.exit(0)

        if choice in ("a", "abort"):
            console.print("Commit cancelled.")
            sys.exit(0)
        elif choice in ("e", "edit"):
            commit_message = edit_in_editor(commit_message, console, ".txt")
            # Loop back to display the edited message and prompt again
            continue
        elif choice in ("c", "commit"):
            break
        else:
            console.print("Invalid choice. Please enter 'e', 'c', or 'a'.")
            continue

    # Execute git commit
    result = subprocess.run(
        ["git", "commit", "--signoff", "-m", commit_message],
        capture_output=True,
        text=True,
        cwd=repo.working_dir,
    )
    if result.returncode == 0:
        console.print("[green]Commit created successfully![/green]")
        if result.stdout:
            console.print(result.stdout)
    else:
        print_error(console, f"Git commit failed: {result.stderr}")
        sys.exit(1)


@cli.command("mr-desc")
@click.pass_context
def mr_desc(ctx: click.Context) -> None:
    """Generate a merge request description."""
    plain_text = ctx.obj.get("plain_text", False)
    console = get_console(plain_text)

    # Check dependencies
    if not check_dependency("git", console):
        sys.exit(1)
    if not check_dependency("glab", console):
        sys.exit(1)

    # Check Claude Code CLI
    if not check_claude_cli(console):
        sys.exit(1)
    check_version_compatibility(console)

    # Initialize repository
    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print_error(console, "Not in a git repository")
        sys.exit(1)

    # Get current branch
    try:
        current_branch = repo.active_branch.name
    except TypeError:
        print_error(console, "Not on a branch (detached HEAD state)")
        sys.exit(1)

    # Find target branch from config
    target_branch = get_target_branch_from_config(repo)
    if not target_branch:
        print_error(
            console,
            "No target branch found in .git/config with 'merge-branch = true'\n"
            "Add this to your target branch section in .git/config:\n"
            "    merge-branch = true",
        )
        sys.exit(1)

    # Validate branches
    if current_branch == target_branch:
        print_error(console, f"Already on target branch '{target_branch}'")
        sys.exit(1)

    # Fetch target branch
    try:
        repo.git.fetch("origin", target_branch)
    except git.exc.GitCommandError:
        pass  # Ignore fetch errors

    # Determine the log range - prefer origin/<target_branch> if it exists,
    # otherwise fall back to local <target_branch> or upstream ref
    log_range = None
    log_base = None

    # Try origin/<target_branch> first
    try:
        repo.git.rev_parse("--verify", f"origin/{target_branch}")
        log_range = f"origin/{target_branch}..{current_branch}"
        log_base = f"origin/{target_branch}"
    except git.exc.GitCommandError:
        pass

    # Fall back to local target branch
    if not log_range:
        try:
            repo.git.rev_parse("--verify", target_branch)
            log_range = f"{target_branch}..{current_branch}"
            log_base = target_branch
        except git.exc.GitCommandError:
            pass

    # Fall back to upstream tracking ref for current branch
    if not log_range:
        try:
            upstream = repo.git.rev_parse("--abbrev-ref", f"{current_branch}@{{upstream}}")
            if upstream:
                log_range = f"{upstream}..{current_branch}"
                log_base = upstream
        except git.exc.GitCommandError:
            pass

    if not log_range:
        print_error(
            console,
            f"Could not find a valid base ref. Neither 'origin/{target_branch}', "
            f"'{target_branch}', nor an upstream tracking branch exists.",
        )
        sys.exit(1)

    # Get commits between branches
    try:
        commits = repo.git.log(log_range, "--pretty=format:%s")
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to get commits: {e}")
        sys.exit(1)

    if not commits.strip():
        print_error(
            console,
            f"No commits found between '{log_base}' and '{current_branch}'",
        )
        sys.exit(1)

    # Extract ticket number
    ticket_number = extract_ticket_number(current_branch)
    ticket_display = ticket_number if ticket_number else "<not detected, ask user>"

    # Build prompt
    prompt = MR_PROMPT_TEMPLATE.format(
        current_branch=current_branch,
        target_branch=target_branch,
        ticket_number=ticket_display,
        commits=commits,
    )

    # Prepare fallback template for graceful degradation
    fallback_template = get_mr_template(current_branch, target_branch, ticket_number)

    # Generate MR description with retry and fallback support
    mr_content: str | None = None
    max_generation_attempts = 3  # Allow user to retry generation

    for generation_attempt in range(max_generation_attempts):
        try:
            mr_content = generate_with_progress(
                console,
                prompt,
                str(repo.working_dir),
                message="Generating merge request description...",
            )
            break  # Success, exit retry loop

        except ClaudeAuthenticationError as e:
            # Auth errors: no retry, immediate failure
            logger.error(f"Authentication error: {e}")
            if console.no_color:
                console.print(e.format_error())
            else:
                console.print(f"[red]{e.format_error()}[/red]")
            sys.exit(1)

        except (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError) as e:
            # Transient errors: offer retry or fallback
            logger.warning(f"Transient error (attempt {generation_attempt + 1}): {e}")
            result = handle_generation_error(
                console,
                e,
                fallback_content=fallback_template,
                operation="MR description generation",
            )
            if result is not None:
                # User chose template
                mr_content = edit_in_editor(result, console, ".md")
                break
            # User chose retry, continue loop

        except (ClaudeCLIError, ClaudeContentError) as e:
            # Non-transient errors: offer fallback but no automatic retry
            logger.error(f"Non-recoverable error: {e}")
            result = handle_generation_error(
                console,
                e,
                fallback_content=fallback_template,
                operation="MR description generation",
            )
            if result is not None:
                mr_content = edit_in_editor(result, console, ".md")
                break
            # User chose retry, continue loop

        except Exception as e:
            # Unexpected errors: log and offer fallback
            logger.exception(f"Unexpected error during generation: {e}")
            result = handle_generation_error(
                console,
                e,
                fallback_content=fallback_template,
                operation="MR description generation",
            )
            if result is not None:
                mr_content = edit_in_editor(result, console, ".md")
                break

    if not mr_content:
        print_error(console, "Failed to generate MR description")
        console.print(
            "[yellow]Tip: Run 'aca doctor' to check your configuration.[/yellow]"
        )
        sys.exit(1)

    # Display the generated content and prompt for action
    while True:
        console.print("\n[bold]Generated Merge Request:[/bold]\n")
        print_output(console, mr_content, markdown=False)
        console.print()

        try:
            choice = (
                input("Do you want to (e)dit, (c)reate, or (a)bort? [e/c/a]: ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            sys.exit(0)

        if choice in ("a", "abort"):
            console.print("Merge request creation cancelled.")
            sys.exit(0)
        elif choice in ("e", "edit"):
            mr_content = edit_in_editor(mr_content, console, ".md")
            # Loop back to display the edited content and prompt again
            continue
        elif choice in ("c", "create"):
            break
        else:
            console.print("Invalid choice. Please enter 'e', 'c', or 'a'.")
            continue

    # Parse title and description from response
    # Look for patterns like: Title: ... or ## Title
    lines = mr_content.split("\n")
    title = None
    description_lines = []
    in_description = False

    for line in lines:
        # Try to find title
        if not title:
            title_match = re.match(r"^(?:Title:|##?\s*Title:?)\s*(.+)$", line, re.I)
            if title_match:
                title = title_match.group(1).strip()
                continue
            # Also check for [IOTIL-###] pattern at start
            iotil_match = re.match(r"^(\[IOTIL-\d+\].+)$", line.strip())
            if iotil_match:
                title = iotil_match.group(1).strip()
                continue

        # Collect description
        if title and (line.startswith("##") or in_description):
            in_description = True
            description_lines.append(line)

    # Fallback: use first non-empty line or first heading (without # prefixes) as title
    if not title:
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Remove markdown heading prefixes (e.g., "# ", "## ", "### ")
            heading_match = re.match(r"^#+\s*(.+)$", stripped)
            if heading_match:
                title = heading_match.group(1).strip()
                break
            # Use the first non-empty line as-is
            title = stripped
            break

    if not title:
        print_error(
            console, "Could not parse title from generated content. Please try again."
        )
        sys.exit(1)

    description = "\n".join(description_lines).strip()
    if not description:
        # Use the whole content as description if we couldn't parse it
        description = mr_content

    # Clean the description to remove markdown wrappers and headers
    description = clean_mr_description(description)

    # Handle branch renaming before creating MR
    # Construct new branch name from ticket number and slugified title
    if ticket_number:
        slugified_title = slugify_branch_name(title)
        if slugified_title:
            new_branch_name = f"IOTIL-{ticket_number}-{slugified_title}"
        else:
            new_branch_name = f"IOTIL-{ticket_number}"
    else:
        # Prompt user for ticket number if not detected
        console.print(
            "[yellow]No IOTIL ticket number detected in branch name.[/yellow]"
        )
        try:
            ticket_input = input(
                "Enter IOTIL ticket number (or press Enter to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            sys.exit(0)

        if ticket_input:
            # Validate it's numeric
            if not ticket_input.isdigit():
                print_error(console, "Ticket number must be numeric.")
                sys.exit(1)
            ticket_number = ticket_input
            slugified_title = slugify_branch_name(title)
            if slugified_title:
                new_branch_name = f"IOTIL-{ticket_number}-{slugified_title}"
            else:
                new_branch_name = f"IOTIL-{ticket_number}"
        else:
            new_branch_name = None

    # Offer branch rename if we have a new name and it differs from current
    if new_branch_name and new_branch_name != current_branch:
        console.print(f"\n[bold]Branch Rename:[/bold]")
        console.print(f"  Current: {current_branch}")
        console.print(f"  New:     {new_branch_name}")

        try:
            rename_choice = (
                input("Rename branch? [Y/n]: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            sys.exit(0)

        if rename_choice in ("", "y", "yes"):
            if not rename_and_push_branch(repo, current_branch, new_branch_name, console):
                print_error(
                    console,
                    "Branch rename failed. You can continue with the current branch "
                    "or abort and fix the issue manually.",
                )
                try:
                    continue_choice = (
                        input("Continue with current branch? [y/N]: ").strip().lower()
                    )
                except (EOFError, KeyboardInterrupt):
                    console.print("\nAborted.")
                    sys.exit(0)

                if continue_choice not in ("y", "yes"):
                    sys.exit(1)
            else:
                # Update current_branch for subsequent operations
                current_branch = new_branch_name
        else:
            console.print(
                "[yellow]Skipping branch rename. "
                "Local and remote branch names may differ from MR title.[/yellow]"
            )

    # Execute glab command (branch has been renamed at this point if user confirmed)
    try:
        result = subprocess.run(
            [
                "glab",
                "mr",
                "create",
                "--title",
                title,
                "--description",
                description,
                "--target-branch",
                target_branch,
                "--draft",
                "--remove-source-branch",
            ],
            capture_output=True,
            text=True,
            cwd=repo.working_dir,
        )
        if result.returncode == 0:
            console.print("[green]Merge request created successfully![/green]")
            if result.stdout:
                console.print(result.stdout)
        else:
            print_error(console, f"glab mr create failed: {result.stderr}")
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        print_error(console, f"glab mr create failed: {e}")
        sys.exit(1)


@cli.command()
@click.option(
    "--full",
    is_flag=True,
    help="Run full diagnostics including live API test",
)
@click.option(
    "--export",
    is_flag=True,
    help="Export diagnostic info for sharing (sanitized)",
)
@click.pass_context
def doctor(ctx: click.Context, full: bool, export: bool) -> None:
    """Run diagnostic checks for ACA dependencies.

    Checks:
    - git installation
    - glab installation (for mr-desc)
    - Claude Code CLI installation and version
    - Claude Code CLI authentication
    - claude-agent-sdk version
    - Network connectivity to Anthropic API
    - Configuration file validity
    - Environment variables

    Use --full to also test the actual Claude API with a simple query.
    Use --export to generate a sanitized diagnostic report for sharing.
    """
    plain_text = ctx.obj.get("plain_text", False)
    console = get_console(plain_text)

    console.print("[bold]ACA Diagnostic Report[/bold]\n")

    all_passed = True
    diagnostic_info: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "checks": {},
    }

    def record_check(name: str, passed: bool, details: str) -> None:
        """Record a check result for export."""
        diagnostic_info["checks"][name] = {
            "passed": passed,
            "details": details,
        }

    # Check git
    console.print("Checking git... ", end="")
    if shutil.which("git"):
        try:
            result = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                console.print(f"[green]✓[/green] {version}")
                record_check("git", True, version)
            else:
                console.print("[red]✗ Failed to get version[/red]")
                record_check("git", False, "Failed to get version")
                all_passed = False
        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")
            record_check("git", False, str(e))
            all_passed = False
    else:
        console.print("[red]✗ Not found[/red]")
        record_check("git", False, "Not found")
        all_passed = False

    # Check glab
    console.print("Checking glab... ", end="")
    if shutil.which("glab"):
        try:
            result = subprocess.run(
                ["glab", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                version = result.stdout.strip().split("\n")[0]
                console.print(f"[green]✓[/green] {version}")
                record_check("glab", True, version)
            else:
                console.print("[red]✗ Failed to get version[/red]")
                record_check("glab", False, "Failed to get version")
                all_passed = False
        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")
            record_check("glab", False, str(e))
            all_passed = False
    else:
        console.print("[yellow]✗ Not found (required for mr-desc command)[/yellow]")
        record_check("glab", False, "Not found (optional)")
        # Don't fail all_passed for optional glab

    # Check Claude Code CLI
    console.print("Checking Claude Code CLI... ", end="")
    cli_version = None
    if shutil.which("claude"):
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                cli_version = result.stdout.strip()
                console.print(f"[green]✓[/green] {cli_version}")
                record_check("claude_cli", True, cli_version)
            else:
                console.print("[red]✗ Failed to get version[/red]")
                console.print(
                    "  [yellow]Install from https://claude.ai/download[/yellow]"
                )
                record_check("claude_cli", False, "Failed to get version")
                all_passed = False
        except subprocess.TimeoutExpired:
            console.print("[red]✗ Timed out[/red]")
            record_check("claude_cli", False, "Timed out")
            all_passed = False
        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")
            record_check("claude_cli", False, str(e))
            all_passed = False
    else:
        console.print("[red]✗ Not found[/red]")
        console.print("  [yellow]Install from https://claude.ai/download[/yellow]")
        record_check("claude_cli", False, "Not found")
        all_passed = False

    # Check Claude Code CLI authentication (multi-layered approach)
    console.print("Checking Claude Code CLI auth... ", end="")
    has_api_key = os.environ.get("ANTHROPIC_API_KEY") is not None
    credentials_file = Path.home() / ".claude" / ".credentials.json"
    has_credentials_file = credentials_file.exists()

    if has_api_key:
        console.print("[green]✓[/green] Authenticated (via API key)")
        record_check("authentication", True, "API key")
    elif has_credentials_file:
        # Validate credentials file
        try:
            import json

            with open(credentials_file, "r") as f:
                creds = json.load(f)
            if creds:
                console.print("[green]✓[/green] Authenticated (via credentials file)")
                record_check("authentication", True, "Credentials file")
            else:
                console.print("[yellow]⚠[/yellow] Credentials file exists but appears empty")
                record_check("authentication", False, "Empty credentials file")
                all_passed = False
        except json.JSONDecodeError:
            console.print("[red]✗ Credentials file is corrupted[/red]")
            console.print(
                "  [yellow]Delete ~/.claude/.credentials.json and re-authenticate[/yellow]"
            )
            record_check("authentication", False, "Corrupted credentials file")
            all_passed = False
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Could not validate credentials: {e}")
            record_check("authentication", False, f"Validation error: {e}")
    else:
        console.print("[red]✗ Not authenticated[/red]")
        console.print(
            "  [yellow]Run 'claude' and sign in to authenticate, "
            "or set the ANTHROPIC_API_KEY environment variable.[/yellow]"
        )
        record_check("authentication", False, "Not authenticated")
        all_passed = False

    # Check claude-agent-sdk
    console.print("Checking claude-agent-sdk... ", end="")
    sdk_version = None
    try:
        from importlib.metadata import version

        sdk_version = version("claude-agent-sdk")
        console.print(f"[green]✓[/green] {sdk_version}")
        record_check("claude_agent_sdk", True, sdk_version)
    except Exception:
        console.print("[red]✗ Not found[/red]")
        console.print("  [yellow]Install with: pip install claude-agent-sdk[/yellow]")
        record_check("claude_agent_sdk", False, "Not found")
        all_passed = False

    # Check network connectivity
    console.print("Checking network connectivity... ", end="")
    connected, network_error = check_network_connectivity()
    if connected:
        console.print("[green]✓[/green] api.anthropic.com reachable")
        record_check("network", True, "Connected")
    else:
        console.print(f"[red]✗ {network_error}[/red]")
        console.print("  [yellow]Check your internet connection and firewall settings[/yellow]")
        record_check("network", False, network_error or "Unknown error")
        all_passed = False

    # Check configuration
    console.print("Checking configuration... ", end="")
    config_path = Path.home() / ".config" / "aca" / "config.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                config_data = tomli.load(f)
            console.print(f"[green]✓[/green] Config file found ({config_path})")
            record_check("config_file", True, str(config_path))

            # Show key config values
            config = get_config()
            console.print(f"    Timeout: {config.timeout}s")
            console.print(f"    Retry attempts: {config.retry_attempts}")
            console.print(f"    Log level: {config.log_level}")
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Config file exists but has errors: {e}")
            record_check("config_file", False, str(e))
    else:
        console.print("[blue]ℹ[/blue] No config file (using defaults)")
        record_check("config_file", True, "Using defaults")
        config = get_config()
        console.print(f"    Timeout: {config.timeout}s (default)")
        console.print(f"    Retry attempts: {config.retry_attempts} (default)")

    # Check environment variables
    console.print("\n[bold]Environment Variables:[/bold]")
    env_vars = [
        ("ANTHROPIC_API_KEY", bool(os.environ.get("ANTHROPIC_API_KEY")), "Set" if os.environ.get("ANTHROPIC_API_KEY") else "Not set"),
        ("ACA_TIMEOUT", bool(os.environ.get("ACA_TIMEOUT")), os.environ.get("ACA_TIMEOUT", "Not set")),
        ("ACA_RETRY_ATTEMPTS", bool(os.environ.get("ACA_RETRY_ATTEMPTS")), os.environ.get("ACA_RETRY_ATTEMPTS", "Not set")),
        ("ACA_LOG_LEVEL", bool(os.environ.get("ACA_LOG_LEVEL")), os.environ.get("ACA_LOG_LEVEL", "Not set")),
        ("EDITOR", bool(os.environ.get("EDITOR")), os.environ.get("EDITOR", "Not set")),
        ("VISUAL", bool(os.environ.get("VISUAL")), os.environ.get("VISUAL", "Not set")),
    ]

    for var_name, is_set, display_value in env_vars:
        if var_name == "ANTHROPIC_API_KEY":
            # Don't show the actual API key
            display_value = "Set (hidden)" if is_set else "Not set"
        status = "[green]✓[/green]" if is_set else "[dim]○[/dim]"
        console.print(f"  {status} {var_name}: {display_value}")

    diagnostic_info["environment"] = {
        var_name: ("Set" if is_set else "Not set")
        for var_name, is_set, _ in env_vars
    }

    # Version compatibility info
    if cli_version and sdk_version:
        console.print("\n[bold]Version Information:[/bold]")
        console.print(f"  Claude CLI: {cli_version}")
        console.print(f"  Agent SDK: {sdk_version}")
        console.print(f"  Python: {sys.version.split()[0]}")
        console.print(f"  Platform: {sys.platform}")

        diagnostic_info["versions"] = {
            "claude_cli": cli_version,
            "agent_sdk": sdk_version,
            "python": sys.version.split()[0],
            "platform": sys.platform,
        }

    # Full test with actual API call
    if full and all_passed:
        console.print("\n[bold]Live API Test:[/bold]")
        console.print("Testing Claude API with simple query... ", end="")
        try:
            test_response = asyncio.run(
                generate_with_claude("Reply with exactly: OK", os.getcwd())
            )
            if "OK" in test_response or len(test_response) > 0:
                console.print("[green]✓[/green] API responding correctly")
                record_check("api_test", True, "API responding")
            else:
                console.print("[yellow]⚠[/yellow] Unexpected response")
                record_check("api_test", False, "Unexpected response")
        except Exception as e:
            console.print(f"[red]✗ API test failed: {e}[/red]")
            record_check("api_test", False, str(e))
            all_passed = False

    # Export diagnostic info
    if export:
        console.print("\n[bold]Diagnostic Export:[/bold]")
        export_path = Path.home() / "aca-diagnostics.json"
        try:
            import json

            with open(export_path, "w") as f:
                json.dump(diagnostic_info, f, indent=2)
            console.print(f"[green]✓[/green] Saved to {export_path}")
            console.print("  [yellow]Share this file when reporting issues[/yellow]")
        except Exception as e:
            console.print(f"[red]✗ Failed to export: {e}[/red]")

    # Summary
    console.print()
    if all_passed:
        console.print("[green]All checks passed![/green]")
    else:
        console.print(
            "[yellow]Some checks failed. Review the output above for details.[/yellow]"
        )
        sys.exit(1)


if __name__ == "__main__":
    cli()

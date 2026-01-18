#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "claude-agent-sdk>=0.1.0",
#     "rich>=13.0.0",
#     "GitPython>=3.1.0",
#     "click>=8.0.0",
#     "tomli>=2.0.0",
# ]
# ///
"""
Common utilities for Claude-based CLI tools.

This module contains shared utilities for configuration management, error handling,
retry logic, Claude SDK wrappers, console utilities, network checks, and dependency
validation. It is designed to be imported by CLI tools like aca.py and lx.py.

Main utility categories:

  Configuration:
    - ACAConfig: Dataclass holding all configuration values
    - get_config(): Load/cache configuration from ~/.config/aca/config.toml and env vars
    - setup_logging(): Configure logging based on verbosity and config

  Error Handling:
    - ACAError: Base exception with context, cause, and troubleshooting steps
    - ErrorContext: Structured error context (command, exit_code, stderr, extras)
    - collect_error_context(): Gather system info for debugging
    - Specialized exceptions: ClaudeAuthenticationError, ClaudeNetworkError,
      ClaudeCLIError, ClaudeTimeoutError, ClaudeRateLimitError, ClaudeContentError

  Retry Logic:
    - retry_with_backoff(): Decorator with exponential backoff and jitter
    - RETRYABLE_EXCEPTIONS: Tuple of exceptions that trigger retry

  Dependency Validation:
    - check_claude_cli(): Verify Claude CLI is installed and authenticated
    - check_dependency(): Check if an executable exists in PATH
    - check_network_connectivity(): Test connection to Anthropic API

  Console Utilities:
    - get_console(): Create Rich console (plain or formatted)
    - print_output(): Print text, optionally as Markdown
    - print_error(): Print error messages with consistent formatting

  Claude SDK Integration:
    - generate_with_claude(): Async wrapper with retry and timeout handling
    - generate_with_progress(): Sync wrapper with spinner UI
    - Model selection: Pass model="sonnet", "opus", or "haiku" to select model
    - Default model can be configured via ~/.config/aca/config.toml (default_model)
      or ACA_DEFAULT_MODEL environment variable (defaults to "sonnet")
"""

import asyncio
import functools
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

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
# Configuration Management
# Loads settings from ~/.config/aca/config.toml and environment variables
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
    default_model: str = "sonnet"
    # Diff compression settings
    diff_size_threshold_bytes: int = 50_000  # 50KB
    diff_files_threshold: int = 100
    diff_compression_enabled: bool = True

    @classmethod
    def load(cls) -> ACAConfig:
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
                config.default_model = data.get("default_model", config.default_model)
                # Diff compression settings
                config.diff_size_threshold_bytes = data.get(
                    "diff_size_threshold_bytes", config.diff_size_threshold_bytes
                )
                config.diff_files_threshold = data.get("diff_files_threshold", config.diff_files_threshold)
                config.diff_compression_enabled = data.get("diff_compression_enabled", config.diff_compression_enabled)
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

        if env_model := os.environ.get("ACA_DEFAULT_MODEL"):
            config.default_model = env_model

        # Diff compression environment variable overrides
        if env_size_threshold := os.environ.get("ACA_DIFF_SIZE_THRESHOLD"):
            try:
                config.diff_size_threshold_bytes = int(env_size_threshold)
            except ValueError:
                logger.warning(f"Invalid ACA_DIFF_SIZE_THRESHOLD value: {env_size_threshold}")

        if env_files_threshold := os.environ.get("ACA_DIFF_FILES_THRESHOLD"):
            try:
                config.diff_files_threshold = int(env_files_threshold)
            except ValueError:
                logger.warning(f"Invalid ACA_DIFF_FILES_THRESHOLD value: {env_files_threshold}")

        if env_compression := os.environ.get("ACA_DIFF_COMPRESSION_ENABLED"):
            config.diff_compression_enabled = env_compression.lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

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

    # Configure the root logger so all module loggers inherit the configuration
    root_logger = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s:%(funcName)s:%(lineno)d] %(message)s")
    )
    root_logger.setLevel(level)
    root_logger.addHandler(handler)


# =============================================================================
# Error Handling
# Custom exception hierarchy with context collection and troubleshooting guidance
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


# Tuple of exceptions that can be retried
RETRYABLE_EXCEPTIONS = (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError)


# =============================================================================
# Error Handling and Network Utilities
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
    context.extra["has_credentials_file"] = (Path.home() / ".claude" / ".credentials.json").exists()

    return context


def check_network_connectivity() -> tuple[bool, str | None]:
    """Check network connectivity to Anthropic API.

    Returns:
        Tuple of (is_connected, error_message)
    """
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=5)
        return True, None
    except TimeoutError:
        return False, "Connection timed out"
    except socket.gaierror:
        return False, "DNS resolution failed"
    except OSError as e:
        return False, str(e)


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


# =============================================================================
# Retry Logic
# Exponential backoff with jitter for transient failures
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
                            f"Attempt {attempt + 1}/{_max_attempts} failed: {e}. Retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"All {_max_attempts} attempts failed. Last error: {e}")
                except (ClaudeAuthenticationError, ClaudeCLIError, ClaudeContentError):
                    # Don't retry these errors
                    raise

            # Re-raise the last exception if all retries failed
            if last_exception:
                raise last_exception

        return wrapper  # type: ignore

    return decorator


# =============================================================================
# Dependency Validation
# Check for required executables and authentication
# =============================================================================


def check_dependency(executable: str, console: Console) -> bool:
    """Check if an executable exists in PATH."""
    if shutil.which(executable) is None:
        console.print(
            f"[red]Error: '{executable}' not found. Please install {executable} and ensure it's in your PATH.[/red]"
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
            "[red]Error: Claude Code CLI not found.[/red]\n[yellow]Install it from https://claude.ai/download[/yellow]"
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
            "[red]Error: Claude Code CLI not found.[/red]\n[yellow]Install it from https://claude.ai/download[/yellow]"
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


def get_precommit_skip_env() -> dict[str, str]:
    """Return environment overrides to skip all pre-commit hooks.

    Uses SKIP_PRECOMMIT as a single toggle. When set to a truthy value, this
    reads .pre-commit-config.yaml to build a comma-separated SKIP list that
    pre-commit understands. If parsing fails, falls back to known hook IDs.
    """

    def is_truthy_env(value: str | None) -> bool:
        if value is None:
            return False
        normalized = value.strip().lower()
        if not normalized:
            return False
        return normalized not in {"0", "false", "no", "off", "n"}

    if not is_truthy_env(os.environ.get("SKIP_PRECOMMIT")):
        return {}

    fallback_hook_ids = [
        "ruff-format",
        "ruff",
        "shfmt",
        "shellcheck",
        "chezmoi-verify",
        "validate-python-version",
    ]

    def find_repo_root() -> Path | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                root = result.stdout.strip()
                if root:
                    return Path(root)
        except Exception:
            pass

        cwd = Path.cwd()
        for parent in (cwd, *cwd.parents):
            if (parent / ".pre-commit-config.yaml").exists():
                return parent
        return None

    def parse_hook_ids(config_text: str) -> list[str]:
        hook_ids: list[str] = []
        seen: set[str] = set()
        for line in config_text.splitlines():
            match = re.match(r"^\s*-\s*id:\s*([^\s#]+)", line)
            if not match:
                continue
            hook_id = match.group(1).strip().strip("\"'")
            if hook_id and hook_id not in seen:
                seen.add(hook_id)
                hook_ids.append(hook_id)
        return hook_ids

    repo_root = find_repo_root()
    if not repo_root:
        return {"SKIP": ",".join(fallback_hook_ids)}

    config_path = repo_root / ".pre-commit-config.yaml"
    try:
        with open(config_path, encoding="utf-8") as f:
            config_text = f.read()
        hook_ids = parse_hook_ids(config_text)
    except Exception:
        hook_ids = []

    if not hook_ids:
        hook_ids = fallback_hook_ids

    return {"SKIP": ",".join(hook_ids)}


# =============================================================================
# Console Utilities
# Rich formatting and output helpers
# =============================================================================


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


# =============================================================================
# Claude SDK Integration
# Async wrappers with timeout and error handling
# =============================================================================


async def _generate_with_claude_impl(
    prompt: str, cwd: str, timeout: int | None = None, model: str | None = None
) -> str:
    """Internal implementation of generate_with_claude without retry.

    Args:
        prompt: The prompt to send to Claude.
        cwd: The current working directory for the agent.
        timeout: Timeout in seconds for the operation.
        model: Model alias to use ("sonnet", "opus", "haiku"). If None, uses config default.

    Returns:
        The accumulated text content from the response.

    Raises:
        ACAError subclasses for specific error types.
    """
    config = get_config()
    _timeout = timeout if timeout is not None else config.timeout
    _model = model if model is not None else config.default_model

    options = ClaudeAgentOptions(permission_mode="acceptEdits", cwd=cwd, model=_model)
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
    except TimeoutError as e:
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
async def generate_with_claude(prompt: str, cwd: str, timeout: int | None = None, model: str | None = None) -> str:
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
        model: Model alias to use ("sonnet", "opus", "haiku"). If None, uses
               config default (from ~/.config/aca/config.toml or ACA_DEFAULT_MODEL).

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
    return await _generate_with_claude_impl(prompt, cwd, timeout, model)


def generate_with_progress(
    console: Console,
    prompt: str,
    cwd: str,
    message: str = "Generating...",
    model: str | None = None,
) -> str:
    """Generate content with Claude showing a progress spinner.

    Args:
        console: Rich console for output
        prompt: The prompt to send to Claude
        cwd: Current working directory
        message: Message to display during generation
        model: Model alias to use ("sonnet", "opus", "haiku"). If None, uses
               config default (from ~/.config/aca/config.toml or ACA_DEFAULT_MODEL).

    Returns:
        Generated content from Claude

    Raises:
        ACAError subclasses for specific error types
    """
    # Don't show spinner if plain text mode
    if console.no_color:
        console.print(message)
        return asyncio.run(generate_with_claude(prompt, cwd, model=model))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description=message, total=None)
        return asyncio.run(generate_with_claude(prompt, cwd, model=model))

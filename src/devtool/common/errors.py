"""Error hierarchy, classification, retry logic, and network utilities."""

import asyncio
import functools
import logging
import os
import random
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class ErrorContext:
    """Context information for debugging errors."""

    command: str | None = None
    exit_code: int | None = None
    stderr: str | None = None
    stdout: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    extra: dict[str, Any] = field(default_factory=dict)


class DevtoolError(Exception):
    """Base exception for all devtool errors."""

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
            stderr = self.context.stderr[:500]
            if len(self.context.stderr) > 500:
                stderr += "..."
            debug_items.append(f"Error Output: {stderr}")
        debug_items.append(f"Timestamp: {self.context.timestamp}")

        # Add extra context items
        sensitive_keys = {"api_key", "token", "secret", "password", "credential"}
        for key, value in self.context.extra.items():
            key_lower = key.lower()
            if any(s in key_lower for s in sensitive_keys):
                if value:
                    debug_items.append(f"{key}: [MASKED]")
                continue
            if isinstance(value, bool):
                debug_items.append(f"{key}: {value}")
            elif value is None:
                continue
            elif isinstance(value, str):
                if len(value) > 500:
                    debug_items.append(f"{key}: {value[:500]}...")
                else:
                    debug_items.append(f"{key}: {value}")
            elif isinstance(value, int | float):
                debug_items.append(f"{key}: {value}")
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


class ClaudeAuthenticationError(DevtoolError):
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


class ClaudeNetworkError(DevtoolError):
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


class ClaudeCLIError(DevtoolError):
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
        ]
        super().__init__(message, cause, context, troubleshooting)


class ClaudeTimeoutError(DevtoolError):
    """Raised when Claude operation times out."""

    def __init__(
        self,
        message: str = "Claude operation timed out",
        cause: Exception | None = None,
        context: ErrorContext | None = None,
        timeout_seconds: int | None = None,
    ):
        troubleshooting = [
            f"Increase timeout (current: {timeout_seconds or 'unknown'}s) via DT_TIMEOUT env var",
            "Check system resources (CPU/memory usage)",
            "Verify Claude service status at status.anthropic.com",
            "Try again with a simpler prompt",
        ]
        if timeout_seconds:
            context = context or ErrorContext()
            context.extra["timeout_seconds"] = timeout_seconds
        super().__init__(message, cause, context, troubleshooting)


class ClaudeRateLimitError(DevtoolError):
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


class ClaudeContentError(DevtoolError):
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
            "Run 'devtool doctor' to verify system configuration",
            "Report issue if this persists: https://github.com/anthropics/claude-code/issues",
        ]
        super().__init__(message, cause, context, troubleshooting)


# Tuple of exceptions that can be retried
RETRYABLE_EXCEPTIONS = (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError)


def collect_error_context(
    command: str | None = None,
    exit_code: int | None = None,
    stderr: str | None = None,
    stdout: str | None = None,
) -> ErrorContext:
    """Collect system and environment context for error debugging."""
    context = ErrorContext(
        command=command,
        exit_code=exit_code,
        stderr=stderr,
        stdout=stdout,
    )

    context.extra["python_version"] = sys.version
    context.extra["platform"] = sys.platform

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

    context.extra["has_api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    context.extra["has_credentials_file"] = (Path.home() / ".claude" / ".credentials.json").exists()

    return context


def check_network_connectivity() -> tuple[bool, str | None]:
    """Check network connectivity to Anthropic API."""
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=5)
        return True, None
    except TimeoutError:
        return False, "Connection timed out"
    except socket.gaierror:
        return False, "DNS resolution failed"
    except OSError as e:
        return False, str(e)


def _classify_error(e: Exception) -> DevtoolError:
    """Classify an exception into the appropriate DevtoolError type."""
    from devtool.common.config import get_config

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
    if isinstance(e, ConnectionError | socket.error):
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
        retry_after = None
        match = re.search(r"retry.?after[:\s]*(\d+)", error_str)
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
        match = re.search(r"exit code[:\s]*(\d+)", error_str)
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


def retry_with_backoff(
    max_attempts: int | None = None,
    initial_delay: float | None = None,
    backoff_factor: float | None = None,
    max_delay: float | None = None,
) -> Callable[[F], F]:
    """Decorator to retry async functions with exponential backoff."""
    from devtool.common.config import get_config

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
                except ClaudeAuthenticationError, ClaudeCLIError, ClaudeContentError:
                    # Don't retry these errors
                    raise

            if last_exception:
                raise last_exception

        return wrapper  # type: ignore[return-value]

    return decorator

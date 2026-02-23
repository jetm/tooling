"""Claude CLI subprocess integration — generation, file-based prompts, progress."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from rich.progress import Progress, SpinnerColumn, TextColumn

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console

    from devtool.common.config import ACAConfig

logger = logging.getLogger(__name__)


async def _generate_with_claude_impl(
    prompt: str,
    cwd: str,
    timeout: int | None = None,
    model: str | None = None,
    skip_file_based_delivery: bool = False,
    section_marker: str | None = None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    tools: list[str] | None = None,
) -> str:
    """Internal implementation of generate_with_claude without retry."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        TextBlock,
        query,
    )
    from claude_agent_sdk._errors import MessageParseError
    from claude_agent_sdk._internal import client as _sdk_client
    from claude_agent_sdk._internal.message_parser import parse_message as _original_parse

    from devtool.common.config import get_config
    from devtool.common.errors import (
        ClaudeContentError,
        ClaudeTimeoutError,
        _classify_error,
        collect_error_context,
    )

    # Monkey-patch SDK to tolerate unknown message types (e.g. rate_limit_event)
    # instead of raising MessageParseError which kills the async generator.
    # TODO: Remove when upstream handles unknown types gracefully
    # (see message_parser.py line 168: raise MessageParseError on unknown type).
    def _lenient_parse_message(data: dict) -> object:
        try:
            return _original_parse(data)
        except MessageParseError:
            msg_type = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
            logger.debug(f"Skipping unrecognized SDK message type: {msg_type}")
            return SystemMessage(subtype="unknown", data=data or {})

    _sdk_client.parse_message = _lenient_parse_message

    config = get_config()
    _timeout = timeout if timeout is not None else config.timeout
    _model = model if model is not None else config.default_model

    # Check if file-based prompt delivery is needed for large prompts
    temp_file_path: str | None = None
    actual_prompt = prompt

    if not skip_file_based_delivery and should_use_file_based_prompt(prompt, config):
        prompt_size_kb = len(prompt.encode("utf-8")) / 1024
        logger.info(f"Prompt size ({prompt_size_kb:.1f} KB) exceeds threshold, attempting file-based delivery")
        file_result = create_file_based_prompt(prompt, section_marker=section_marker, target_dir=cwd)
        if file_result is not None:
            actual_prompt, temp_file_path = file_result
            modified_size_kb = len(actual_prompt.encode("utf-8")) / 1024
            logger.info(
                f"Using file-based prompt delivery: {prompt_size_kb:.1f} KB -> "
                f"{modified_size_kb:.1f} KB (content written to {temp_file_path})"
            )
        else:
            logger.warning("File-based prompt creation failed, using original prompt")

    opts: dict[str, object] = {"cwd": cwd, "model": _model}
    if system_prompt is not None:
        opts["system_prompt"] = system_prompt
    else:
        opts["permission_mode"] = "acceptEdits"
    if max_turns is not None:
        opts["max_turns"] = max_turns
    if effort is not None:
        opts["effort"] = effort
    if tools is not None:
        opts["tools"] = tools
    options = ClaudeAgentOptions(**opts)
    accumulated_text = ""
    result_message: ResultMessage | None = None

    logger.debug(f"Starting Claude query with timeout={_timeout}s")

    async def collect_response() -> None:
        nonlocal accumulated_text, result_message
        async for message in query(prompt=actual_prompt, options=options):
            if isinstance(message, SystemMessage):
                continue
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        accumulated_text += block.text
            elif isinstance(message, ResultMessage):
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
    finally:
        if temp_file_path is not None:
            cleanup_temp_prompt_file(temp_file_path)

    # Use ResultMessage.result if available and accumulated text is empty
    if result_message is not None:
        if not accumulated_text.strip() and result_message.result:
            logger.debug("Using ResultMessage.result as response")
            return result_message.result.strip()

    if not accumulated_text.strip():
        raise ClaudeContentError(
            message="Claude returned an empty response",
            context=collect_error_context(),
        )

    logger.debug(f"Claude query completed, response length: {len(accumulated_text)}")
    return accumulated_text.strip()


async def generate_with_claude(
    prompt: str,
    cwd: str,
    timeout: int | None = None,
    model: str | None = None,
    skip_file_based_delivery: bool = False,
    section_marker: str | None = None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    tools: list[str] | None = None,
) -> str:
    """Call Claude Agent SDK to generate content.

    Includes automatic retry with exponential backoff for transient errors.
    """
    from devtool.common.errors import retry_with_backoff

    @retry_with_backoff()
    async def _inner() -> str:
        return await _generate_with_claude_impl(
            prompt,
            cwd,
            timeout,
            model,
            skip_file_based_delivery,
            section_marker,
            system_prompt=system_prompt,
            max_turns=max_turns,
            effort=effort,
            tools=tools,
        )

    return await _inner()


def generate_with_progress(
    console: Console,
    prompt: str,
    cwd: str,
    message: str = "Generating...",
    model: str | None = None,
    skip_file_based_delivery: bool = False,
    section_marker: str | None = None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    tools: list[str] | None = None,
) -> str:
    """Generate content with Claude showing a progress spinner."""
    sdk_kwargs = {
        "model": model,
        "skip_file_based_delivery": skip_file_based_delivery,
        "section_marker": section_marker,
        "system_prompt": system_prompt,
        "max_turns": max_turns,
        "effort": effort,
        "tools": tools,
    }

    if console.no_color:
        console.print(message)
        return asyncio.run(generate_with_claude(prompt, cwd, **sdk_kwargs))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description=message, total=None)
        return asyncio.run(generate_with_claude(prompt, cwd, **sdk_kwargs))


def generate_with_retry(
    console: Console,
    prompt: str,
    cwd: str,
    fallback_template: str,
    operation: str,
    *,
    model: str | None = None,
    cleanup_fn: Callable[[], None] | None = None,
    skip_file_based_delivery: bool = False,
    section_marker: str | None = None,
    post_process_fn: Callable[[str], str | None] | None = None,
    edit_suffix: str = ".md",
    max_attempts: int = 3,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    tools: list[str] | None = None,
) -> str:
    """Generate content with retry, error handling, and fallback template support.

    Returns the generated (and optionally post-processed) content, or exits on failure.
    """
    import sys

    from devtool.common.console import print_error
    from devtool.common.errors import (
        ClaudeAuthenticationError,
        ClaudeCLIError,
        ClaudeContentError,
        ClaudeNetworkError,
        ClaudeRateLimitError,
        ClaudeTimeoutError,
    )
    from devtool.common.git import edit_in_editor, handle_generation_error

    result_content: str | None = None

    for attempt in range(max_attempts):
        try:
            raw = generate_with_progress(
                console,
                prompt,
                cwd,
                message=f"Generating {operation}...",
                model=model,
                skip_file_based_delivery=skip_file_based_delivery,
                section_marker=section_marker,
                system_prompt=system_prompt,
                max_turns=max_turns,
                effort=effort,
                tools=tools,
            )
            if post_process_fn is not None:
                result_content = post_process_fn(raw)
            else:
                result_content = raw
            break

        except ClaudeAuthenticationError as e:
            logger.error(f"Authentication error: {e}")
            if console.no_color:
                console.print(e.format_error())
            else:
                console.print(f"[red]{e.format_error()}[/red]")
            if cleanup_fn:
                cleanup_fn()
            sys.exit(1)

        except (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError) as e:
            logger.warning(f"Transient error (attempt {attempt + 1}): {e}")
            fallback = handle_generation_error(console, e, fallback_content=fallback_template, operation=operation)
            if fallback is not None:
                result_content = edit_in_editor(fallback, console, edit_suffix)
                break

        except (ClaudeCLIError, ClaudeContentError) as e:
            logger.error(f"Non-recoverable error: {e}")
            fallback = handle_generation_error(console, e, fallback_content=fallback_template, operation=operation)
            if fallback is not None:
                result_content = edit_in_editor(fallback, console, edit_suffix)
                break

        except Exception as e:
            logger.exception(f"Unexpected error during generation: {e}")
            fallback = handle_generation_error(console, e, fallback_content=fallback_template, operation=operation)
            if fallback is not None:
                result_content = edit_in_editor(fallback, console, edit_suffix)
                break

    if cleanup_fn:
        cleanup_fn()

    if not result_content:
        print_error(console, f"Failed to generate {operation}")
        console.print("[yellow]Tip: Run 'devtool doctor' to check your configuration.[/yellow]")
        sys.exit(1)

    return result_content


# ---- File-based prompt delivery ----


def should_use_file_based_prompt(prompt: str, config: ACAConfig) -> bool:
    """Determine if file-based prompt delivery should be used."""
    if not config.prompt_file_enabled:
        return False

    prompt_size = len(prompt.encode("utf-8"))
    threshold = config.prompt_file_threshold_bytes

    if prompt_size > threshold:
        logger.debug(
            f"Prompt size ({prompt_size} bytes) exceeds threshold ({threshold} bytes), will use file-based delivery"
        )
        return True

    return False


def write_prompt_to_tempfile(content: str, prefix: str = "aca_prompt_", target_dir: str | None = None) -> str:
    """Write prompt content to a temporary file."""
    try:
        temp_dir = None
        if target_dir:
            aca_tmp_dir = Path(target_dir) / ".aca" / "tmp"
            try:
                aca_tmp_dir.mkdir(parents=True, exist_ok=True)
                temp_dir = str(aca_tmp_dir)
                logger.debug(f"Using repo-local temp directory: {temp_dir}")
            except OSError as e:
                logger.warning(f"Failed to create repo temp directory {aca_tmp_dir}: {e}, using system temp")
                temp_dir = None

        fd = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w",
            suffix=".md",
            prefix=prefix,
            dir=temp_dir,
            delete=False,
            encoding="utf-8",
        )
        fd.write(content)
        fd.close()
        logger.debug(f"Wrote prompt content to temp file: {fd.name} ({len(content)} bytes)")
        return fd.name
    except OSError as e:
        logger.error(f"Failed to write prompt to temp file: {e}")
        raise


def cleanup_temp_prompt_file(file_path: str | None) -> None:
    """Safely delete a temporary prompt file."""
    if file_path is None:
        return

    try:
        os.unlink(file_path)
        logger.debug(f"Cleaned up temp prompt file: {file_path}")
    except OSError as e:
        logger.warning(f"Failed to clean up temp prompt file {file_path}: {e}")


def create_file_based_prompt(
    original_prompt: str,
    section_marker: str | None = None,
    target_dir: str | None = None,
) -> tuple[str, str] | None:
    """Create a file-based prompt by extracting content and writing to a temp file."""
    default_marker = "## Staged Changes Diff"
    marker = section_marker if section_marker is not None else default_marker

    if marker in original_prompt:
        parts = original_prompt.split(marker, 1)
        header = parts[0]

        if len(parts) > 1:
            rest = parts[1]
            newline_idx = rest.find("\n")
            if newline_idx != -1:
                format_note = rest[:newline_idx]
                section_content = rest[newline_idx + 1 :]
            else:
                format_note = ""
                section_content = ""
        else:
            format_note = ""
            section_content = ""

        if not section_content.strip():
            logger.warning("Section content is empty, skipping file-based delivery")
            return None

        temp_file_path = write_prompt_to_tempfile(section_content.strip(), prefix="aca_content_", target_dir=target_dir)

        modified_prompt = f"""{header}{marker}{format_note}

**NOTE**: The content is too large to include directly in this prompt.
Please read the content from the following file:

**File path**: `{temp_file_path}`

After reading the file, analyze the content and follow the instructions above.
"""

        logger.debug(
            f"Created file-based prompt with marker '{marker}': "
            f"original={len(original_prompt)} bytes, "
            f"modified={len(modified_prompt)} bytes, content_file={temp_file_path}"
        )

        return modified_prompt, temp_file_path

    # Fallback: no marker found — write entire prompt to file
    logger.info(f"Marker '{marker}' not found in prompt, falling back to full prompt file-based delivery")

    temp_file_path = write_prompt_to_tempfile(original_prompt, prefix="aca_full_prompt_", target_dir=target_dir)

    modified_prompt = f"""Please read and follow the instructions in the file below.

**File path**: `{temp_file_path}`

Read the file contents carefully and execute the task described within.
"""

    logger.debug(
        f"Created file-based prompt (full fallback): "
        f"original={len(original_prompt)} bytes, "
        f"modified={len(modified_prompt)} bytes, prompt_file={temp_file_path}"
    )

    return modified_prompt, temp_file_path

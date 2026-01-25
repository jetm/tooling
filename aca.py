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
ACA - AI Commit Assistant

A CLI tool for generating commit messages and merge request descriptions
using the Claude Agent SDK.

Generated content can be edited using your $EDITOR before committing or
creating MRs. Editor fallback chain: $EDITOR -> $VISUAL -> nano -> vi.

Diff Size Analysis
------------------
Large diffs are automatically detected and a warning is displayed when
compression will be applied. This helps manage prompt size for better
AI performance.

Configuration options in ~/.config/aca/config.toml:

    # Diff compression settings
    diff_size_threshold_bytes = 50000  # 50KB - threshold to trigger compression
    diff_files_threshold = 100         # File count threshold
    diff_compression_enabled = true    # Enable/disable compression warnings
    diff_compression_strategy = "smart"  # Options: stat, compact, filtered, function-context, smart

    # Smart compression settings (used when strategy is "smart")
    diff_max_priority_files = 15       # Max files with full diff (1-50)
    diff_token_limit = 100000          # Max characters for compressed output
    diff_smart_priority_enabled = true # Enable smart file prioritization

Environment variable overrides:
    ACA_DIFF_SIZE_THRESHOLD           - Override size threshold (bytes)
    ACA_DIFF_FILES_THRESHOLD          - Override file count threshold
    ACA_DIFF_COMPRESSION_ENABLED      - Enable/disable (1/true/yes/on)
    ACA_DIFF_COMPRESSION_STRATEGY     - Strategy (stat/compact/filtered/function-context/smart)
    ACA_DIFF_MAX_PRIORITY_FILES       - Max priority files for smart compression
    ACA_DIFF_TOKEN_LIMIT              - Token limit for smart compression
    ACA_DIFF_SMART_PRIORITY_ENABLED   - Enable smart prioritization (1/true/yes/on)

Debugging flags (for commit command):
    --no-compress     Disable compression for this commit (overrides config/env)
    --show-prompt     Display the full prompt before sending to Claude

Troubleshooting:
    - Use `aca commit --show-prompt` to inspect the exact prompt sent to Claude
    - Use `aca commit --no-compress` to test without compression
    - Use `aca doctor` to validate compression configuration and test compression
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import click
import git
import tomli
from rich.console import Console

from common_utils import (
    RETRYABLE_EXCEPTIONS,
    ACAConfig,
    ACAError,
    ClaudeAuthenticationError,
    ClaudeCLIError,
    ClaudeContentError,
    ClaudeNetworkError,
    ClaudeRateLimitError,
    ClaudeTimeoutError,
    check_claude_cli,
    check_dependency,
    check_network_connectivity,
    check_version_compatibility,
    cleanup_temp_prompt_file,
    create_file_based_prompt,
    generate_with_claude,
    generate_with_progress,
    get_config,
    get_console,
    get_precommit_skip_env,
    print_error,
    print_output,
    setup_logging,
    should_use_file_based_prompt,
)

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# Git Utilities
# =============================================================================


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
                f"[yellow]Warning:[/yellow] {source_name}={editor_cmd!r} not found or invalid, trying next option..."
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


def rename_and_push_branch(repo: git.Repo, old_name: str, new_name: str, console: Console) -> bool:
    """Rename a branch locally and update the remote.

    The new branch is pushed with --set-upstream to ensure proper remote tracking.

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
            confirm = input(f"Branch '{new_name}' already exists locally. Overwrite? [y/N]: ").strip().lower()
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


def validate_branch_ready_for_mr(repo: git.Repo, branch_name: str, console: Console) -> bool:
    """Validate that a branch is ready for MR creation.

    Checks for uncommitted changes and unpushed commits, prompting user to resolve
    issues before proceeding with MR creation.

    Args:
        repo: GitPython Repo object
        branch_name: Name of the branch to validate
        console: Rich console for output

    Returns:
        True if validation passes or user resolves issues, False if user aborts
    """
    # Check for uncommitted changes (modified tracked files)
    has_uncommitted = repo.is_dirty(untracked_files=False)

    # Check for staged but uncommitted changes
    staged_changes = list(repo.index.diff("HEAD"))

    # Check for untracked files
    untracked_files = repo.untracked_files

    if has_uncommitted or staged_changes or untracked_files:
        console.print("\n[yellow]You have uncommitted changes. MR should include all changes.[/yellow]")
        console.print("\n[bold]Status:[/bold]")
        status_output = repo.git.status("--short")
        console.print(status_output)

        try:
            choice = input("\nOptions: (c)ommit first, (a)bort, (i)gnore and continue: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            return False

        if choice in ("c", "commit"):
            console.print(
                "\n[cyan]Please commit your changes first using 'aca commit' or 'git commit', "
                "then run 'aca mr-desc' again.[/cyan]"
            )
            return False
        elif choice in ("a", "abort"):
            console.print("\nAborted.")
            return False
        elif choice not in ("i", "ignore"):
            console.print("[red]Invalid choice. Aborting.[/red]")
            return False
        # User chose ignore, continue with validation

    # Check for unpushed commits
    try:
        unpushed_count = repo.git.rev_list(f"origin/{branch_name}..{branch_name}", "--count")
        unpushed_count = int(unpushed_count.strip())
    except git.exc.GitCommandError:
        # Remote branch might not exist yet, which is fine
        unpushed_count = 0

    if unpushed_count > 0:
        console.print(f"\n[yellow]You have {unpushed_count} unpushed commit(s). MR requires pushed commits.[/yellow]")
        console.print("\n[bold]Unpushed commits:[/bold]")
        log_output = repo.git.log(f"origin/{branch_name}..{branch_name}", "--oneline")
        console.print(log_output)

        try:
            choice = input("\nOptions: (p)ush now, (a)bort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            return False

        if choice in ("p", "push"):
            console.print("\nPushing commits to origin...")
            try:
                repo.git.push("origin", branch_name)
                console.print(f"[green]Successfully pushed {unpushed_count} commit(s) to origin[/green]")
            except git.exc.GitCommandError as e:
                print_error(console, f"Failed to push: {e}")
                return False
        elif choice in ("a", "abort"):
            console.print("\nAborted.")
            return False
        else:
            console.print("[red]Invalid choice. Aborting.[/red]")
            return False

    return True


def get_target_branch_from_config(repo: git.Repo) -> str | None:
    """Get the target branch for MR from git config.

    Reads the branch-switch.name config value, which is set by git-switch-main.py
    when detecting/caching the main branch.
    """
    try:
        main_branch = repo.config_reader().get_value("branch-switch", "name")
        if main_branch:
            return str(main_branch)
    except Exception:
        pass

    return None


def strip_markdown_code_blocks(text: str) -> str:
    """Remove markdown code block wrappers from text.

    Handles cases where Claude wraps responses in code blocks like:
    ```
    content
    ```
    or
    ```markdown
    content
    ```
    or with ellipsis patterns:
    ```
    ...
    content
    ...
    ```
    """
    # Known language identifiers that can appear as the first content line
    # when the opening fence is exactly ``` (no language on the same line)
    KNOWN_LANG_IDENTIFIERS = {"markdown", "commit", "text", "txt"}

    lines = text.strip().split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1] == "```":
        opening_fence = lines[0].strip()
        # Extract content between ``` markers
        content_lines = lines[1:-1]

        # Only skip the first content line as a language identifier when:
        # 1. The opening fence is exactly ``` (no language attached), AND
        # 2. The first content line is a known language identifier
        if opening_fence == "```" and content_lines and content_lines[0].strip().lower() in KNOWN_LANG_IDENTIFIERS:
            content_lines = content_lines[1:]

        # Strip leading ... patterns
        while content_lines and content_lines[0].strip() in ("...", ""):
            content_lines = content_lines[1:]

        # Strip trailing ... patterns
        while content_lines and content_lines[-1].strip() in ("...", ""):
            content_lines = content_lines[:-1]

        return "\n".join(content_lines).strip()
    return text


def clean_mr_description(description: str) -> str:
    """Clean merge request description by removing markdown wrappers and headers.

    Handles:
    - Markdown code block wrappers (```markdown ... ```)
    - Standalone wrapper artifact lines (markdown, ...) outside code fences
    - Leading "# Description" or "## Description" headers (case-insensitive)
    - Leading empty lines
    """
    # Wrapper artifact patterns that should be trimmed from leading/trailing lines
    WRAPPER_ARTIFACTS = {"markdown", "...", ""}

    # First strip markdown code blocks
    cleaned = strip_markdown_code_blocks(description)

    # Split into lines and process
    lines = cleaned.split("\n")

    # Trim leading wrapper artifacts (markdown, ..., empty lines)
    while lines and lines[0].strip().lower() in WRAPPER_ARTIFACTS:
        lines = lines[1:]

    # Trim trailing wrapper artifacts (markdown, ..., empty lines)
    while lines and lines[-1].strip().lower() in WRAPPER_ARTIFACTS:
        lines = lines[:-1]

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


def clean_mr_output(content: str) -> str:
    """Clean full MR output by removing code block wrappers around sections.

    Handles LLM output like:
    **Title:**
    ```
    [IOTIL-123] Title content
    ```

    Or with markdown headers:
    ## Title
    ```
    [IOTIL-123] Title content
    ```

    **Description:**
    ```markdown
    ## Problem
    Description content
    ```

    Returns the content with code blocks removed, keeping section structure.
    """
    # Pattern to match section headers followed by code blocks
    # Matches various header formats:
    # - **Label:** or *Label:* or Label:
    # - ## Label or # Label or ## Label: (markdown headers with optional colon)
    # Anchored to start of line to avoid matching subheadings inside description body
    pattern = re.compile(
        r"^\s*"  # Anchor to start of line with optional leading whitespace
        r"((?:\*{0,2}(?:Title|Description):\*{0,2})|(?:#{1,2}\s*(?:Title|Description):?))"  # Section header
        r"\s*\n"  # Newline after header
        r"```[a-zA-Z]*\n"  # Opening fence with optional language
        r"(.*?)"  # Content (non-greedy)
        r"\n```",  # Closing fence
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )

    def replace_section(match: re.Match[str]) -> str:
        header = match.group(1)
        content = match.group(2).strip()
        return f"{header}\n{content}"

    return pattern.sub(replace_section, content)


# =============================================================================
# Fallback Templates
# =============================================================================


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


def run_precommit_hooks(repo: git.Repo, console: Console, staged_files: list[str]) -> tuple[bool, list[str]]:
    """Run pre-commit hooks on staged files.

    Honors SKIP_PRECOMMIT by translating it to pre-commit's native SKIP env var,
    dynamically derived from .pre-commit-config.yaml hook IDs.

    Args:
        repo: Git repository object
        console: Rich console for output
        staged_files: List of staged file paths

    Returns:
        Tuple of (success: bool, modified_files: list[str])
        - success: True if hooks pass or are skipped, False if hooks fail
        - modified_files: List of files modified by hooks (empty if none modified)
    """
    # Check if pre-commit is installed
    if not shutil.which("pre-commit"):
        console.print("[dim]pre-commit not found, skipping hook validation[/dim]")
        return True, []

    # Check if .pre-commit-config.yaml exists in the repo
    config_path = Path(repo.working_dir) / ".pre-commit-config.yaml"
    if not config_path.is_file():
        console.print("[dim]No .pre-commit-config.yaml found, skipping hook validation[/dim]")
        return True, []

    # No files to check
    if not staged_files:
        return True, []

    skip_env = get_precommit_skip_env()
    if skip_env:
        console.print("[yellow]⚠ Skipping pre-commit hooks (SKIP_PRECOMMIT is set)[/yellow]")
        return True, []

    # Capture unstaged state BEFORE running hooks so we can detect new changes
    try:
        pre_hook_unstaged_output = repo.git.diff("--name-only")
        pre_hook_unstaged = {f for f in pre_hook_unstaged_output.split("\n") if f.strip()}
    except Exception:
        pre_hook_unstaged = set()

    # Run pre-commit on staged files
    cmd = ["pre-commit", "run", "--files"] + staged_files
    env = os.environ.copy()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=repo.working_dir,
        env=env,
    )

    # Check which files were modified by hooks
    # We detect NEW unstaged changes (files that weren't unstaged before but are now)
    # scoped to the files we asked pre-commit to run on.
    modified_files: list[str] = []
    try:
        post_hook_unstaged_output = repo.git.diff("--name-only")
        post_hook_unstaged = {f for f in post_hook_unstaged_output.split("\n") if f.strip()}

        # Only flag files that have NEW unstaged changes and were in our staged set
        new_unstaged = post_hook_unstaged - pre_hook_unstaged
        staged_set = set(staged_files)
        modified_files = sorted(new_unstaged & staged_set)
    except Exception:
        pass

    if result.returncode == 0:
        return True, modified_files

    # Display hook output on failure
    console.print("\n[red bold]Pre-commit hooks failed:[/red bold]\n")
    if result.stdout:
        console.print(result.stdout)
    if result.stderr:
        console.print(f"[red]{result.stderr}[/red]")

    return False, modified_files


# =============================================================================
# Diff Compression Module
# =============================================================================
#
# This module provides intelligent diff compression for large commits to ensure
# the Claude prompt stays within reasonable size limits while preserving the
# most important context for commit message generation.
#
# ## Compression Flow in commit()
#
# 1. **Diff Size Check**: calculate_diff_size() computes byte/line/file counts
# 2. **Threshold Evaluation**: should_compress_diff() checks against config thresholds
#    - Default: 50KB or 100 files triggers compression
# 3. **Strategy Selection**: config.diff_compression_strategy determines approach
# 4. **Compression Application**: apply_compression_strategy() transforms the diff
# 5. **Validation**: Compressed output is validated for completeness
# 6. **Fallback**: On any failure, original diff is used with appropriate logging
#
# ## Available Strategies
#
# - **stat**: Maximum compression. Shows only file-level statistics (--stat).
#   Use when: Diff is very large, commit message only needs high-level overview
#
# - **compact**: Moderate compression. Reduces context to 1 line (-U1).
#   Use when: Need to see actual changes but context is less important
#
# - **filtered**: Excludes generated/binary files via pattern matching.
#   Use when: Many non-essential files are inflating the diff
#
# - **function-context**: Shows complete functions where changes occurred.
#   Use when: Understanding function-level context is important
#
# - **smart** (default): Hybrid approach. Full diff for priority source files
#   (10-15 files), statistical summary for remaining files.
#   Use when: Balance between detail and size is needed
#
# ## Configuration Options
#
# Config file (~/.config/aca/config.toml):
#   diff_compression_enabled = true         # Enable/disable compression
#   diff_compression_strategy = "smart"     # Strategy name
#   diff_size_threshold_bytes = 51200       # 50KB threshold
#   diff_files_threshold = 100              # File count threshold
#   diff_token_limit = 100000               # Character limit for smart strategy
#
# Environment variables (override config):
#   ACA_DIFF_COMPRESSION_ENABLED=true/false
#   ACA_DIFF_COMPRESSION_STRATEGY=stat|compact|filtered|function-context|smart
#
# ## Troubleshooting
#
# - Compression too aggressive? Try "function-context" or increase token_limit
# - Still too large? Use "stat" strategy for maximum compression
# - Unexpected results? Check logs with --verbose, look for compression warnings
# - Generation failures? Try ACA_DIFF_COMPRESSION_ENABLED=false to rule out compression
#
# =============================================================================


def calculate_diff_size(diff_output: str, repo: git.Repo | None = None) -> dict[str, int]:
    """Calculate size metrics for a git diff output.

    Args:
        diff_output: Raw git diff output string
        repo: Optional GitPython Repo object for accurate file count via --name-only

    Returns:
        Dictionary with keys:
        - bytes: Total byte size of the diff (UTF-8 encoded)
        - chars: Total character count of the diff
        - lines: Total line count
        - files: Number of changed files (from --name-only if repo provided, else regex fallback)

    Example:
        >>> metrics = calculate_diff_size(diff_output, repo)
        >>> print(f"Size: {metrics['bytes']} bytes, {metrics['files']} files")
    """
    # Calculate file count using --name-only if repo is available (more accurate)
    if repo is not None:
        try:
            name_only_output = repo.git.diff("--cached", "--name-only")
            file_count = len(name_only_output.strip().split("\n")) if name_only_output.strip() else 0
        except git.exc.GitCommandError:
            # Fall back to regex-based counting
            import re

            file_count = len(re.findall(r"^diff --git", diff_output, re.MULTILINE))
    else:
        import re

        file_count = len(re.findall(r"^diff --git", diff_output, re.MULTILINE))

    return {
        "bytes": len(diff_output.encode("utf-8")),
        "chars": len(diff_output),
        "lines": diff_output.count("\n"),
        "files": file_count,
    }


def extract_diff_statistics(repo: git.Repo) -> dict[str, int]:
    """Extract insertion/deletion statistics from staged changes.

    Args:
        repo: GitPython Repo object

    Returns:
        Dictionary with keys:
        - insertions: Number of lines added
        - deletions: Number of lines removed
        - files_changed: Number of files changed

    Example:
        >>> stats = extract_diff_statistics(repo)
        >>> print(f"Changes: +{stats['insertions']} -{stats['deletions']}")
    """
    import re

    result = {"insertions": 0, "deletions": 0, "files_changed": 0}

    try:
        stat_output = repo.git.diff("--cached", "--stat")
        if not stat_output.strip():
            return result

        # Parse the summary line (e.g., "3 files changed, 10 insertions(+), 5 deletions(-)")
        # The summary is typically the last non-empty line
        lines = stat_output.strip().split("\n")
        summary_line = lines[-1] if lines else ""

        # Extract files changed
        files_match = re.search(r"(\d+)\s+files?\s+changed", summary_line)
        if files_match:
            result["files_changed"] = int(files_match.group(1))

        # Extract insertions
        ins_match = re.search(r"(\d+)\s+insertions?\(\+\)", summary_line)
        if ins_match:
            result["insertions"] = int(ins_match.group(1))

        # Extract deletions
        del_match = re.search(r"(\d+)\s+deletions?\(-\)", summary_line)
        if del_match:
            result["deletions"] = int(del_match.group(1))

    except git.exc.GitCommandError:
        logger.debug("Failed to get diff statistics", exc_info=True)

    return result


def should_compress_diff(diff_size: dict[str, int], config: ACAConfig) -> bool:
    """Determine if diff compression should be applied based on thresholds.

    Args:
        diff_size: Dictionary from calculate_diff_size() with 'bytes' and 'files' keys
        config: ACAConfig instance with threshold settings

    Returns:
        True if either size or file count exceeds configured thresholds,
        False otherwise

    Example:
        >>> if should_compress_diff(diff_size, config):
        ...     print("Compression will be applied")
    """
    return diff_size["bytes"] > config.diff_size_threshold_bytes or diff_size["files"] > config.diff_files_threshold


# Compression strategy exclusion patterns
COMPRESSION_EXCLUDE_PATTERNS: set[str] = {
    # Binary files
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.webp",
    "*.svg",
    "*.pdf",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.otf",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.bz2",
    "*.7z",
    "*.rar",
    "*.exe",
    "*.dll",
    "*.so",
    "*.dylib",
    "*.bin",
    # Lock files
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "Gemfile.lock",
    "composer.lock",
    "Pipfile.lock",
    "uv.lock",
    # Minified files
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.bundle.css",
    # Generated files
    "*-generated.*",
    "*.pb.go",
    "*.pb.h",
    "*.pb.cc",
    "*.pb.py",
    "*.g.dart",
    "*.freezed.dart",
    "*.generated.ts",
}

# File priority patterns for smart compression
# Higher scores = higher priority (included in full diff)
FILE_PRIORITY_PATTERNS: dict[str, dict[str, int]] = {
    # High priority (score: 100): Core source code files
    "high": {
        "*.py": 100,
        "*.ts": 100,
        "*.tsx": 100,
        "*.js": 100,
        "*.jsx": 100,
        "*.go": 100,
        "*.rs": 100,
        "*.java": 100,
        "*.c": 100,
        "*.cpp": 100,
        "*.h": 100,
        "*.hpp": 100,
        "*.rb": 100,
        "*.php": 100,
        "*.cs": 100,
        "*.swift": 100,
        "*.kt": 100,
    },
    # Medium priority (score: 50): Configuration and documentation
    "medium": {
        "*.yaml": 50,
        "*.yml": 50,
        "*.toml": 50,
        "*.json": 50,
        "*.md": 50,
        "*.rst": 50,
        "*.txt": 50,
        "Dockerfile": 50,
        "Makefile": 50,
        "*.sh": 50,
        "*.bash": 50,
    },
    # Low priority (score: 10): Build artifacts, lock files
    "low": {
        "*.lock": 10,
        "*.sum": 10,
        "*.mod": 10,
        "package.json": 10,
        "pyproject.toml": 10,
        ".gitignore": 10,
        ".dockerignore": 10,
    },
}

# Indicators of auto-generated files (lower priority)
AUTO_GENERATED_INDICATORS: set[str] = {
    # Comment patterns (checked in file content)
    "@generated",
    "auto-generated",
    "autogenerated",
    "do not edit",
    "do not modify",
    "generated by",
    "code generated",
    # File patterns (checked in filename)
    "*.pb.go",
    "*.pb.h",
    "*.pb.cc",
    "*.pb.py",
    "*.g.dart",
    "*.freezed.dart",
    "*.generated.ts",
    "*_generated.*",
}

# Valid compression strategies
VALID_COMPRESSION_STRATEGIES: set[str] = {"stat", "compact", "filtered", "function-context", "smart"}


def score_file_priority(filepath: str, file_content_sample: str | None = None) -> int:
    """Score a file's priority for smart compression.

    Higher scores indicate higher priority files that should be included
    in the full diff output. Lower scores indicate files that should only
    get stat-level summaries.

    Args:
        filepath: Path to the file being scored
        file_content_sample: Optional first ~1000 chars of file content for
                            auto-generated detection

    Returns:
        Priority score (0-100). 0 means exclude entirely.
    """
    import fnmatch

    filename = filepath.split("/")[-1]  # Get basename

    # Check if file matches exclusion patterns (binary, lock files, etc.)
    for pattern in COMPRESSION_EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(filepath, pattern):
            return 0  # Exclude entirely

    # Check if file matches auto-generated file patterns
    for indicator in AUTO_GENERATED_INDICATORS:
        if indicator.startswith("*"):
            # It's a file pattern
            if fnmatch.fnmatch(filename, indicator) or fnmatch.fnmatch(filepath, indicator):
                return 5  # Very low priority for generated files

    # Check file content for auto-generated indicators
    is_auto_generated = False
    if file_content_sample:
        content_lower = file_content_sample.lower()
        # Only check first 50 lines worth of content (roughly 2000 chars)
        content_to_check = content_lower[:2000]
        for indicator in AUTO_GENERATED_INDICATORS:
            if not indicator.startswith("*") and indicator in content_to_check:
                is_auto_generated = True
                break

    # Find matching priority pattern
    # Check exact filename matches first (patterns without wildcards)
    # to ensure specific filenames like 'package.json' get their designated
    # score instead of matching wildcard patterns like '*.json'
    base_score = 25  # Default score if no pattern matches
    pattern_matched = False

    # First pass: exact filename matches only
    for _priority_level, patterns in FILE_PRIORITY_PATTERNS.items():
        for pattern, score in patterns.items():
            if "*" not in pattern and "?" not in pattern:
                # Exact match pattern
                if filename == pattern or filepath == pattern or filepath.endswith("/" + pattern):
                    base_score = score
                    pattern_matched = True
                    break
        if pattern_matched:
            break

    # Second pass: wildcard patterns (only if no exact match found)
    if not pattern_matched:
        for _priority_level, patterns in FILE_PRIORITY_PATTERNS.items():
            for pattern, score in patterns.items():
                if "*" in pattern or "?" in pattern:
                    if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(filepath, pattern):
                        base_score = score
                        pattern_matched = True
                        break
            if pattern_matched:
                break

    # Apply path-based adjustments
    path_lower = filepath.lower()
    if any(d in path_lower for d in ["tests/", "test/", "__tests__/", "_test."]):
        base_score = int(base_score * 0.8)
    elif any(d in path_lower for d in ["docs/", "documentation/"]):
        base_score = int(base_score * 0.7)
    elif any(d in path_lower for d in ["scripts/", "tools/"]):
        base_score = int(base_score * 0.9)

    # Reduce score if auto-generated
    if is_auto_generated:
        base_score = int(base_score * 0.5)

    return base_score


def compress_diff_stat(repo: git.Repo) -> str:
    """Compress diff using statistical summary (--stat format).

    Most aggressive compression. Shows only file names and change counts.
    Best for: 100+ file changes, massive refactors.

    Args:
        repo: Git repository object

    Returns:
        Statistical summary of staged changes
    """
    result = repo.git.diff("--cached", "--stat")
    return result if result else "No changes staged"


def compress_diff_compact(repo: git.Repo) -> str:
    """Compress diff using minimal context (-U1 format).

    Provides 1 line of context around changes instead of default 3.
    Best for: 50-100 files, mixed changes.

    Args:
        repo: Git repository object

    Returns:
        Compact diff with minimal context
    """
    result = repo.git.diff("--cached", "--compact-summary", "-U1")
    return result if result else "No changes staged"


def compress_diff_filtered(repo: git.Repo) -> tuple[str, int, int]:
    """Compress diff by excluding generated and binary files.

    Filters out lock files, minified files, generated code, and binary files.
    Binary files are detected via git numstat (they show '-' for insertions/deletions).
    Best for: Repos with many generated assets.

    Args:
        repo: Git repository object

    Returns:
        Tuple of (filtered diff, files included, files excluded)
    """
    import fnmatch

    # Get list of all staged files
    all_files = repo.git.diff("--cached", "--name-only").splitlines()
    if not all_files:
        return "No changes staged", 0, 0

    # Detect binary files using numstat
    # Binary files show "-" for insertions and deletions in numstat output
    binary_files: set[str] = set()
    numstat_output = repo.git.diff("--cached", "--numstat").splitlines()
    for line in numstat_output:
        parts = line.split("\t")
        if len(parts) >= 3:
            insertions, deletions, filepath = parts[0], parts[1], parts[2]
            # Binary files show "-" for both insertions and deletions
            if insertions == "-" and deletions == "-":
                binary_files.add(filepath)

    # Filter files based on exclusion patterns and binary detection
    included_files: list[str] = []
    excluded_files: list[str] = []

    for filepath in all_files:
        # Exclude binary files first
        if filepath in binary_files:
            excluded_files.append(filepath)
            continue

        filename = filepath.split("/")[-1]  # Get basename
        excluded = False
        for pattern in COMPRESSION_EXCLUDE_PATTERNS:
            if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(filepath, pattern):
                excluded = True
                break
        if excluded:
            excluded_files.append(filepath)
        else:
            included_files.append(filepath)

    if not included_files:
        # All files were excluded, return stat summary instead
        stat_summary = repo.git.diff("--cached", "--stat")
        return f"All files matched exclusion patterns. Summary:\n{stat_summary}", 0, len(excluded_files)

    # Get diff only for included files
    result = repo.git.diff("--cached", "--", *included_files)
    return result if result else "No changes in included files", len(included_files), len(excluded_files)


def compress_diff_function_context(repo: git.Repo) -> str:
    """Compress diff using function context format.

    Shows complete functions/classes where changes occurred.
    Best for: 20-50 files, focused changes requiring semantic context.

    Args:
        repo: Git repository object

    Returns:
        Diff with full function context
    """
    result = repo.git.diff("--cached", "--function-context")
    return result if result else "No changes staged"


def compress_diff_smart(repo: git.Repo, config: ACAConfig) -> tuple[str, int, int, int]:
    """Compress diff using smart file prioritization.

    Analyzes all changed files, scores them by importance using file extension
    and path patterns, detects auto-generated files, and creates a hybrid diff
    output with full context for priority files and stat-only for others.

    Best for: Mixed changes with some important files and some generated/config files.

    Args:
        repo: Git repository object
        config: ACA configuration with smart compression settings

    Returns:
        Tuple of (compressed_diff, priority_files_count, remaining_files_count, char_count)
    """
    # Step 1: Get all changed files
    all_files = repo.git.diff("--cached", "--name-only").splitlines()
    if not all_files:
        return "No changes staged", 0, 0, 0

    # Step 2: Score and sort files
    scored_files: list[tuple[str, int]] = []
    for filepath in all_files:
        # Get content sample for auto-generated detection
        content_sample: str | None = None
        try:
            # Get staged file content (first 1000 chars)
            raw_content = repo.git.show(f":0:{filepath}")
            content_sample = raw_content[:1000] if raw_content else None
        except git.GitCommandError:
            # File might be deleted or binary, skip content check
            pass
        except UnicodeDecodeError:
            # Binary or non-UTF8 file, skip content check
            pass

        score = score_file_priority(filepath, content_sample)
        if score > 0:  # Only include files with non-zero scores
            scored_files.append((filepath, score))

    if not scored_files:
        # All files were excluded, return stat summary
        stat_summary = repo.git.diff("--cached", "--stat")
        return f"All files matched exclusion patterns. Summary:\n{stat_summary}", 0, len(all_files), len(stat_summary)

    # Sort by score descending
    scored_files.sort(key=lambda x: x[1], reverse=True)

    # Step 3: Determine priority files
    max_priority = config.diff_max_priority_files
    priority_files = [f for f, _ in scored_files[:max_priority]]
    remaining_files = [f for f, _ in scored_files[max_priority:]]

    # Add any excluded files (score=0) to remaining for stat
    excluded_files = [f for f in all_files if f not in [sf[0] for sf in scored_files]]
    remaining_files.extend(excluded_files)

    # Step 4: Build hybrid diff with token limit enforcement
    token_limit = config.diff_token_limit
    max_iterations = 3
    current_priority_count = len(priority_files)
    demoted_files: set[str] = set()  # Track files demoted due to token limit

    for _iteration in range(max_iterations):
        output_parts: list[str] = []

        # Build priority files list, excluding demoted files
        eligible_files = [(f, s) for f, s in scored_files if f not in demoted_files]
        current_priority_files = [f for f, _ in eligible_files[:current_priority_count]]

        # Header
        output_parts.append("# Smart Compressed Diff")
        output_parts.append(f"# Priority files (full diff): {len(current_priority_files)}")
        output_parts.append(f"# Summary files (stat only): {len(scored_files) - len(current_priority_files)}")
        output_parts.append("")

        # Priority files section
        output_parts.append("## Priority Files (Full Diff)")
        output_parts.append("")

        files_over_limit: list[str] = []

        for filepath in current_priority_files:
            try:
                file_diff = repo.git.diff("--cached", "-U3", "--", filepath)
                if file_diff:
                    output_parts.append(file_diff)
                    output_parts.append("")

                    # Check if we're exceeding the token limit
                    current_length = sum(len(p) for p in output_parts)
                    if current_length > token_limit:
                        # Mark this file to move to stat-only
                        files_over_limit.append(filepath)
            except git.GitCommandError:
                # Skip files that can't be diffed
                pass

        # Move files over limit to demoted set and rebuild
        if files_over_limit:
            demoted_files.update(files_over_limit)
            # Reduce count for next iteration (in case we're still over limit)
            current_priority_count = max(1, current_priority_count - len(files_over_limit))
            continue

        # Remaining files section (non-priority + demoted + excluded)
        remaining_to_stat = (
            [f for f, _ in eligible_files[len(current_priority_files) :]] + list(demoted_files) + excluded_files
        )
        if remaining_to_stat:
            output_parts.append("")
            output_parts.append("## Remaining Files (Summary)")
            output_parts.append("")
            try:
                stat_output = repo.git.diff("--cached", "--stat", "--", *remaining_to_stat)
                if stat_output:
                    output_parts.append(stat_output)
            except git.GitCommandError:
                output_parts.append("(Unable to generate stat summary)")

        final_output = "\n".join(output_parts)
        char_count = len(final_output)

        # Check if within limit
        if char_count <= token_limit:
            return final_output, current_priority_count, len(remaining_to_stat), char_count

        # Still over limit, reduce priority files for next iteration
        current_priority_count = max(1, int(current_priority_count * 0.8))

    # Fallback to pure stat if still over limit after iterations
    stat_output = repo.git.diff("--cached", "--stat")
    header = f"# Smart Compression Fallback (diff exceeded {token_limit} char limit)\n\n"
    fallback_output = header + stat_output
    return fallback_output, 0, len(all_files), len(fallback_output)


def apply_compression_strategy(
    repo: git.Repo, strategy: str, original_diff: str, config: ACAConfig | None = None
) -> tuple[str, dict[str, int | str]]:
    """Apply the specified compression strategy to the diff.

    Args:
        repo: Git repository object
        strategy: Compression strategy name ("stat", "compact", "filtered", "function-context", "smart")
        original_diff: The original full diff output
        config: ACA configuration (required for "smart" strategy)

    Returns:
        Tuple of (compressed_diff, compression_info dict)
        compression_info contains: strategy, original_size, compressed_size, files_included, files_excluded
        For "smart" strategy, also includes: char_count, token_limit
    """
    original_size = len(original_diff.encode("utf-8"))
    files_included = 0
    files_excluded = 0
    char_count = 0

    try:
        match strategy:
            case "stat":
                compressed_diff = compress_diff_stat(repo)
            case "compact":
                compressed_diff = compress_diff_compact(repo)
            case "filtered":
                compressed_diff, files_included, files_excluded = compress_diff_filtered(repo)
            case "function-context":
                compressed_diff = compress_diff_function_context(repo)
            case "smart":
                if config is None:
                    config = get_config()
                compressed_diff, files_included, files_excluded, char_count = compress_diff_smart(repo, config)
            case _:
                # Unknown strategy, fall back to compact
                logger.warning(f"Unknown compression strategy '{strategy}', falling back to 'compact'")
                compressed_diff = compress_diff_compact(repo)
                strategy = "compact"

        compressed_size = len(compressed_diff.encode("utf-8"))

        compression_info: dict[str, int | str] = {
            "strategy": strategy,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "files_included": files_included,
            "files_excluded": files_excluded,
        }

        # Add smart-specific info
        if strategy == "smart" and config is not None:
            compression_info["char_count"] = char_count
            compression_info["token_limit"] = config.diff_token_limit

        return compressed_diff, compression_info

    except git.GitCommandError as e:
        logger.error(f"Git command failed during compression: {e}")
        # Fall back to original diff on error
        return original_diff, {
            "strategy": "none",
            "original_size": original_size,
            "compressed_size": original_size,
            "files_included": 0,
            "files_excluded": 0,
        }


def format_compression_info(info: dict[str, int | str]) -> str:
    """Format compression info for user display.

    Args:
        info: Compression info dictionary from apply_compression_strategy()

    Returns:
        Formatted string showing compression statistics
    """
    strategy = info["strategy"]
    original_kb = int(info["original_size"]) / 1024
    compressed_kb = int(info["compressed_size"]) / 1024

    if int(info["original_size"]) > 0:
        reduction_pct = (1 - int(info["compressed_size"]) / int(info["original_size"])) * 100
    else:
        reduction_pct = 0

    result = (
        f"Strategy: {strategy} | Size: {original_kb:.1f} KB → {compressed_kb:.1f} KB ({reduction_pct:.0f}% reduction)"
    )

    if strategy == "filtered" and (info["files_included"] or info["files_excluded"]):
        result += f" | Files: {info['files_included']}/{info['files_included'] + info['files_excluded']} included"

    if strategy == "smart":
        files_included = int(info.get("files_included", 0))
        files_excluded = int(info.get("files_excluded", 0))
        char_count = int(info.get("char_count", 0))
        token_limit = int(info.get("token_limit", 100_000))
        result = (
            f"Strategy: {strategy} | "
            f"Priority: {files_included} files (full), {files_excluded} files (stat) | "
            f"Size: {char_count / 1024:.1f} KB / {token_limit / 1024:.0f} KB limit"
        )

    return result


def get_mr_template(current_branch: str, target_branch: str, ticket_number: str | None = None) -> str:
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
Ticket: {ticket_number or "Not detected"}
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
            console.print("[yellow]This appears to be a transient error. You can retry or use a template.[/yellow]")

        try:
            choice = input("Would you like to (r)etry, use (t)emplate, or (a)bort? [r/t/a]: ").strip().lower()
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

### Diff Format

The diff may be provided in one of these formats:
- **Full diff**: Complete unified diff with all changes
- **Statistical summary** (`--stat`): File-level summary showing files changed and line counts
- **Compact diff** (`-U1`): Minimal context (1 line) around changes
- **Filtered diff**: Source files only, excluding generated/binary files
- **Function context**: Complete functions/classes where changes occurred
- **Smart compression**: Hybrid format with full diff for priority source files (10-15 files) and statistical summary for remaining files

Regardless of format, focus on understanding WHAT changed to explain WHY it changed.

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
- DON'T add "Related:" if a ticket has not been found.

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
    "--verbose",
    "-v",
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
@click.option(
    "--no-compress",
    is_flag=True,
    help="Disable compression for this commit (overrides config/env)",
)
@click.option(
    "--show-prompt",
    is_flag=True,
    help="Display the full prompt before sending to Claude",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Auto-confirm prompts (skips edit/commit/abort and show-prompt confirmations; error recovery remains interactive)",
)
@click.pass_context
def commit(ctx: click.Context, no_compress: bool, show_prompt: bool, yes: bool) -> None:
    """Generate a commit message for staged changes."""
    plain_text = ctx.obj.get("plain_text", False)
    console = get_console(plain_text)

    # Check git dependency
    if not check_dependency("git", console):
        sys.exit(1)

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
                print_error(console, "No staged changes found. Use 'git add' to stage changes.")
                sys.exit(1)
    else:
        # No HEAD yet (fresh repo) - check if there are any staged files at all
        # diff against None gives us all staged files
        staged = list(repo.index.diff(None))
        if not staged and not repo.index.entries:
            print_error(console, "No staged changes found. Use 'git add' to stage changes.")
            sys.exit(1)

    # Collect git context
    try:
        branch_name = repo.active_branch.name
    except TypeError:
        # Detached HEAD - use short commit SHA as identifier
        branch_name = repo.head.commit.hexsha[:7]

    try:
        diff_output = repo.git.diff("--cached")
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to get staged diff: {e}")
        sys.exit(1)

    if not diff_output.strip():
        print_error(console, "No staged changes found. Use 'git add' to stage changes.")
        sys.exit(1)

    # Run pre-commit hooks before invoking Claude.
    staged_files_output = repo.git.diff("--cached", "--name-only")
    staged_files = [f for f in staged_files_output.split("\n") if f]

    # Warn user if hooks will be completely bypassed
    if get_precommit_skip_env():
        console.print("[yellow]⚠ Pre-commit hooks will be completely bypassed (validation + commit phase)[/yellow]")

    hooks_passed, modified_files = run_precommit_hooks(repo, console, staged_files)

    if not hooks_passed:
        print_error(console, "Pre-commit hooks failed. Please fix the issues and try again.")
        console.print("[yellow]Tip: Set SKIP_PRECOMMIT=1 to bypass hooks temporarily[/yellow]")
        sys.exit(1)

    if modified_files:
        console.print("\n[yellow bold]⚠ Pre-commit hooks modified files:[/yellow bold]\n")
        for file in modified_files:
            console.print(f"  • {file}")
        console.print()
        console.print("[cyan]The following files were automatically formatted or modified by hooks.[/cyan]")
        console.print("[cyan]Please review the changes, stage them, and run 'aca commit' again.[/cyan]")
        console.print()
        console.print("[dim]Next steps:[/dim]")
        console.print("  1. Review the modified files: git diff")
        console.print("  2. Stage the changes: git add <files>")
        console.print("  3. Run 'aca commit' again")
        sys.exit(0)

    # Check Claude Code CLI (after hooks have passed with no modifications)
    if not check_claude_cli(console):
        sys.exit(1)
    check_version_compatibility(console)

    # Check diff size and apply compression if needed
    diff_size = calculate_diff_size(diff_output, repo)
    diff_stats = extract_diff_statistics(repo)
    config = get_config()
    needs_compression = should_compress_diff(diff_size, config)

    # Variables for prompt building
    final_diff = diff_output
    compression_info: dict[str, int | str] | None = None
    diff_format_note = ""

    # Feature gate: compression in commit phase
    # This flag allows the subsequent phase to extend compression without conflicts
    # Override with --no-compress flag
    compression_commit_phase_enabled = config.diff_compression_enabled and not no_compress

    # Notify user if compression is disabled via flag
    if no_compress and config.diff_compression_enabled:
        console.print("[yellow]⚠ Compression disabled via --no-compress flag[/yellow]")

    if needs_compression and compression_commit_phase_enabled:
        size_kb = diff_size["bytes"] / 1024
        threshold_kb = config.diff_size_threshold_bytes / 1024
        logger.debug(
            f"Compression threshold check: size={size_kb:.1f}KB, files={diff_size['files']}, "
            f"thresholds={threshold_kb:.0f}KB/{config.diff_files_threshold} files"
        )
        console.print()
        console.print("[yellow bold]⚠ Large diff detected[/yellow bold]")
        console.print()
        console.print(f"Size: {size_kb:.1f} KB, Files: {diff_size['files']}, Lines: {diff_size['lines']}")
        console.print(f"Changes: +{diff_stats['insertions']} -{diff_stats['deletions']}")
        console.print()

        # Apply compression strategy with error recovery
        strategy = config.diff_compression_strategy
        logger.debug(f"Selected compression strategy: {strategy}")

        try:
            final_diff, compression_info = apply_compression_strategy(repo, strategy, diff_output, config)

            # Validate compression output
            if compression_info is None:
                logger.warning("Compression returned None info, falling back to original diff")
                console.print("[yellow]⚠ Compression produced no metadata, using original diff[/yellow]")
                final_diff = diff_output
                compression_info = {
                    "strategy": "none",
                    "original_size": len(diff_output.encode("utf-8")),
                    "compressed_size": len(diff_output.encode("utf-8")),
                    "files_included": 0,
                    "files_excluded": 0,
                }

            # Validate required keys exist
            required_keys = {"strategy", "original_size", "compressed_size"}
            if not required_keys.issubset(compression_info.keys()):
                missing = required_keys - compression_info.keys()
                logger.warning(f"Compression info missing keys {missing}, falling back to original diff")
                console.print("[yellow]⚠ Compression metadata incomplete, using original diff[/yellow]")
                final_diff = diff_output
                compression_info = {
                    "strategy": "none",
                    "original_size": len(diff_output.encode("utf-8")),
                    "compressed_size": len(diff_output.encode("utf-8")),
                    "files_included": 0,
                    "files_excluded": 0,
                }

            # Handle empty diff after compression (edge case: all files matched exclusion patterns)
            if not final_diff.strip():
                logger.warning("Compression produced empty diff, falling back to original diff")
                console.print(
                    "[yellow]⚠ All files matched exclusion patterns during compression, using original diff[/yellow]"
                )
                final_diff = diff_output
                compression_info["strategy"] = "none"
                compression_info["compressed_size"] = compression_info["original_size"]

        except git.GitCommandError as e:
            # Git command failures during compression
            logger.error(f"Git command failed during compression: {e}", exc_info=True)
            console.print("[yellow]⚠ Compression failed (git error), using original diff[/yellow]")
            final_diff = diff_output
            compression_info = {
                "strategy": "none",
                "original_size": len(diff_output.encode("utf-8")),
                "compressed_size": len(diff_output.encode("utf-8")),
                "files_included": 0,
                "files_excluded": 0,
            }
        except MemoryError as e:
            # Memory errors with extremely large diffs
            logger.error(f"Memory error during compression: {e}", exc_info=True)
            console.print("[yellow]⚠ Compression failed (memory error), using original diff[/yellow]")
            final_diff = diff_output
            compression_info = {
                "strategy": "none",
                "original_size": len(diff_output.encode("utf-8")),
                "compressed_size": len(diff_output.encode("utf-8")),
                "files_included": 0,
                "files_excluded": 0,
            }
        except Exception as e:
            # Catch-all for unexpected compression errors
            logger.error(f"Unexpected error during compression: {e}", exc_info=True)
            console.print("[yellow]⚠ Compression failed unexpectedly, using original diff[/yellow]")
            final_diff = diff_output
            compression_info = {
                "strategy": "none",
                "original_size": len(diff_output.encode("utf-8")),
                "compressed_size": len(diff_output.encode("utf-8")),
                "files_included": 0,
                "files_excluded": 0,
            }

        # Log compression results
        actual_strategy = str(compression_info["strategy"])
        logger.debug(
            f"Compression results: strategy={actual_strategy}, "
            f"original={compression_info['original_size']}, compressed={compression_info['compressed_size']}"
        )

        # Display enhanced compression results
        if actual_strategy != "none":
            original_kb = int(compression_info["original_size"]) / 1024
            compressed_kb = int(compression_info["compressed_size"]) / 1024
            if int(compression_info["original_size"]) > 0:
                reduction_pct = (
                    1 - int(compression_info["compressed_size"]) / int(compression_info["original_size"])
                ) * 100
            else:
                reduction_pct = 0

            # Enhanced notification with compression ratio and strategy details
            console.print(f"[green]Compression applied: {actual_strategy}[/green]", end="")

            # Add strategy-specific details
            if actual_strategy == "smart":
                files_full = int(compression_info.get("files_included", 0))
                files_stat = int(compression_info.get("files_excluded", 0))
                char_limit_kb = int(compression_info.get("token_limit", 100_000)) / 1024
                console.print(
                    f" | Priority: {files_full} files (full), {files_stat} files (stat) | "
                    f"Size: {compressed_kb:.1f} KB / {char_limit_kb:.0f} KB limit"
                )
            elif actual_strategy == "filtered":
                files_inc = int(compression_info.get("files_included", 0))
                files_exc = int(compression_info.get("files_excluded", 0))
                console.print(f" | Files: {files_inc} included, {files_exc} excluded | Size: {compressed_kb:.1f} KB")
            else:
                console.print(f" | Size: {compressed_kb:.1f} KB")

            console.print(
                f"[cyan]Reduced by {reduction_pct:.0f}%[/cyan] ({original_kb:.1f} KB → {compressed_kb:.1f} KB)"
            )
            console.print(f"Thresholds: {threshold_kb:.0f} KB or {config.diff_files_threshold} files")
            console.print()

            diff_format_note = (
                f"\n**Diff format:** {actual_strategy} (compressed from {original_kb:.1f} KB to {compressed_kb:.1f} KB)"
            )
        else:
            # Compression was skipped or failed - notify user
            console.print("[yellow]Using original diff (compression not applied)[/yellow]")
            console.print(f"Thresholds: {threshold_kb:.0f} KB or {config.diff_files_threshold} files")
            console.print()

        logger.debug(f"Final diff size for prompt: {len(final_diff.encode('utf-8')) / 1024:.1f} KB")
    else:
        logger.debug(f"Diff size within limits: {diff_size['bytes'] / 1024:.1f} KB, {diff_size['files']} files")

    ticket_number = extract_ticket_number(branch_name)

    # Build prompt
    prompt = f"""{COMMIT_PROMPT}

## Git Context
- Branch: {branch_name}
- Ticket: {ticket_number or "none"}

## Staged Changes Diff{diff_format_note}
{final_diff}
"""

    # Validate prompt size to ensure it's not too large for Claude
    prompt_size_bytes = len(prompt.encode("utf-8"))
    prompt_size_kb = prompt_size_bytes / 1024
    max_prompt_size_kb = 200  # 200KB reasonable limit for Claude prompts

    if prompt_size_kb > max_prompt_size_kb:
        logger.warning(f"Prompt size ({prompt_size_kb:.1f} KB) exceeds recommended limit ({max_prompt_size_kb} KB)")
        console.print()
        console.print(f"[yellow bold]⚠ Large prompt size: {prompt_size_kb:.1f} KB[/yellow bold]")
        console.print(
            f"[yellow]The prompt exceeds the recommended {max_prompt_size_kb} KB limit. "
            "Consider using a more aggressive compression strategy (e.g., 'stat' or 'smart').[/yellow]"
        )
        console.print("[dim]Tip: Set ACA_DIFF_COMPRESSION_STRATEGY=stat for maximum compression[/dim]")
        console.print()

        # If compression was not applied but could have helped, suggest enabling it
        if not (needs_compression and compression_commit_phase_enabled):
            console.print("[dim]Tip: Set ACA_DIFF_COMPRESSION_ENABLED=true to enable automatic compression[/dim]")
            console.print()
    else:
        logger.debug(f"Prompt size within limits: {prompt_size_kb:.1f} KB")

    # Check if file-based delivery will be used
    will_use_file_based = config.prompt_file_enabled and should_use_file_based_prompt(prompt, config)

    # Notify about file-based delivery for large prompts
    if will_use_file_based:
        console.print(
            f"[dim]File-based prompt delivery will be used for this large diff ({prompt_size_kb:.1f} KB)[/dim]"
        )

    # Pre-create file-based prompt if --show-prompt is set and file-based delivery will be used
    # This ensures the preview shows the actual prompt that will be sent to Claude
    prepared_prompt: str | None = None
    prepared_temp_file: str | None = None

    if show_prompt and will_use_file_based:
        file_result = create_file_based_prompt(
            prompt,
            section_marker="## Staged Changes Diff",
            target_dir=str(repo.working_dir),
        )
        if file_result is not None:
            prepared_prompt, prepared_temp_file = file_result
            logger.debug(f"Pre-created file-based prompt for --show-prompt preview: {prepared_temp_file}")

    # Determine which prompt to show in preview
    display_prompt = prepared_prompt if prepared_prompt is not None else prompt
    display_size_bytes = len(display_prompt.encode("utf-8"))
    display_size_kb = display_size_bytes / 1024

    # Handle --show-prompt flag: display prompt and ask for confirmation
    if show_prompt:
        console.print()
        console.print("[bold]═══════════════════════════════════════════════════════════════════[/bold]")
        console.print("[bold]PROMPT PREVIEW[/bold]")
        if prepared_prompt is not None:
            console.print(f"[dim]Size: {display_size_kb:.1f} KB ({display_size_bytes:,} characters)[/dim]")
            console.print(
                "[yellow]Note: File-based delivery active. Diff content is in the temp file shown below.[/yellow]"
            )
        else:
            console.print(f"[dim]Size: {prompt_size_kb:.1f} KB ({prompt_size_bytes:,} characters)[/dim]")
        console.print("[bold]═══════════════════════════════════════════════════════════════════[/bold]")
        console.print()
        console.print(display_prompt)
        console.print()
        console.print("[bold]═══════════════════════════════════════════════════════════════════[/bold]")
        console.print()

        # Skip confirmation if --yes flag is provided
        if not yes:
            try:
                confirm = input("Send this prompt to Claude? [y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Clean up temp file on abort
                if prepared_temp_file is not None:
                    cleanup_temp_prompt_file(prepared_temp_file)
                console.print("\nAborted.")
                sys.exit(0)

            if confirm not in ("y", "yes"):
                # Clean up temp file on abort
                if prepared_temp_file is not None:
                    cleanup_temp_prompt_file(prepared_temp_file)
                console.print("Aborted.")
                sys.exit(0)

    # Prepare fallback template for graceful degradation
    # Note: This template is compression-agnostic and doesn't include diff content,
    # so it works regardless of whether compression was applied or failed
    fallback_template = get_commit_template(branch_name, ticket_number)

    # Generate commit message with retry and fallback support
    commit_message: str | None = None
    max_generation_attempts = 3  # Allow user to retry generation

    # Use prepared prompt if available (from --show-prompt flow), otherwise use original
    generation_prompt = prepared_prompt if prepared_prompt is not None else prompt
    # Skip automatic file-based delivery if we already prepared the prompt
    skip_auto_file_delivery = prepared_prompt is not None

    # Helper to clean up the prepared temp file
    def cleanup_prepared_file() -> None:
        if prepared_temp_file is not None:
            cleanup_temp_prompt_file(prepared_temp_file)

    for generation_attempt in range(max_generation_attempts):
        try:
            raw_response = generate_with_progress(
                console,
                generation_prompt,
                str(repo.working_dir),
                message="Generating commit message...",
                skip_file_based_delivery=skip_auto_file_delivery,
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
            cleanup_prepared_file()
            sys.exit(1)

        except (ClaudeNetworkError, ClaudeTimeoutError, ClaudeRateLimitError) as e:
            # Transient errors: offer retry or fallback
            # Include compression context in error logs for diagnostics
            if compression_info is not None and str(compression_info.get("strategy", "none")) != "none":
                logger.warning(
                    f"Transient error (attempt {generation_attempt + 1}): {e} "
                    f"[compression: {compression_info['strategy']}, size: {int(compression_info['compressed_size']) / 1024:.1f}KB]"
                )
            else:
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
                    line for line in commit_message.split("\n") if not line.strip().startswith("#")
                ).strip()
                break
            # User chose retry, continue loop

        except (ClaudeCLIError, ClaudeContentError) as e:
            # Non-transient errors: offer fallback but no automatic retry
            # Check for "Argument list too long" error which indicates ARG_MAX exceeded
            error_str = str(e)
            if "Argument list too long" in error_str or (hasattr(e, "cause") and "E2BIG" in str(e.cause)):
                logger.error(
                    f"ARG_MAX exceeded error: {e} "
                    f"[prompt_size: {prompt_size_kb:.1f}KB, file_delivery: {config.prompt_file_enabled}]"
                )
                console.print()
                console.print("[red bold]Error: Prompt too large for command-line delivery[/red bold]")
                console.print()
                console.print(
                    "[yellow]The diff is too large to pass to Claude via command-line arguments. "
                    "This is a known limitation of the Claude Agent SDK.[/yellow]"
                )
                console.print()
                console.print("[bold]Possible solutions:[/bold]")
                console.print("  1. Ensure file-based delivery is enabled: ACA_PROMPT_FILE_ENABLED=true")
                console.print("  2. Use a more aggressive compression strategy: ACA_DIFF_COMPRESSION_STRATEGY=stat")
                console.print("  3. Stage fewer files and commit in smaller batches")
                console.print()
                if not config.prompt_file_enabled:
                    console.print(
                        "[cyan]File-based delivery is currently DISABLED. "
                        "Enable it with: ACA_PROMPT_FILE_ENABLED=true[/cyan]"
                    )
                else:
                    console.print(
                        "[dim]File-based delivery is enabled but may have failed. "
                        "Check logs with ACA_LOG_LEVEL=DEBUG for details.[/dim]"
                    )
                cleanup_prepared_file()
                sys.exit(1)

            # Include compression context in error logs for diagnostics
            if compression_info is not None and str(compression_info.get("strategy", "none")) != "none":
                logger.error(
                    f"Non-recoverable error: {e} "
                    f"[compression: {compression_info['strategy']}, size: {int(compression_info['compressed_size']) / 1024:.1f}KB]"
                )
                console.print(
                    "[dim]Tip: If generation fails consistently with compressed diffs, "
                    "try ACA_DIFF_COMPRESSION_ENABLED=false[/dim]"
                )
            else:
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
                    line for line in commit_message.split("\n") if not line.strip().startswith("#")
                ).strip()
                break
            # User chose retry, continue loop

        except OSError as e:
            # Handle ARG_MAX (Argument list too long) errors specifically
            import errno

            if e.errno == errno.E2BIG or "Argument list too long" in str(e):
                logger.error(
                    f"ARG_MAX exceeded (OSError): {e} "
                    f"[prompt_size: {prompt_size_kb:.1f}KB, file_delivery: {config.prompt_file_enabled}]"
                )
                console.print()
                console.print("[red bold]Error: Prompt too large for command-line delivery[/red bold]")
                console.print()
                console.print(
                    "[yellow]The diff is too large to pass to Claude via command-line arguments. "
                    "This is a known limitation of the Claude Agent SDK.[/yellow]"
                )
                console.print()
                console.print("[bold]Possible solutions:[/bold]")
                console.print("  1. Ensure file-based delivery is enabled: ACA_PROMPT_FILE_ENABLED=true")
                console.print("  2. Use a more aggressive compression strategy: ACA_DIFF_COMPRESSION_STRATEGY=stat")
                console.print("  3. Stage fewer files and commit in smaller batches")
                console.print()
                if not config.prompt_file_enabled:
                    console.print(
                        "[cyan]File-based delivery is currently DISABLED. "
                        "Enable it with: ACA_PROMPT_FILE_ENABLED=true[/cyan]"
                    )
                else:
                    console.print(
                        "[dim]File-based delivery is enabled but may have failed. "
                        "Check logs with ACA_LOG_LEVEL=DEBUG for details.[/dim]"
                    )
                cleanup_prepared_file()
                sys.exit(1)
            else:
                # Re-raise other OSErrors
                raise

        except Exception as e:
            # Unexpected errors: log and offer fallback
            # Include compression context in error logs for diagnostics
            if compression_info is not None and str(compression_info.get("strategy", "none")) != "none":
                logger.exception(
                    f"Unexpected error during generation: {e} "
                    f"[compression: {compression_info['strategy']}, size: {int(compression_info['compressed_size']) / 1024:.1f}KB]"
                )
                console.print(
                    "[dim]Tip: If generation fails consistently with compressed diffs, "
                    "try ACA_DIFF_COMPRESSION_ENABLED=false[/dim]"
                )
            else:
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
                    line for line in commit_message.split("\n") if not line.strip().startswith("#")
                ).strip()
                break

    # Clean up the prepared temp file after generation loop completes
    cleanup_prepared_file()

    if not commit_message:
        print_error(console, "Failed to generate a valid commit message")
        console.print("[yellow]Tip: Run 'aca doctor' to check your configuration.[/yellow]")
        sys.exit(1)

    # Type narrowing for linters/type-checkers.
    assert commit_message is not None

    # Display the generated message and prompt for action
    if yes:
        # Auto-confirm mode: display message and proceed directly to commit
        console.print("\n[bold]Generated Commit Message:[/bold]\n")
        print_output(console, commit_message, markdown=False)
        console.print()
    else:
        while True:
            console.print("\n[bold]Generated Commit Message:[/bold]\n")
            print_output(console, commit_message, markdown=False)
            console.print()

            try:
                choice = input("Do you want to (e)dit, (c)ommit, or (a)bort? [e/c/a]: ").strip().lower()
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
    # Check if we should skip hooks during commit
    skip_env = get_precommit_skip_env()
    should_skip_hooks = bool(skip_env)

    # Build commit command
    commit_cmd = ["git", "commit"]
    if should_skip_hooks:
        commit_cmd.append("--no-verify")
        console.print("[yellow]⚠ Committing with --no-verify (SKIP_PRECOMMIT is set)[/yellow]")
    commit_cmd.extend(["--signoff", "-m", commit_message])

    result = subprocess.run(
        commit_cmd,
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
@click.option(
    "--base",
    type=str,
    default=None,
    help="Base commit, branch, or tag to compare against (overrides automatic detection)",
)
@click.pass_context
def mr_desc(ctx: click.Context, base: str | None) -> None:
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
            "No target branch configured.\n"
            "Run 'git-switch-main.py' to detect and cache the main branch, or set manually:\n"
            "  git config branch-switch.name <branch-name>",
        )
        sys.exit(1)
    assert target_branch is not None

    # Validate branches
    if current_branch == target_branch:
        print_error(console, f"Already on target branch '{target_branch}'")
        sys.exit(1)

    # Fetch target branch
    try:
        repo.git.fetch("origin", target_branch)
    except git.exc.GitCommandError:
        pass  # Ignore fetch errors

    # Determine the log range - prefer user-provided base, then origin/<target_branch>,
    # otherwise fall back to local <target_branch> or upstream ref
    log_range = None
    log_base = None

    # Check if user provided a custom base
    if base:
        try:
            repo.git.rev_parse("--verify", base)
            log_range = f"{base}..{current_branch}"
            log_base = base
        except git.exc.GitCommandError:
            print_error(
                console,
                f"Invalid base reference '{base}'. Please provide a valid commit hash, branch name, or tag.\n"
                "Run 'git log --oneline' or 'git branch -a' to see available references.",
            )
            sys.exit(1)
    else:
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

    # Display commits to be included in MR
    commit_count = len(commits.strip().split("\n"))
    console.print(
        f"\n[bold]Commits to be included in MR[/bold] ({commit_count} commit{'s' if commit_count != 1 else ''}):"
    )
    console.print(f"[dim]Base: {log_base}[/dim]")
    for commit_line in commits.strip().split("\n"):
        console.print(f"  • {commit_line}")
    console.print()

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
    # Some type-checkers don't narrow Optional args well here; an empty string
    # behaves the same as None for our template logic.
    fallback_template = get_mr_template(current_branch, target_branch, ticket_number or "")

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
                section_marker="## Commits",  # Use commits section for file-based delivery
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
        console.print("[yellow]Tip: Run 'aca doctor' to check your configuration.[/yellow]")
        sys.exit(1)

    # Type narrowing for linters/type-checkers.
    assert mr_content is not None

    # Clean markdown wrappers from the generated content
    mr_content = clean_mr_output(mr_content)

    # Display the generated content and prompt for action
    while True:
        console.print("\n[bold]Generated Merge Request:[/bold]\n")
        print_output(console, mr_content, markdown=False)
        console.print()

        try:
            choice = input("Do you want to (e)dit, (c)reate, or (a)bort? [e/c/a]: ").strip().lower()
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
                title = strip_markdown_code_blocks(title_match.group(1).strip())
                continue
            # Also check for [IOTIL-###] pattern at start
            iotil_match = re.match(r"^(\[IOTIL-\d+\].+)$", line.strip())
            if iotil_match:
                title = strip_markdown_code_blocks(iotil_match.group(1).strip())
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
                title = strip_markdown_code_blocks(heading_match.group(1).strip())
                break
            # Use the first non-empty line as-is
            title = strip_markdown_code_blocks(stripped)
            break

    if not title:
        print_error(console, "Could not parse title from generated content. Please try again.")
        sys.exit(1)

    # Type narrowing for linters/type-checkers.
    assert title is not None

    description = "\n".join(description_lines).strip()
    if not description:
        # Use the whole content as description if we couldn't parse it
        description = mr_content

    # Clean the description to remove markdown wrappers and headers
    description = clean_mr_description(description)

    # Clean title for branch naming (strip any remaining markdown wrappers)
    cleaned_title = strip_markdown_code_blocks(title)

    # Handle branch renaming before creating MR
    # Construct new branch name from ticket number and slugified title
    if ticket_number:
        # Compute expected branch name from title before checking if rename is needed
        title_without_ticket = re.sub(r"^\[IOTIL-\d+\]\s*", "", cleaned_title, flags=re.IGNORECASE)
        slugified_title = slugify_branch_name(title_without_ticket)
        if slugified_title:
            expected_branch_name = f"IOTIL-{ticket_number}-{slugified_title}"
        else:
            expected_branch_name = f"IOTIL-{ticket_number}"
        # Only skip rename if branch already matches the expected name (case-insensitive)
        if current_branch.upper() == expected_branch_name.upper():
            new_branch_name = None
        else:
            new_branch_name = expected_branch_name
    else:
        # Prompt user for ticket number if not detected
        console.print("[yellow]No IOTIL ticket number detected in branch name.[/yellow]")
        try:
            ticket_input = input("Enter IOTIL ticket number (or press Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nAborted.")
            sys.exit(0)

        if ticket_input:
            # Validate it's numeric
            if not ticket_input.isdigit():
                print_error(console, "Ticket number must be numeric.")
                sys.exit(1)
            ticket_number = ticket_input
            # Strip [IOTIL-xxx] prefix from title before slugifying to avoid duplication
            title_without_ticket = re.sub(r"^\[IOTIL-\d+\]\s*", "", cleaned_title, flags=re.IGNORECASE)
            slugified_title = slugify_branch_name(title_without_ticket)
            if slugified_title:
                new_branch_name = f"IOTIL-{ticket_number}-{slugified_title}"
            else:
                new_branch_name = f"IOTIL-{ticket_number}"
        else:
            new_branch_name = None

    # Offer branch rename if we have a new name and it differs from current
    if new_branch_name and new_branch_name != current_branch:
        console.print("\n[bold]Branch Rename:[/bold]")
        console.print(f"  Current: {current_branch}")
        console.print(f"  New:     {new_branch_name}")

        try:
            rename_choice = input("Rename branch? [Y/n]: ").strip().lower()
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
                    continue_choice = input("Continue with current branch? [y/N]: ").strip().lower()
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
                "[yellow]Skipping branch rename. Local and remote branch names may differ from MR title.[/yellow]"
            )

    # Validate branch is ready for MR (all changes committed and pushed)
    if not validate_branch_ready_for_mr(repo, current_branch, console):
        sys.exit(1)

    # Execute glab command (branch has been renamed at this point if user confirmed)
    try:
        title_str = cast("str", title)
        glab_cmd: list[str] = [
            "glab",
            "mr",
            "create",
            "--title",
            title_str,
            "--description",
            description,
            "--target-branch",
            target_branch,
            "--draft",
            "--remove-source-branch",
        ]
        result = subprocess.run(
            glab_cmd,
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
    - Diff compression configuration and simulation
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
            result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
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
            result = subprocess.run(["glab", "--version"], capture_output=True, text=True, timeout=10)
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
            result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                cli_version = result.stdout.strip()
                console.print(f"[green]✓[/green] {cli_version}")
                record_check("claude_cli", True, cli_version)
            else:
                console.print("[red]✗ Failed to get version[/red]")
                console.print("  [yellow]Install from https://claude.ai/download[/yellow]")
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

            with open(credentials_file) as f:
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
            console.print("  [yellow]Delete ~/.claude/.credentials.json and re-authenticate[/yellow]")
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
                tomli.load(f)  # Validate TOML syntax
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

    # Diff Compression Configuration section
    console.print("\n[bold]Diff Compression Configuration:[/bold]")
    config = get_config()
    compression_checks_passed = True

    # Valid strategies
    valid_strategies = {"stat", "compact", "filtered", "function-context", "smart"}

    # Check diff_compression_enabled
    status = "[green]✓[/green]" if config.diff_compression_enabled else "[dim]○[/dim]"
    enabled_str = "enabled" if config.diff_compression_enabled else "disabled"
    console.print(f"  {status} Compression: {enabled_str}")

    # Check diff_compression_strategy
    strategy_valid = config.diff_compression_strategy in valid_strategies
    if strategy_valid:
        console.print(f"  [green]✓[/green] Strategy: {config.diff_compression_strategy}")
    else:
        console.print(f"  [red]✗[/red] Strategy: {config.diff_compression_strategy} (invalid)")
        console.print(f"    [yellow]Valid options: {', '.join(sorted(valid_strategies))}[/yellow]")
        compression_checks_passed = False

    # Check diff_size_threshold_bytes
    size_threshold_kb = config.diff_size_threshold_bytes / 1024
    if config.diff_size_threshold_bytes > 0:
        if config.diff_size_threshold_bytes < 1024:  # Less than 1KB
            console.print(f"  [yellow]⚠[/yellow] Size threshold: {size_threshold_kb:.1f} KB (very low)")
        else:
            console.print(f"  [green]✓[/green] Size threshold: {size_threshold_kb:.0f} KB")
    else:
        console.print(f"  [red]✗[/red] Size threshold: {size_threshold_kb:.0f} KB (must be > 0)")
        compression_checks_passed = False

    # Check diff_files_threshold
    if config.diff_files_threshold > 0:
        if config.diff_files_threshold < 5:
            console.print(f"  [yellow]⚠[/yellow] Files threshold: {config.diff_files_threshold} (very low)")
        else:
            console.print(f"  [green]✓[/green] Files threshold: {config.diff_files_threshold}")
    else:
        console.print(f"  [red]✗[/red] Files threshold: {config.diff_files_threshold} (must be > 0)")
        compression_checks_passed = False

    # Check diff_max_priority_files (for smart strategy)
    if 1 <= config.diff_max_priority_files <= 50:
        console.print(f"  [green]✓[/green] Max priority files: {config.diff_max_priority_files}")
    else:
        console.print(f"  [red]✗[/red] Max priority files: {config.diff_max_priority_files} (must be 1-50)")
        compression_checks_passed = False

    # Check diff_token_limit
    token_limit_kb = config.diff_token_limit / 1024
    if config.diff_token_limit >= 10_000:
        console.print(f"  [green]✓[/green] Token limit: {token_limit_kb:.0f} KB")
    else:
        console.print(f"  [red]✗[/red] Token limit: {token_limit_kb:.0f} KB (must be >= 10 KB)")
        compression_checks_passed = False

    # Check diff_smart_priority_enabled
    smart_status = "[green]✓[/green]" if config.diff_smart_priority_enabled else "[dim]○[/dim]"
    smart_str = "enabled" if config.diff_smart_priority_enabled else "disabled"
    console.print(f"  {smart_status} Smart prioritization: {smart_str}")

    record_check("compression_config", compression_checks_passed, f"Strategy: {config.diff_compression_strategy}")

    if not compression_checks_passed:
        all_passed = False

    # Prompt Delivery Configuration section
    console.print("\n[bold]Prompt Delivery Configuration:[/bold]")
    prompt_config_passed = True

    # Check prompt_file_enabled
    prompt_status = "[green]✓[/green]" if config.prompt_file_enabled else "[dim]○[/dim]"
    prompt_enabled_str = "enabled" if config.prompt_file_enabled else "disabled"
    console.print(f"  {prompt_status} File-based delivery: {prompt_enabled_str}")

    # Check prompt_file_threshold_bytes
    prompt_threshold_kb = config.prompt_file_threshold_bytes / 1024
    if config.prompt_file_threshold_bytes >= 10_000:
        console.print(f"  [green]✓[/green] Threshold: {prompt_threshold_kb:.0f} KB")
    else:
        console.print(f"  [red]✗[/red] Threshold: {prompt_threshold_kb:.0f} KB (must be >= 10 KB)")
        prompt_config_passed = False

    # Check temp directory
    import tempfile as tempfile_module

    temp_dir = tempfile_module.gettempdir()
    console.print(f"  [green]✓[/green] Temp directory: {temp_dir}")

    # Test temp directory write permissions
    try:
        # We need delete=False to test cleanup manually, so we can't use context manager
        test_file = tempfile_module.NamedTemporaryFile(  # noqa: SIM115
            mode="w", prefix="aca_test_", suffix=".txt", delete=False, dir=temp_dir
        )
        test_file.write("ACA temp file test")
        test_file.close()
        os.unlink(test_file.name)
        console.print("  [green]✓[/green] Temp directory writable")
    except Exception as e:
        console.print(f"  [red]✗[/red] Temp directory not writable: {e}")
        prompt_config_passed = False

    # Check temp directory space (warn if less than 100MB)
    try:
        import shutil as shutil_module

        total, used, free = shutil_module.disk_usage(temp_dir)
        free_mb = free / (1024 * 1024)
        if free_mb < 100:
            console.print(f"  [yellow]⚠[/yellow] Temp directory low on space: {free_mb:.0f} MB free")
        else:
            console.print(f"  [green]✓[/green] Temp directory space: {free_mb:.0f} MB free")
    except Exception:
        console.print("  [dim]○[/dim] Could not check temp directory space")

    record_check("prompt_delivery_config", prompt_config_passed, f"Threshold: {prompt_threshold_kb:.0f} KB")

    if not prompt_config_passed:
        all_passed = False

    # Prompt Delivery Test section
    console.print("\n[bold]Prompt Delivery Test:[/bold]")
    prompt_test_passed = True

    try:
        # Create a mock large prompt (150KB) to test file-based delivery
        mock_diff = "+" * 150_000  # 150KB of content
        mock_prompt = f"""# Test Prompt

## Staged Changes Diff
{mock_diff}
"""
        mock_prompt_size_kb = len(mock_prompt.encode("utf-8")) / 1024
        console.print(f"  Mock prompt size: {mock_prompt_size_kb:.1f} KB")

        # Check if file-based delivery would trigger
        from common_utils import should_use_file_based_prompt

        would_trigger = should_use_file_based_prompt(mock_prompt, config)
        if config.prompt_file_enabled:
            if would_trigger:
                console.print("  [green]✓[/green] File-based delivery would trigger")
            else:
                console.print("  [yellow]⚠[/yellow] File-based delivery would NOT trigger (threshold too high?)")
        else:
            console.print("  [dim]○[/dim] File-based delivery disabled, skipping trigger test")

        # Test actual file creation and cleanup
        if config.prompt_file_enabled:
            from common_utils import cleanup_temp_prompt_file, create_file_based_prompt

            result = create_file_based_prompt(mock_prompt)
            if result is not None:
                modified_prompt, temp_file_path = result
                modified_size_kb = len(modified_prompt.encode("utf-8")) / 1024
                console.print(f"  [green]✓[/green] File creation successful: {temp_file_path}")
                console.print(f"  [green]✓[/green] Modified prompt size: {modified_size_kb:.1f} KB")

                # Check temp file exists and has content
                if os.path.exists(temp_file_path):
                    file_size = os.path.getsize(temp_file_path)
                    console.print(f"  [green]✓[/green] Temp file size: {file_size / 1024:.1f} KB")
                else:
                    console.print("  [red]✗[/red] Temp file was not created")
                    prompt_test_passed = False

                # Clean up
                cleanup_temp_prompt_file(temp_file_path)
                if not os.path.exists(temp_file_path):
                    console.print("  [green]✓[/green] Cleanup successful")
                else:
                    console.print("  [yellow]⚠[/yellow] Cleanup may have failed (file still exists)")
            else:
                console.print("  [red]✗[/red] File-based prompt creation failed")
                prompt_test_passed = False

        if prompt_test_passed:
            console.print("  [green]✓[/green] Prompt delivery test passed")
            record_check("prompt_delivery_test", True, "Test passed")
        else:
            console.print("  [red]✗[/red] Prompt delivery test failed")
            record_check("prompt_delivery_test", False, "Test failed")
            all_passed = False

    except Exception as e:
        console.print(f"  [red]✗[/red] Prompt delivery test failed: {e}")
        record_check("prompt_delivery_test", False, str(e))
        all_passed = False

    # Compression Simulation Test section
    console.print("\n[bold]Compression Simulation Test:[/bold]")

    # Generate synthetic files that guarantee exceeding the configured thresholds
    # Use max(size_threshold + 1, files_threshold + 1) to ensure triggering
    num_test_files = max(config.diff_files_threshold + 1, 8)
    # Ensure size exceeds threshold: generate enough content per file
    target_size_bytes = config.diff_size_threshold_bytes + 1024  # 1KB over threshold
    bytes_per_file = (target_size_bytes // num_test_files) + 1

    # Mix of file types to test filtering
    test_files = [
        ("src/main.py", "py"),
        ("src/utils.py", "py"),
        ("src/api/handler.ts", "ts"),
        ("package-lock.json", "lock"),
        ("dist/bundle.min.js", "min"),
        ("src/components/App.tsx", "tsx"),
        ("tests/test_main.py", "py"),
        ("config.yaml", "yaml"),
    ]
    # Extend to ensure we have enough files
    while len(test_files) < num_test_files:
        idx = len(test_files)
        test_files.append((f"src/module{idx}.py", "py"))

    compression_test_passed = True
    import tempfile

    try:
        # Create a temporary git repo with synthetic files
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Initialize a git repo
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir, check=True)

            # Create directories and initial files
            for filename, _ in test_files:
                file_path = tmp_path / filename
                file_path.parent.mkdir(parents=True, exist_ok=True)
                # Write initial content
                file_path.write_text(f"# Initial content for {filename}\n")

            # Initial commit
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "Initial"], cwd=tmpdir, check=True)

            # Modify files to create a diff that exceeds thresholds
            lines_per_file = bytes_per_file // 50  # ~50 bytes per line
            for filename, _ in test_files:
                file_path = tmp_path / filename
                content_lines = [f"# Modified content for {filename}"]
                for i in range(lines_per_file):
                    content_lines.append(f"line_{i} = 'modified content line {i} for file {filename}'")
                file_path.write_text("\n".join(content_lines))

            # Stage all changes
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)

            # Open the repo and get the diff
            import git

            test_repo = git.Repo(tmpdir)
            original_diff = test_repo.git.diff("--staged")

            # Calculate diff size
            diff_size = {
                "bytes": len(original_diff.encode("utf-8")),
                "chars": len(original_diff),
                "lines": original_diff.count("\n"),
                "files": len(test_files),
            }

            diff_size_kb = diff_size["bytes"] / 1024
            console.print(f"  Synthetic diff: {diff_size_kb:.1f} KB, {diff_size['files']} files")

            # Verify compression would trigger
            would_compress = should_compress_diff(diff_size, config)
            if would_compress:
                console.print("  [green]✓[/green] Compression triggers (exceeds thresholds)")
            else:
                console.print("  [red]✗[/red] Compression should trigger but didn't")
                console.print(
                    f"    Size threshold: {config.diff_size_threshold_bytes} bytes, actual: {diff_size['bytes']} bytes"
                )
                console.print(f"    Files threshold: {config.diff_files_threshold}, actual: {diff_size['files']}")
                compression_test_passed = False

            # Actually run compression
            console.print(f"  Testing compression strategy '{config.diff_compression_strategy}'...")

            compressed_diff, compression_info = apply_compression_strategy(
                test_repo, config.diff_compression_strategy, original_diff, config
            )

            # Verify compressed output is not empty
            if not compressed_diff or len(compressed_diff.strip()) == 0:
                console.print("  [red]✗[/red] Compression returned empty output")
                compression_test_passed = False
            else:
                compressed_size_kb = len(compressed_diff.encode("utf-8")) / 1024
                console.print(f"  [green]✓[/green] Compressed output: {compressed_size_kb:.1f} KB")

            # Verify required metadata is present
            required_metadata = ["strategy", "original_size", "compressed_size"]
            missing_metadata = [key for key in required_metadata if key not in compression_info]

            if missing_metadata:
                console.print(f"  [red]✗[/red] Missing metadata: {', '.join(missing_metadata)}")
                compression_test_passed = False
            else:
                console.print("  [green]✓[/green] Compression metadata complete")

            # Show compression ratio
            if compression_info.get("original_size") and compression_info.get("compressed_size"):
                original_size = compression_info["original_size"]
                compressed_size = compression_info["compressed_size"]
                if original_size > 0:
                    ratio = (1 - compressed_size / original_size) * 100
                    console.print(f"  [green]✓[/green] Compression ratio: {ratio:.1f}%")

            # Verify strategy reported matches configured
            if compression_info.get("strategy") != config.diff_compression_strategy:
                # Allow fallback to compact for unknown strategies
                if compression_info.get("strategy") != "compact":
                    console.print(
                        f"  [yellow]⚠[/yellow] Strategy mismatch: expected "
                        f"'{config.diff_compression_strategy}', got '{compression_info.get('strategy')}'"
                    )

        if compression_test_passed:
            console.print("  [green]✓[/green] Compression simulation passed")
            record_check("compression_simulation", True, f"Strategy: {config.diff_compression_strategy}")
        else:
            console.print("  [red]✗[/red] Compression simulation failed")
            record_check("compression_simulation", False, "Compression test failed")
            all_passed = False

    except Exception as e:
        console.print(f"  [red]✗[/red] Simulation failed: {e}")
        record_check("compression_simulation", False, str(e))
        all_passed = False

    # Check environment variables
    console.print("\n[bold]Environment Variables:[/bold]")
    env_vars = [
        (
            "ANTHROPIC_API_KEY",
            bool(os.environ.get("ANTHROPIC_API_KEY")),
            "Set" if os.environ.get("ANTHROPIC_API_KEY") else "Not set",
        ),
        (
            "ACA_TIMEOUT",
            bool(os.environ.get("ACA_TIMEOUT")),
            os.environ.get("ACA_TIMEOUT", "Not set"),
        ),
        (
            "ACA_RETRY_ATTEMPTS",
            bool(os.environ.get("ACA_RETRY_ATTEMPTS")),
            os.environ.get("ACA_RETRY_ATTEMPTS", "Not set"),
        ),
        (
            "ACA_LOG_LEVEL",
            bool(os.environ.get("ACA_LOG_LEVEL")),
            os.environ.get("ACA_LOG_LEVEL", "Not set"),
        ),
        ("EDITOR", bool(os.environ.get("EDITOR")), os.environ.get("EDITOR", "Not set")),
        ("VISUAL", bool(os.environ.get("VISUAL")), os.environ.get("VISUAL", "Not set")),
        # Compression-related environment variables
        # ACA_DIFF_COMPRESSION is a short alias for ACA_DIFF_COMPRESSION_ENABLED
        # Precedence: ACA_DIFF_COMPRESSION_ENABLED takes priority if both are set
        (
            "ACA_DIFF_COMPRESSION",
            bool(os.environ.get("ACA_DIFF_COMPRESSION")),
            os.environ.get("ACA_DIFF_COMPRESSION", "Not set"),
        ),
        (
            "ACA_DIFF_COMPRESSION_ENABLED",
            bool(os.environ.get("ACA_DIFF_COMPRESSION_ENABLED")),
            os.environ.get("ACA_DIFF_COMPRESSION_ENABLED", "Not set"),
        ),
        (
            "ACA_DIFF_COMPRESSION_STRATEGY",
            bool(os.environ.get("ACA_DIFF_COMPRESSION_STRATEGY")),
            os.environ.get("ACA_DIFF_COMPRESSION_STRATEGY", "Not set"),
        ),
        (
            "ACA_DIFF_SIZE_THRESHOLD",
            bool(os.environ.get("ACA_DIFF_SIZE_THRESHOLD")),
            os.environ.get("ACA_DIFF_SIZE_THRESHOLD", "Not set"),
        ),
        (
            "ACA_DIFF_FILES_THRESHOLD",
            bool(os.environ.get("ACA_DIFF_FILES_THRESHOLD")),
            os.environ.get("ACA_DIFF_FILES_THRESHOLD", "Not set"),
        ),
        (
            "ACA_DIFF_MAX_PRIORITY_FILES",
            bool(os.environ.get("ACA_DIFF_MAX_PRIORITY_FILES")),
            os.environ.get("ACA_DIFF_MAX_PRIORITY_FILES", "Not set"),
        ),
        (
            "ACA_DIFF_TOKEN_LIMIT",
            bool(os.environ.get("ACA_DIFF_TOKEN_LIMIT")),
            os.environ.get("ACA_DIFF_TOKEN_LIMIT", "Not set"),
        ),
        # Prompt delivery environment variables
        (
            "ACA_PROMPT_FILE_ENABLED",
            bool(os.environ.get("ACA_PROMPT_FILE_ENABLED")),
            os.environ.get("ACA_PROMPT_FILE_ENABLED", "Not set"),
        ),
        (
            "ACA_PROMPT_FILE_THRESHOLD",
            bool(os.environ.get("ACA_PROMPT_FILE_THRESHOLD")),
            os.environ.get("ACA_PROMPT_FILE_THRESHOLD", "Not set"),
        ),
    ]

    for var_name, is_set, display_value in env_vars:
        if var_name == "ANTHROPIC_API_KEY":
            # Don't show the actual API key
            display_value = "Set (hidden)" if is_set else "Not set"
        status = "[green]✓[/green]" if is_set else "[dim]○[/dim]"
        console.print(f"  {status} {var_name}: {display_value}")

    diagnostic_info["environment"] = {var_name: ("Set" if is_set else "Not set") for var_name, is_set, _ in env_vars}

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

    # Add compression configuration to diagnostic info for export
    diagnostic_info["compression"] = {
        "enabled": config.diff_compression_enabled,
        "strategy": config.diff_compression_strategy,
        "size_threshold_kb": config.diff_size_threshold_bytes / 1024,
        "files_threshold": config.diff_files_threshold,
        "max_priority_files": config.diff_max_priority_files,
        "token_limit": config.diff_token_limit,
        "smart_priority_enabled": config.diff_smart_priority_enabled,
    }

    # Add prompt delivery configuration to diagnostic info for export
    diagnostic_info["prompt_delivery"] = {
        "enabled": config.prompt_file_enabled,
        "threshold_kb": config.prompt_file_threshold_bytes / 1024,
        "temp_directory": tempfile_module.gettempdir(),
    }

    # Full test with actual API call
    if full and all_passed:
        console.print("\n[bold]Live API Test:[/bold]")
        console.print("Testing Claude API with simple query... ", end="")
        try:
            test_response = asyncio.run(generate_with_claude("Reply with exactly: OK", os.getcwd()))
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
        console.print("[yellow]Some checks failed. Review the output above for details.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter

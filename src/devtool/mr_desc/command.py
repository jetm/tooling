"""devtool mr-desc — AI-powered merge request description generation."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from typing import TYPE_CHECKING, cast

import click

if TYPE_CHECKING:
    import git
    from rich.console import Console

logger = logging.getLogger(__name__)


# =============================================================================
# MR-specific text processing
# =============================================================================


def clean_mr_description(description: str) -> str:
    """Clean merge request description by removing markdown wrappers and headers."""
    from devtool.common.git import strip_markdown_code_blocks

    WRAPPER_ARTIFACTS = {"markdown", "...", ""}

    cleaned = strip_markdown_code_blocks(description)
    lines = cleaned.split("\n")

    while lines and lines[0].strip().lower() in WRAPPER_ARTIFACTS:
        lines = lines[1:]
    while lines and lines[-1].strip().lower() in WRAPPER_ARTIFACTS:
        lines = lines[:-1]

    result_lines = []
    skip_header = True

    for line in lines:
        if skip_header:
            if not line.strip():
                continue
            if re.match(r"^#{1,2}\s*description\s*$", line.strip(), re.IGNORECASE):
                continue
            skip_header = False
        result_lines.append(line)

    return "\n".join(result_lines).strip()


def clean_mr_output(content: str) -> str:
    """Clean full MR output by removing code block wrappers around sections."""

    pattern = re.compile(
        r"^\s*"
        r"((?:\*{0,2}(?:Title|Description):\*{0,2})|(?:#{1,2}\s*(?:Title|Description):?))"
        r"\s*\n"
        r"```[a-zA-Z]*\n"
        r"(.*?)"
        r"\n```",
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )

    def replace_section(match: re.Match[str]) -> str:
        header = match.group(1)
        section_content = match.group(2).strip()
        return f"{header}\n{section_content}"

    return pattern.sub(replace_section, content)


def slugify_branch_name(title: str, max_length: int = 50) -> str:
    """Convert a title into a valid git branch name slug."""
    if not title:
        return ""

    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")

    if len(slug) > max_length:
        truncated = slug[:max_length]
        last_hyphen = truncated.rfind("-")
        if last_hyphen > max_length // 2:
            slug = truncated[:last_hyphen]
        else:
            slug = truncated.rstrip("-")

    return slug


def rename_and_push_branch(repo: git.Repo, old_name: str, new_name: str, console: Console) -> bool:
    """Rename a branch locally and update the remote."""
    import git

    from devtool.common.console import print_error

    # Check if new branch name already exists locally
    try:
        repo.git.rev_parse("--verify", new_name)
        try:
            confirm = input(f"Branch '{new_name}' already exists locally. Overwrite? [y/N]: ").strip().lower()
        except EOFError, KeyboardInterrupt:
            console.print("\nAborted.")
            return False

        if confirm not in ("y", "yes"):
            console.print("Branch rename cancelled.")
            return False

        try:
            repo.git.branch("-D", new_name)
        except git.exc.GitCommandError as e:
            print_error(console, f"Failed to delete existing branch '{new_name}': {e}")
            return False
    except git.exc.GitCommandError:
        pass

    console.print(f"Renaming branch from '{old_name}' to '{new_name}'...")
    try:
        repo.git.branch("-m", old_name, new_name)
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to rename branch: {e}")
        return False

    remote_branch_exists = False
    try:
        result = repo.git.ls_remote("--heads", "origin", old_name)
        remote_branch_exists = bool(result.strip())
    except git.exc.GitCommandError:
        pass

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
    """Validate that a branch is ready for MR creation."""
    import git

    from devtool.common.console import print_error

    has_uncommitted = repo.is_dirty(untracked_files=False)
    staged_changes = list(repo.index.diff("HEAD"))
    untracked_files = repo.untracked_files

    if has_uncommitted or staged_changes or untracked_files:
        console.print("\n[yellow]You have uncommitted changes. MR should include all changes.[/yellow]")
        console.print("\n[bold]Status:[/bold]")
        status_output = repo.git.status("--short")
        console.print(status_output)

        try:
            choice = input("\nOptions: (c)ommit first, (a)bort, (i)gnore and continue: ").strip().lower()
        except EOFError, KeyboardInterrupt:
            console.print("\nAborted.")
            return False

        if choice in ("c", "commit"):
            console.print(
                "\n[cyan]Please commit your changes first using 'devtool commit' or 'git commit', "
                "then run 'devtool mr-desc' again.[/cyan]"
            )
            return False
        elif choice in ("a", "abort"):
            console.print("\nAborted.")
            return False
        elif choice not in ("i", "ignore"):
            console.print("[red]Invalid choice. Aborting.[/red]")
            return False

    try:
        unpushed_count = repo.git.rev_list(f"origin/{branch_name}..{branch_name}", "--count")
        unpushed_count = int(unpushed_count.strip())
    except git.exc.GitCommandError:
        unpushed_count = 0

    if unpushed_count > 0:
        console.print(f"\n[yellow]You have {unpushed_count} unpushed commit(s). MR requires pushed commits.[/yellow]")
        console.print("\n[bold]Unpushed commits:[/bold]")
        log_output = repo.git.log(f"origin/{branch_name}..{branch_name}", "--oneline")
        console.print(log_output)

        try:
            choice = input("\nOptions: (p)ush now, (a)bort: ").strip().lower()
        except EOFError, KeyboardInterrupt:
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


def get_mr_template(current_branch: str, target_branch: str, ticket_number: str | None = None) -> str:
    """Get a fallback MR description template."""
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


MR_PROMPT_TEMPLATE = """Generate a title and description for a GitLab merge request based on the following commits. Do not run any commands or tools — output only text.

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
Output ONLY the raw content below. No preamble, no commentary, no markdown bold/backtick formatting on the title line. Start your response with "Title:" on the very first line.

Title: [IOTIL-###] Short descriptive title

## Problem

Brief overview...

## Solution

Approach taken...

## Key changes

- Change 1
- Change 2

## Reviewer notes

Production impact..."""


# =============================================================================
# MR Description Command
# =============================================================================


@click.command()
@click.option(
    "--base",
    type=str,
    default=None,
    help="Base commit, branch, or tag to compare against (overrides automatic detection)",
)
@click.option("--plain-text", is_flag=True, help="Output plain text without formatting")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
@click.pass_context
def mr_desc(ctx: click.Context, base: str | None, plain_text: bool, verbose: bool) -> None:
    """Generate a merge request description."""
    import git

    from devtool.common.console import (
        check_claude_cli,
        check_dependency,
        check_version_compatibility,
        get_console,
        print_error,
        print_output,
        setup_logging,
    )
    from devtool.common.git import (
        PREAMBLE_PATTERNS,
        edit_in_editor,
        extract_ticket_number,
        get_target_branch_from_config,
        strip_markdown_code_blocks,
    )

    setup_logging(verbose=verbose)
    console = get_console(plain_text)

    if not check_dependency("git", console):
        sys.exit(1)
    if not check_dependency("glab", console):
        sys.exit(1)
    cli_version = check_claude_cli(console)
    if cli_version is None:
        sys.exit(1)
    check_version_compatibility(console, version=cli_version)

    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print_error(console, "Not in a git repository")
        sys.exit(1)

    try:
        current_branch = repo.active_branch.name
    except TypeError:
        print_error(console, "Not on a branch (detached HEAD state)")
        sys.exit(1)

    target_branch = get_target_branch_from_config(repo)
    if not target_branch:
        print_error(
            console,
            "No target branch configured.\n"
            "Run 'devtool switch-main' to detect and cache the main branch, or set manually:\n"
            "  git config branch-switch.name <branch-name>",
        )
        sys.exit(1)
    assert target_branch is not None

    if current_branch == target_branch:
        print_error(console, f"Already on target branch '{target_branch}'")
        sys.exit(1)

    try:
        repo.git.fetch("origin", target_branch)
    except git.exc.GitCommandError:
        pass

    # Determine the log range
    log_range = None
    log_base = None

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
        try:
            repo.git.rev_parse("--verify", f"origin/{target_branch}")
            log_range = f"origin/{target_branch}..{current_branch}"
            log_base = f"origin/{target_branch}"
        except git.exc.GitCommandError:
            pass

        if not log_range:
            try:
                repo.git.rev_parse("--verify", target_branch)
                log_range = f"{target_branch}..{current_branch}"
                log_base = target_branch
            except git.exc.GitCommandError:
                pass

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

    try:
        commits = repo.git.log(log_range, "--pretty=format:%s")
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to get commits: {e}")
        sys.exit(1)

    if not commits.strip():
        print_error(console, f"No commits found between '{log_base}' and '{current_branch}'")
        sys.exit(1)

    commit_count = len(commits.strip().split("\n"))
    console.print(
        f"\n[bold]Commits to be included in MR[/bold] ({commit_count} commit{'s' if commit_count != 1 else ''}):"
    )
    console.print(f"[dim]Base: {log_base}[/dim]")
    for commit_line in commits.strip().split("\n"):
        console.print(f"  • {commit_line}")
    console.print()

    ticket_number = extract_ticket_number(current_branch)
    ticket_display = ticket_number if ticket_number else "<not detected, ask user>"

    prompt = MR_PROMPT_TEMPLATE.format(
        current_branch=current_branch,
        target_branch=target_branch,
        ticket_number=ticket_display,
        commits=commits,
    )

    fallback_template = get_mr_template(current_branch, target_branch, ticket_number or "")

    from devtool.common.claude import generate_with_retry

    mr_content = generate_with_retry(
        console,
        prompt,
        str(repo.working_dir),
        fallback_template,
        "MR description",
        section_marker="## Commits",
    )

    mr_content = clean_mr_output(mr_content)

    # Display the generated content and prompt for action
    while True:
        console.print("\n[bold]Generated Merge Request:[/bold]\n")
        print_output(console, mr_content, markdown=False)
        console.print()

        try:
            choice = input("Do you want to (e)dit, (c)reate, or (a)bort? [e/c/a]: ").strip().lower()
        except EOFError, KeyboardInterrupt:
            console.print("\nAborted.")
            sys.exit(0)

        if choice in ("a", "abort"):
            console.print("Merge request creation cancelled.")
            sys.exit(0)
        elif choice in ("e", "edit"):
            mr_content = edit_in_editor(mr_content, console, ".md")
            continue
        elif choice in ("c", "create"):
            break
        else:
            console.print("Invalid choice. Please enter 'e', 'c', or 'a'.")
            continue

    # Parse title and description from response
    lines = mr_content.split("\n")
    title = None
    description_lines = []
    in_description = False

    for line in lines:
        if not title:
            cleaned_line = line.strip().strip("*_`#").strip()
            title_match = re.match(r"^Title:\s*(.+)$", cleaned_line, re.I)
            if title_match:
                title = title_match.group(1).strip().strip("`")
                continue
            iotil_match = re.match(r"^(\[IOTIL-\d+\].+)$", line.strip())
            if iotil_match:
                title = strip_markdown_code_blocks(iotil_match.group(1).strip())
                continue

        if title and (line.startswith("##") or in_description):
            in_description = True
            description_lines.append(line)

    if not title:
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if PREAMBLE_PATTERNS.match(stripped):
                continue
            heading_match = re.match(r"^#+\s*(.+)$", stripped)
            if heading_match:
                title = strip_markdown_code_blocks(heading_match.group(1).strip())
                break
            title = strip_markdown_code_blocks(stripped)
            break

    if not title:
        print_error(console, "Could not parse title from generated content. Please try again.")
        sys.exit(1)

    assert title is not None

    description = "\n".join(description_lines).strip()
    if not description:
        description = mr_content

    description = clean_mr_description(description)

    cleaned_title = strip_markdown_code_blocks(title)

    # Handle branch renaming before creating MR
    if ticket_number:
        title_without_ticket = re.sub(r"^\[IOTIL-\d+\]\s*", "", cleaned_title, flags=re.IGNORECASE)
        slugified_title = slugify_branch_name(title_without_ticket)
        if slugified_title:
            expected_branch_name = f"IOTIL-{ticket_number}-{slugified_title}"
        else:
            expected_branch_name = f"IOTIL-{ticket_number}"
        if current_branch.upper() == expected_branch_name.upper():
            new_branch_name = None
        else:
            new_branch_name = expected_branch_name
    else:
        console.print("[yellow]No IOTIL ticket number detected in branch name.[/yellow]")
        try:
            ticket_input = input("Enter IOTIL ticket number (or press Enter to skip): ").strip()
        except EOFError, KeyboardInterrupt:
            console.print("\nAborted.")
            sys.exit(0)

        if ticket_input:
            if not ticket_input.isdigit():
                print_error(console, "Ticket number must be numeric.")
                sys.exit(1)
            ticket_number = ticket_input
            title_without_ticket = re.sub(r"^\[IOTIL-\d+\]\s*", "", cleaned_title, flags=re.IGNORECASE)
            slugified_title = slugify_branch_name(title_without_ticket)
            if slugified_title:
                new_branch_name = f"IOTIL-{ticket_number}-{slugified_title}"
            else:
                new_branch_name = f"IOTIL-{ticket_number}"
        else:
            new_branch_name = None

    if new_branch_name and new_branch_name != current_branch:
        console.print("\n[bold]Branch Rename:[/bold]")
        console.print(f"  Current: {current_branch}")
        console.print(f"  New:     {new_branch_name}")

        try:
            rename_choice = input("Rename branch? [Y/n]: ").strip().lower()
        except EOFError, KeyboardInterrupt:
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
                except EOFError, KeyboardInterrupt:
                    console.print("\nAborted.")
                    sys.exit(0)

                if continue_choice not in ("y", "yes"):
                    sys.exit(1)
            else:
                current_branch = new_branch_name
        else:
            console.print(
                "[yellow]Skipping branch rename. Local and remote branch names may differ from MR title.[/yellow]"
            )

    if not validate_branch_ready_for_mr(repo, current_branch, console):
        sys.exit(1)

    # Execute glab command
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
        result = subprocess.run(glab_cmd, capture_output=True, text=True, cwd=repo.working_dir)
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

    # Post-MR Jira integration (non-fatal)
    from devtool.common.git import extract_issue_key

    issue_key = extract_issue_key(current_branch)
    if issue_key:
        # Backfill Jira description
        try:
            diff = repo.git.diff(log_base, current_branch)
            if diff.strip():
                from devtool.jira.backfill import backfill_jira_issue

                backfill_jira_issue(issue_key, diff, str(repo.working_dir), console)
        except Exception as e:
            console.print(f"[yellow]Warning: Jira backfill failed: {e}[/yellow]")

        # Transition to Peer Review
        try:
            from devtool.jira.status import transition_jira_issue

            transition_jira_issue(issue_key, "Peer Review")
            console.print(f"[green]{issue_key}[/green] -> Peer Review")
        except Exception as e:
            console.print(f"[yellow]Warning: Jira status transition failed: {e}[/yellow]")

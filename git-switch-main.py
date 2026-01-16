#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "gitpython>=3.1.0",
#     "rich>=13.0.0",
# ]
# ///
"""
Switch to the main/default branch with automatic stashing.

This script detects the main branch of your repository through multiple
strategies (cached config, origin/HEAD, common branch names), automatically
stashes any uncommitted changes, switches to the main branch, and restores
the stash.

Usage:
    git-switch-main.py

The script will:
1. Detect the main branch (cached in git config, or auto-detected)
2. Stash any uncommitted changes (including untracked files)
3. Switch to the main branch (creating a tracking branch if needed)
4. Restore the stashed changes

Configuration:
    The detected main branch is cached in git config under 'branch-switch.name'.
    To manually set it: git config branch-switch.name <branch-name>
"""

import sys
from datetime import datetime

import git
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

console = Console()


def get_repo() -> git.Repo | None:
    """Find and return the git repository, or None if not in a repo."""
    try:
        return git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        console.print(
            Panel(
                "Not in a git repository.\n\nPlease run this command from within a git repository.",
                title="Error",
                border_style="red",
            )
        )
        return None


def detect_main_branch(repo: git.Repo) -> str | None:
    """
    Detect the main branch using multiple strategies.

    Priority:
    1. Cached config (branch-switch.name)
    2. origin/HEAD symbolic reference
    3. Common branch names (stage, main, master, trunk, develop)
    """
    # Priority 1: Check cached config
    try:
        cached_branch = repo.config_reader().get_value("branch-switch", "name", default=None)
        if cached_branch:
            # Verify the branch exists locally or on origin
            local_exists = cached_branch in [h.name for h in repo.heads]
            remote_exists = False
            try:
                remote_exists = cached_branch in [ref.remote_head for ref in repo.remotes.origin.refs]
            except (AttributeError, IndexError):
                pass

            if local_exists or remote_exists:
                console.print(f"[blue]ℹ[/blue] Using cached main branch: [bold]{cached_branch}[/bold]")
                return cached_branch
            else:
                console.print(f"[yellow]⚠[/yellow] Cached branch '{cached_branch}' no longer exists, detecting...")
    except Exception:
        pass

    candidates: list[str] = []

    # Priority 2: Check origin/HEAD
    try:
        origin_head = repo.remotes.origin.refs.HEAD
        if origin_head.is_valid():
            # Get the target of the symbolic reference
            target = origin_head.reference.name
            # Extract branch name from refs/remotes/origin/<branch>
            if "/" in target:
                branch_name = target.split("/")[-1]
                candidates.append(branch_name)
    except (AttributeError, TypeError, KeyError):
        pass

    # Priority 3: Check common branch names
    common_names = ["stage", "main", "master", "trunk", "develop"]
    local_branches = [h.name for h in repo.heads]

    remote_branches: list[str] = []
    try:
        remote_branches = [ref.remote_head for ref in repo.remotes.origin.refs]
    except (AttributeError, IndexError):
        pass

    for name in common_names:
        if name in local_branches or name in remote_branches:
            if name not in candidates:
                candidates.append(name)

    # Priority 4: Deduplicate and select
    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_candidates: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    if not unique_candidates:
        console.print(
            Panel(
                "Could not detect the main branch.\n\n"
                "Please set it manually:\n"
                "  git config branch-switch.name <branch-name>",
                title="Error",
                border_style="red",
            )
        )
        return None

    if len(unique_candidates) == 1:
        selected = unique_candidates[0]
        console.print(f"[green]✓[/green] Detected main branch: [bold]{selected}[/bold]")
        # Save to config
        with repo.config_writer() as config:
            config.set_value("branch-switch", "name", selected)
        return selected

    # Multiple candidates - prompt user
    console.print("[yellow]⚠[/yellow] Multiple potential main branches found. Please select one:")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Branch", style="cyan")

    for i, branch in enumerate(unique_candidates, 1):
        table.add_row(str(i), branch)

    console.print(table)

    choices = [str(i) for i in range(1, len(unique_candidates) + 1)]
    choice = Prompt.ask(
        "Select branch number",
        choices=choices,
        default="1",
    )

    selected = unique_candidates[int(choice) - 1]
    console.print(f"[green]✓[/green] Selected: [bold]{selected}[/bold]")

    # Save to config
    with repo.config_writer() as config:
        config.set_value("branch-switch", "name", selected)

    return selected


def auto_stash(repo: git.Repo) -> str | None:
    """
    Automatically stash changes if the working tree is dirty.

    Returns the stash reference if a stash was created, None otherwise.
    """
    if not repo.is_dirty(untracked_files=True):
        return None

    # Generate stash message
    message = f"auto-switch-main {datetime.now().isoformat()}"

    console.print(f"[yellow]⚠[/yellow] Stashing changes: {message}")

    try:
        repo.git.stash("push", "--include-untracked", "-m", message)

        # Get the stash reference (first entry in stash list)
        stash_list = repo.git.stash("list")
        if stash_list:
            # Extract stash@{0} from the first line
            first_line = stash_list.split("\n")[0]
            stash_ref = first_line.split(":")[0]  # e.g., "stash@{0}"
            console.print(f"[green]✓[/green] Created stash: {stash_ref}")
            return stash_ref

    except git.exc.GitCommandError as e:
        console.print(
            Panel(
                f"Failed to stash changes:\n{e}",
                title="Stash Error",
                border_style="red",
            )
        )
        raise

    return None


def restore_stash(repo: git.Repo, stash_ref: str) -> None:
    """Restore a previously created stash."""
    try:
        repo.git.stash("pop", "--index", stash_ref)
        console.print(f"[green]✓[/green] Restored stash: {stash_ref}")
    except git.exc.GitCommandError as e:
        console.print(
            Panel(
                f"Failed to restore stash automatically:\n{e}\n\n"
                f"Your changes are still saved in: {stash_ref}\n"
                "You can restore them manually with:\n"
                f"  git stash pop {stash_ref}",
                title="Stash Restore Warning",
                border_style="yellow",
            )
        )


def switch_to_branch(repo: git.Repo, branch_name: str) -> None:
    """
    Switch to the specified branch.

    Creates a local tracking branch if the branch only exists on the remote.
    """
    # Check if branch exists locally
    local_branches = {h.name: h for h in repo.heads}
    if branch_name in local_branches:
        local_branches[branch_name].checkout()
        console.print(f"[green]✓[/green] Switched to local branch: [bold]{branch_name}[/bold]")
        return

    # Check if branch exists on remote
    try:
        remote_refs = {ref.remote_head: ref for ref in repo.remotes.origin.refs}
        if branch_name in remote_refs:
            remote_ref = remote_refs[branch_name]

            # Create local tracking branch
            local_branch = repo.create_head(branch_name, remote_ref)
            local_branch.set_tracking_branch(remote_ref)
            local_branch.checkout()

            console.print(f"[green]✓[/green] Created and switched to tracking branch: [bold]{branch_name}[/bold]")
            return
    except (AttributeError, IndexError):
        pass

    # Branch not found
    console.print(
        Panel(
            f"Branch '{branch_name}' does not exist locally or on origin.\n\n"
            "Please check the branch name or set a different main branch:\n"
            "  git config branch-switch.name <branch-name>",
            title="Branch Not Found",
            border_style="red",
        )
    )
    raise git.exc.GitCommandError("checkout", f"Branch '{branch_name}' not found")


def main() -> None:
    """Main execution flow."""
    # Get repository
    repo = get_repo()
    if repo is None:
        sys.exit(1)

    # Detect main branch
    branch_name = detect_main_branch(repo)
    if branch_name is None:
        sys.exit(1)

    # Check if already on the target branch
    try:
        current_branch = repo.active_branch.name
        if current_branch == branch_name:
            console.print(f"[blue]ℹ[/blue] Already on branch: [bold]{branch_name}[/bold]")
            sys.exit(0)
    except TypeError:
        # Detached HEAD state
        pass

    # Auto-stash changes
    stash_ref = None
    try:
        stash_ref = auto_stash(repo)
    except git.exc.GitCommandError:
        sys.exit(1)

    # Switch branch with stash restoration in finally block
    try:
        switch_to_branch(repo, branch_name)
    except git.exc.GitCommandError:
        # Restore stash before exiting on error
        if stash_ref:
            restore_stash(repo, stash_ref)
        sys.exit(1)
    else:
        # Restore stash after successful switch
        if stash_ref:
            restore_stash(repo, stash_ref)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(Panel(f"Unexpected error: {e}", border_style="red"))
        sys.exit(1)

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "gitpython>=3.1.0",
# ]
# ///
"""
Find commits where currently staged files were modified in a given range.

Usage:
    ./find_related_commits.py <commit_hash>
    ./find_related_commits.py abc123

This script finds all commits between the given commit hash and HEAD
where any of the currently staged files were also modified.
"""

import argparse
import os
import shutil
import subprocess
import sys

import git


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Find commits where staged files were modified in a given range",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s abc123          # Find commits affecting staged files since abc123
  %(prog)s HEAD~10         # Find commits affecting staged files in last 10 commits
        """,
    )

    parser.add_argument(
        "commit_hash",
        help="Base commit hash to search from (exclusive) to HEAD (inclusive)",
    )

    return parser.parse_args()


def find_repo_root() -> git.Repo:
    """
    Auto-detect repository root from current directory.

    Returns:
        git.Repo: GitPython repository object

    Raises:
        SystemExit: If not in a git repository
    """
    try:
        repo = git.Repo(os.getcwd(), search_parent_directories=True)
        return repo
    except git.exc.InvalidGitRepositoryError:
        print("Error: Not in a git repository", file=sys.stderr)
        sys.exit(2)


def get_staged_files(repo: git.Repo) -> list[str]:
    """
    Get the list of files currently staged in the repository index.

    Args:
        repo: GitPython repository object

    Returns:
        List of file paths that are staged (relative to repo root)

    Raises:
        SystemExit: If no files are staged (exit code 1)
    """
    try:
        # Check if HEAD exists
        repo.head.commit
        staged_diffs = repo.index.diff("HEAD")
    except ValueError:
        # Fresh repo with no HEAD - use diff against empty tree
        staged_diffs = repo.index.diff(git.NULL_TREE)

    # Extract file paths - use a_path for deletions, b_path for additions/modifications
    staged_files: set[str] = set()
    for diff in staged_diffs:
        if diff.a_path:
            staged_files.add(diff.a_path)
        if diff.b_path and diff.b_path != diff.a_path:
            staged_files.add(diff.b_path)

    if not staged_files:
        print(
            "Error: No staged files found. Stage files with 'git add' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    return sorted(staged_files)


def get_commits_in_range(repo: git.Repo, base_commit: str) -> list[git.Commit]:
    """
    Get all commits in the range base_commit..HEAD (exclusive of base).

    Args:
        repo: GitPython repository object
        base_commit: The base commit hash or reference (exclusive)

    Returns:
        List of Commit objects from oldest to newest

    Raises:
        SystemExit: If base_commit is invalid (exit code 1)
    """
    try:
        # Validate the base commit exists
        repo.commit(base_commit)
    except git.exc.BadName:
        print(f"Error: Invalid commit reference '{base_commit}'", file=sys.stderr)
        sys.exit(1)

    # Use iter_commits with revision range syntax
    # Format: "base_commit..HEAD" means commits reachable from HEAD but not from base_commit
    revision_range = f"{base_commit}..HEAD"

    try:
        commits = list(repo.iter_commits(revision_range))
    except git.exc.GitCommandError as e:
        print(f"Error: Failed to get commits in range: {e}", file=sys.stderr)
        sys.exit(2)

    # iter_commits returns newest first, reverse for chronological order
    commits.reverse()

    return commits


def get_commit_modified_files(commit: git.Commit) -> set[str]:
    """
    Get the set of files modified in a commit.

    Args:
        commit: GitPython Commit object

    Returns:
        Set of file paths modified in this commit
    """
    # commit.stats.files returns a dict of {filepath: {insertions, deletions, lines}}
    return set(commit.stats.files.keys())


def find_related_commits(
    commits: list[git.Commit],
    staged_files: set[str],
) -> list[tuple[git.Commit, set[str]]]:
    """
    Find commits that modified any of the staged files.

    Args:
        commits: List of commits to search through
        staged_files: Set of staged file paths to look for

    Returns:
        List of tuples: (commit, set of overlapping files)
        Only includes commits with at least one overlapping file.
        Ordered from oldest to newest commit.
    """
    related: list[tuple[git.Commit, set[str]]] = []

    for commit in commits:
        commit_files = get_commit_modified_files(commit)
        overlap = staged_files & commit_files  # Set intersection

        if overlap:
            related.append((commit, overlap))

    return related


def format_output(related_commits: list[tuple[git.Commit, set[str]]]) -> None:
    """
    Print related commits grouped by commit with their overlapping files.

    Output format:
        <commit_hash_short> <subject_line>
          - file1.py
          - file2.py

        <commit_hash_short> <subject_line>
          - file3.py

    Args:
        related_commits: List of (commit, overlapping_files) tuples
    """
    if not related_commits:
        print("No commits found that modified the staged files.")
        return

    for commit, files in related_commits:
        # Short hash (7 chars) + subject line (first line of message)
        short_hash = commit.hexsha[:7]
        subject = commit.message.split("\n", 1)[0]

        print(f"{short_hash} {subject}")
        for filepath in sorted(files):
            print(f"  - {filepath}")
        print()  # Blank line between commits


def find_real_git() -> str:
    """
    Find the real git binary, avoiding any wrappers.

    Returns:
        Path to the real git binary.

    Raises:
        FileNotFoundError: If git binary cannot be found.
    """
    # Common locations for the real git binary
    common_paths = [
        "/usr/bin/git",
        "/usr/local/bin/git",
        "/opt/homebrew/bin/git",  # macOS with Homebrew
    ]

    for path in common_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # Fallback to shutil.which, which might find a wrapper
    git_path = shutil.which("git")
    if git_path:
        return git_path

    raise FileNotFoundError("Could not find git binary")


def run_git_rebase(oldest_commit_hash: str) -> bool:
    """
    Run fixup commit and interactive git rebase with a clean editor configuration.

    Args:
        oldest_commit_hash: The oldest commit hash to rebase from.

    Returns:
        True if rebase was successful, False otherwise.
    """
    try:
        git_bin = find_real_git()

        # Use a clean environment to avoid custom editor wrappers
        env = os.environ.copy()
        # Prefer vi/vim as a simple, reliable editor
        # Check common editor paths
        for editor in ["/usr/bin/vim", "/usr/bin/vi", "/usr/bin/nano"]:
            if os.path.isfile(editor) and os.access(editor, os.X_OK):
                env["GIT_EDITOR"] = editor
                env["GIT_SEQUENCE_EDITOR"] = editor
                break
        else:
            # Fallback to basic vi
            env["GIT_EDITOR"] = "vi"
            env["GIT_SEQUENCE_EDITOR"] = "vi"

        # Remove any editor-related env vars that might interfere
        env.pop("VISUAL", None)
        env.pop("EDITOR", None)

        # Step 1: Create fixup commit for the staged changes
        fixup_cmd = [git_bin, "commit", f"--fixup={oldest_commit_hash}"]
        print(f"\nRunning: {' '.join(fixup_cmd)}")
        result = subprocess.run(fixup_cmd, env=env, check=False)
        if result.returncode != 0:
            print("Error: Failed to create fixup commit.", file=sys.stderr)
            return False

        # Step 2: Run interactive rebase with autosquash
        rebase_cmd = [git_bin, "rebase", "-i", "--autosquash", f"{oldest_commit_hash}^"]
        print(f"Running: {' '.join(rebase_cmd)}")
        result = subprocess.run(rebase_cmd, env=env, check=False)
        return result.returncode == 0
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return False
    except subprocess.SubprocessError as e:
        print(f"Error running git commands: {e}", file=sys.stderr)
        return False


def prompt_run_rebase(oldest_commit_hash: str) -> None:
    """
    Prompt the user to run the interactive rebase.

    Args:
        oldest_commit_hash: The oldest commit hash to rebase from.
    """
    print()
    try:
        response = input("Do you want to run the interactive rebase now? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if response.lower() in ("y", "yes"):
        success = run_git_rebase(oldest_commit_hash)
        if not success:
            print("\nRebase failed or was aborted.", file=sys.stderr)


def print_squash_commands(related_commits: list[tuple[git.Commit, set[str]]]) -> None:
    """
    Print git commands to squash the related commits and offer to run them.

    Args:
        related_commits: List of (commit, overlapping_files) tuples, oldest first
    """
    if not related_commits:
        return

    if len(related_commits) == 1:
        commit = related_commits[0][0]
        print("=" * 60)
        print("To amend this single commit with your staged changes:")
        print("=" * 60)
        print()
        print(f"  git commit --fixup={commit.hexsha[:7]}")
        print(f"  git rebase -i --autosquash {commit.hexsha}^")
        print()
        prompt_run_rebase(commit.hexsha)
        return

    # Multiple commits - provide interactive rebase command
    oldest_commit = related_commits[0][0]
    commit_hashes = [c.hexsha[:7] for c, _ in related_commits]

    print("=" * 60)
    print("Git commands to squash these commits:")
    print("=" * 60)
    print()
    print("Option 1: Interactive rebase (recommended)")
    print("-" * 40)
    print(f"  git rebase -i {oldest_commit.hexsha}^")
    print()
    print("  In the editor, mark these commits as 'squash' or 'fixup':")
    for h in commit_hashes[1:]:  # Skip first, it will be 'pick'
        print(f"    {h} -> squash (or 's')")
    print()
    print("Option 2: Soft reset and recommit")
    print("-" * 40)
    print(f"  git reset --soft {oldest_commit.hexsha}^")
    print("  git commit -m 'Your combined commit message'")
    print()
    print("Option 3: Create fixup commits and autosquash")
    print("-" * 40)
    print(f"  git commit --fixup={oldest_commit.hexsha[:7]}")
    print(f"  git rebase -i --autosquash {oldest_commit.hexsha}^")
    print()

    prompt_run_rebase(oldest_commit.hexsha)


def main() -> int:
    """
    Main entry point.

    Returns:
        Exit code: 0 for success, 1 for user errors, 2 for runtime errors
    """
    args = parse_arguments()
    repo = find_repo_root()

    # Step 1: Get staged files (exits with code 1 if none)
    staged_files = get_staged_files(repo)
    staged_set = set(staged_files)

    # Step 2: Get commits in range (exits with code 1 if invalid commit)
    commits = get_commits_in_range(repo, args.commit_hash)

    if not commits:
        print(f"No commits found in range {args.commit_hash}..HEAD")
        return 0

    # Step 3: Find commits that modified staged files
    related = find_related_commits(commits, staged_set)

    # Step 4: Output results
    format_output(related)

    # Step 5: Print squash commands
    print_squash_commands(related)

    return 0


if __name__ == "__main__":
    sys.exit(main())

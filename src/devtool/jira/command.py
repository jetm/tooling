"""devtool jira -- Jira issue lifecycle management."""

import sys

import click


@click.group()
def jira() -> None:
    """Manage Jira issues from the command line.

    \b
    Examples:
        devtool jira create https://linaro.atlassian.net/browse/IOTIL-100
        devtool jira status in-progress
        devtool jira backfill
    """


@jira.command()
@click.argument("url")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
def create(url: str, verbose: bool) -> None:
    """Create a child issue from a Jira URL and checkout a branch.

    Pass an Epic URL to create a Story, or a Story URL to create a Sub-task.
    If a Sub-task URL is passed, its info is printed and no action is taken.
    """
    import git

    from devtool.common.console import get_console, print_error, setup_logging
    from devtool.jira.client import connect_jira, parse_jira_url
    from devtool.jira.create import create_child_issue

    setup_logging(verbose=verbose)
    console = get_console(plain_text=False)

    # Require git repo
    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print_error(console, "Not in a git repository")
        sys.exit(1)

    # Parse URL
    try:
        project_key, issue_key = parse_jira_url(url)
    except ValueError as e:
        print_error(console, str(e))
        sys.exit(1)

    # Connect to Jira
    try:
        jira_client = connect_jira()
    except (ValueError, RuntimeError) as e:
        print_error(console, str(e))
        sys.exit(1)

    # Fetch issue and determine type
    try:
        issue = jira_client.issue(issue_key)
    except Exception as e:
        print_error(console, f"Issue not found: {issue_key}\n{e}")
        sys.exit(1)

    issue_type = issue.get("fields", {}).get("issuetype", {}).get("name", "Unknown")

    if issue_type == "Sub-task":
        summary = issue.get("fields", {}).get("summary", "")
        issue_status = issue.get("fields", {}).get("status", {}).get("name", "Unknown")
        console.print(f"[bold]{issue_key}[/bold]: {summary}")
        console.print(f"  Type: {issue_type}")
        console.print(f"  Status: {issue_status}")
        console.print("\n[dim]Already a work unit -- nothing to create.[/dim]")
        return

    if issue_type not in ("Epic", "Story"):
        print_error(
            console, f"Unsupported issue type '{issue_type}' for {issue_key}. Expected Epic, Story, or Sub-task."
        )
        sys.exit(1)

    # Create child issue
    child_type = "Story" if issue_type == "Epic" else "Sub-task"
    console.print(f"Creating {child_type} under [bold]{issue_key}[/bold] ({issue_type})...")

    try:
        new_key = create_child_issue(jira_client, project_key, issue_key, issue_type)
    except Exception as e:
        print_error(console, f"Failed to create {child_type}: {e}")
        sys.exit(1)

    console.print(f"[green]Created {new_key}[/green] under {issue_key}")

    # Create and checkout branch
    branch_name = new_key
    if branch_name in [ref.name for ref in repo.branches]:
        print_error(console, f"Branch '{branch_name}' already exists")
        sys.exit(1)

    try:
        repo.git.checkout("-b", branch_name)
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to create branch '{branch_name}': {e}")
        sys.exit(1)

    console.print(f"[green]Checked out branch '{branch_name}'[/green]")


@jira.command()
@click.argument("target", type=click.Choice(["in-progress", "peer-review", "review-staging", "resolved"]))
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
def status(target: str, verbose: bool) -> None:
    """Transition the current branch's Jira issue to a new status.

    \b
    Supported statuses:
        in-progress      -> "In Progress"
        peer-review      -> "Peer Review"
        review-staging   -> "Review Staging" (requires passing pipeline)
        resolved         -> "Resolved"
    """
    import git

    from devtool.common.console import get_console, print_error, setup_logging
    from devtool.common.git import extract_issue_key
    from devtool.jira.status import STATUS_MAP, check_merge_pipeline, transition_jira_issue

    setup_logging(verbose=verbose)
    console = get_console(plain_text=False)

    # Get issue key from branch
    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print_error(console, "Not in a git repository")
        sys.exit(1)

    try:
        branch_name = repo.active_branch.name
    except TypeError:
        print_error(console, "Not on a branch (detached HEAD state)")
        sys.exit(1)

    issue_key = extract_issue_key(branch_name)
    if not issue_key:
        print_error(console, f"No Jira issue key found in branch name '{branch_name}'")
        sys.exit(1)

    jira_status = STATUS_MAP[target]

    # Pipeline check for review-staging
    if target == "review-staging":
        try:
            passed, message = check_merge_pipeline(branch_name)
        except RuntimeError as e:
            print_error(console, str(e))
            sys.exit(1)
        if not passed:
            print_error(console, f"Cannot transition to Review Staging: {message}")
            sys.exit(1)

    # Execute transition
    try:
        transition_jira_issue(issue_key, jira_status)
    except (ValueError, RuntimeError) as e:
        print_error(console, str(e))
        sys.exit(1)
    except Exception as e:
        print_error(console, f"Transition to '{jira_status}' not available for {issue_key}: {e}")
        sys.exit(1)

    console.print(f"[green]{issue_key}[/green] -> {jira_status}")


@jira.command()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
def backfill(verbose: bool) -> None:
    """AI-generate a problem-focused Jira description from the current diff.

    Reads the diff between the current branch and the target branch,
    uses Claude to generate a problem-focused summary, and updates
    the Jira issue's title and description.
    """
    import git

    from devtool.common.console import (
        check_claude_cli,
        get_console,
        print_error,
        setup_logging,
    )
    from devtool.common.git import extract_issue_key, get_target_branch_from_config
    from devtool.jira.backfill import backfill_jira_issue

    setup_logging(verbose=verbose)
    console = get_console(plain_text=False)

    # Check dependencies
    from devtool.common.config import get_config

    config = get_config()
    if not config.openrouter_api_key:
        cli_version = check_claude_cli(console)
        if cli_version is None:
            sys.exit(1)

    try:
        repo = git.Repo(search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print_error(console, "Not in a git repository")
        sys.exit(1)

    try:
        branch_name = repo.active_branch.name
    except TypeError:
        print_error(console, "Not on a branch (detached HEAD state)")
        sys.exit(1)

    issue_key = extract_issue_key(branch_name)
    if not issue_key:
        print_error(console, f"No Jira issue key found in branch name '{branch_name}'")
        sys.exit(1)

    # Compute diff against target branch (same logic as mr-create)
    target_branch = get_target_branch_from_config(repo)
    if not target_branch:
        print_error(
            console,
            "No target branch configured.\nRun 'devtool switch-main' to detect and cache the main branch.",
        )
        sys.exit(1)

    log_base = None
    for ref in [f"origin/{target_branch}", target_branch]:
        try:
            repo.git.rev_parse("--verify", ref)
            log_base = ref
            break
        except git.exc.GitCommandError:
            continue

    if not log_base:
        print_error(console, f"Could not find base ref: 'origin/{target_branch}' or '{target_branch}'")
        sys.exit(1)

    try:
        diff = repo.git.diff(log_base, branch_name)
    except git.exc.GitCommandError as e:
        print_error(console, f"Failed to compute diff: {e}")
        sys.exit(1)

    if not diff.strip():
        print_error(console, f"No changes found between '{log_base}' and '{branch_name}'")
        sys.exit(1)

    result = backfill_jira_issue(issue_key, diff, str(repo.working_dir), console)
    if not result:
        sys.exit(1)

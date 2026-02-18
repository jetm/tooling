"""devtool doctor — diagnostic checks for devtool dependencies."""

import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)


def _check_executable(
    name: str,
    console: object,
    record_check: object,
    *,
    required: bool = True,
    record_name: str | None = None,
    install_hint: str | None = None,
) -> str | None:
    """Check if an executable exists and can report its version.

    Returns the version string on success, None on failure.
    """
    check_name = record_name or name

    console.print(f"Checking {name}... ", end="")
    if not shutil.which(name):
        if required:
            console.print("[red]✗ Not found[/red]")
        else:
            console.print("[yellow]✗ Not found[/yellow]")
        if install_hint:
            console.print(f"  [yellow]{install_hint}[/yellow]")
        record_check(check_name, False, "Not found")
        return None

    try:
        result = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            version = result.stdout.strip().split("\n")[0]
            console.print(f"[green]✓[/green] {version}")
            record_check(check_name, True, version)
            return version
        console.print("[red]✗ Failed to get version[/red]")
        if install_hint:
            console.print(f"  [yellow]{install_hint}[/yellow]")
        record_check(check_name, False, "Failed to get version")
        return None
    except subprocess.TimeoutExpired:
        console.print("[red]✗ Timed out[/red]")
        record_check(check_name, False, "Timed out")
        return None
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
        record_check(check_name, False, str(e))
        return None


@click.command()
@click.option("--full", is_flag=True, help="Run full diagnostics including live API test")
@click.option("--export", is_flag=True, help="Export diagnostic info for sharing (sanitized)")
@click.option("--plain-text", is_flag=True, help="Output plain text without formatting")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug logging")
@click.pass_context
def doctor(ctx: click.Context, full: bool, export: bool, plain_text: bool, verbose: bool) -> None:
    """Run diagnostic checks for devtool dependencies.

    Checks: git, glab, Claude CLI, authentication, claude-agent-sdk,
    network connectivity, configuration file, environment variables.

    Use --full to also test the actual Claude API with a simple query.
    Use --export to generate a sanitized diagnostic report for sharing.
    """
    from devtool.common.config import get_config
    from devtool.common.console import get_console, setup_logging
    from devtool.common.errors import check_network_connectivity

    setup_logging(verbose=verbose)
    console = get_console(plain_text)

    console.print("[bold]devtool Diagnostic Report[/bold]\n")

    all_passed = True
    diagnostic_info: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "checks": {},
    }

    def record_check(name: str, passed: bool, details: str) -> None:
        diagnostic_info["checks"][name] = {"passed": passed, "details": details}

    # Check git
    if _check_executable("git", console, record_check) is None:
        all_passed = False

    # Check glab (optional — only needed for mr-desc)
    _check_executable("glab", console, record_check, required=False)

    # Check Claude Code CLI
    cli_version = _check_executable(
        "claude",
        console,
        record_check,
        record_name="claude_cli",
        install_hint="Install from https://claude.ai/download",
    )
    if cli_version is None:
        all_passed = False

    # Check authentication
    console.print("Checking Claude Code CLI auth... ", end="")
    has_api_key = os.environ.get("ANTHROPIC_API_KEY") is not None
    credentials_file = Path.home() / ".claude" / ".credentials.json"
    has_credentials_file = credentials_file.exists()

    if has_api_key:
        console.print("[green]✓[/green] Authenticated (via API key)")
        record_check("authentication", True, "API key")
    elif has_credentials_file:
        try:
            with open(credentials_file) as f:
                creds = json.load(f)
            if creds:
                console.print("[green]✓[/green] Authenticated (via credentials file)")
                record_check("authentication", True, "credentials file")
            else:
                console.print("[yellow]⚠[/yellow] Credentials file exists but appears empty")
                record_check("authentication", False, "Empty credentials")
                all_passed = False
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Could not validate credentials: {e}")
            record_check("authentication", False, str(e))
            all_passed = False
    else:
        console.print("[red]✗ Not authenticated[/red]")
        console.print("  [yellow]Run 'claude' and sign in, or set ANTHROPIC_API_KEY[/yellow]")
        record_check("authentication", False, "Not authenticated")
        all_passed = False

    # Check claude-agent-sdk
    console.print("Checking claude-agent-sdk... ", end="")
    try:
        from importlib.metadata import version

        sdk_version = version("claude-agent-sdk")
        console.print(f"[green]✓[/green] {sdk_version}")
        record_check("claude_agent_sdk", True, sdk_version)
    except Exception:
        console.print("[red]✗ Not found[/red]")
        console.print("  [yellow]Install with: uv tool install -e . --force[/yellow]")
        record_check("claude_agent_sdk", False, "Not found")
        all_passed = False

    # Check network connectivity
    console.print("Checking network connectivity... ", end="")
    connected, network_error = check_network_connectivity()
    if connected:
        console.print("[green]✓[/green] api.anthropic.com reachable")
        record_check("network", True, "Reachable")
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
                tomllib.load(f)
            console.print(f"[green]✓[/green] Config file found ({config_path})")
            config = get_config()
            console.print(f"    Default model: {config.default_model}")
            console.print(f"    Timeout: {config.timeout}s")
            console.print(f"    Compression: {'enabled' if config.diff_compression_enabled else 'disabled'}")
            console.print(f"    Strategy: {config.diff_compression_strategy}")
            record_check("configuration", True, str(config_path))
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Config file has errors: {e}")
            record_check("configuration", False, str(e))
    else:
        console.print("[blue]ℹ[/blue] No config file (using defaults)")
        config = get_config()
        console.print(f"    Default model: {config.default_model} (default)")
        console.print(f"    Timeout: {config.timeout}s (default)")
        record_check("configuration", True, "Using defaults")

    # Check environment variables
    console.print("Checking environment variables... ", end="")
    env_vars = {
        "ACA_TIMEOUT": os.environ.get("ACA_TIMEOUT"),
        "ACA_RETRY_ATTEMPTS": os.environ.get("ACA_RETRY_ATTEMPTS"),
        "ACA_LOG_LEVEL": os.environ.get("ACA_LOG_LEVEL"),
        "ACA_DEFAULT_MODEL": os.environ.get("ACA_DEFAULT_MODEL"),
        "ACA_DIFF_COMPRESSION_ENABLED": os.environ.get("ACA_DIFF_COMPRESSION_ENABLED"),
        "ACA_DIFF_COMPRESSION_STRATEGY": os.environ.get("ACA_DIFF_COMPRESSION_STRATEGY"),
        "SKIP_PRECOMMIT": os.environ.get("SKIP_PRECOMMIT"),
    }
    active_env = {k: v for k, v in env_vars.items() if v is not None}
    if active_env:
        console.print(f"[green]✓[/green] {len(active_env)} override(s) active")
        for k, v in active_env.items():
            console.print(f"    {k}={v}")
        record_check("environment", True, str(active_env))
    else:
        console.print("[blue]ℹ[/blue] No overrides set")
        record_check("environment", True, "No overrides")

    # Full diagnostics: live API test
    if full:
        console.print("Running live API test... ", end="")
        try:
            from devtool.common.claude import generate_with_progress

            response = generate_with_progress(
                console=console,
                prompt="Reply with exactly: OK",
                cwd=str(Path.cwd()),
                message="Testing Claude API...",
                model="haiku",
            )
            if response and response.strip():
                console.print(f"[green]✓[/green] Claude responded: {response.strip()[:50]}")
                record_check("live_api_test", True, "OK")
            else:
                console.print("[red]✗ Empty response[/red]")
                record_check("live_api_test", False, "Empty response")
                all_passed = False
        except Exception as e:
            console.print(f"[red]✗ {e}[/red]")
            record_check("live_api_test", False, str(e))
            all_passed = False

    # Export diagnostics
    if export:
        console.print("\n[bold]Diagnostic Export:[/bold]")
        export_path = Path.home() / "devtool-diagnostics.json"
        try:
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

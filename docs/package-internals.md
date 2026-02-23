# Package Internals (`devtool.common`)

Shared modules used across devtool commands. Located in `src/devtool/common/`.

## Modules

### `config.py` — Configuration

- **`DevtoolConfig`** — Dataclass holding all config values (model, timeouts, compression thresholds)
- **`get_config()`** — Load/cache config from `~/.config/devtool/config.toml` and env vars

### `errors.py` — Error Handling & Retry

- **`DevtoolError`** — Base exception with context, cause, and troubleshooting steps
- **`ErrorContext`** — Structured error context (command, exit_code, stderr)
- Specialized: `ClaudeAuthenticationError`, `ClaudeNetworkError`, `ClaudeCLIError`, `ClaudeTimeoutError`, `ClaudeRateLimitError`, `ClaudeContentError`
- **`retry_with_backoff()`** — Decorator with exponential backoff and jitter
- **`RETRYABLE_EXCEPTIONS`** — Tuple of exceptions that trigger retry
- **`collect_error_context()`** — Gather context for error reporting
- **`check_network_connectivity()`** — Test connection to Anthropic API

### `console.py` — Console & Logging

- **`setup_logging()`** — Configure logging based on verbosity
- **`get_console()`** — Create Rich console (plain or formatted)
- **`print_output()`** — Print text, optionally as Markdown
- **`print_error()`** — Consistent error formatting
- **`check_dependency()`** — Check if an executable exists in PATH
- **`check_claude_cli()`** — Verify Claude CLI is installed and authenticated
- **`check_version_compatibility()`** — Validate Claude CLI version
- **`get_precommit_skip_env()`** — Check SKIP_PRECOMMIT environment variable

### `claude.py` — Claude Agent SDK Integration

- **`generate_with_claude()`** — Async wrapper with retry and timeout
- **`generate_with_progress()`** — Sync wrapper with spinner UI
- **`should_use_file_based_prompt()`** — Check if prompt exceeds size threshold
- **`create_file_based_prompt()`** — Write large prompts to temp files for file-based delivery
- **`cleanup_temp_prompt_file()`** — Clean up temp prompt files
- Model selection: `"sonnet"`, `"opus"`, or `"haiku"` (configurable via `default_model` in config or `DT_DEFAULT_MODEL` env var)

### `git.py` — Git Utilities

- **`edit_in_editor()`** — Open content in user's editor ($EDITOR / $VISUAL / config)
- **`extract_ticket_number()`** — Extract IOTIL ticket number from branch name
- **`strip_markdown_code_blocks()`** — Remove markdown code block wrappers
- **`get_target_branch_from_config()`** — Read MR target branch from git config
- **`handle_generation_error()`** — Error recovery with retry/template/abort options

## Usage Pattern

```python
from devtool.common.config import get_config
from devtool.common.claude import generate_with_progress
from devtool.common.console import check_claude_cli, get_console

console = get_console(plain_text=False)
check_claude_cli(console)
config = get_config()

result = generate_with_progress(
    console=console,
    prompt="your prompt here",
    cwd="/path/to/repo",
    model=config.default_model,
)
```

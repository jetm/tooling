# Common Utilities (`common_utils.py`)

Shared utility module imported by Claude-powered CLI tools (`aca.py`, `lx.py`).

## Main Categories

### Configuration
- **`ACAConfig`** — Dataclass holding all config values
- **`get_config()`** — Load/cache config from `~/.config/aca/config.toml` and env vars
- **`setup_logging()`** — Configure logging based on verbosity

### Error Handling
- **`ACAError`** — Base exception with context, cause, and troubleshooting steps
- **`ErrorContext`** — Structured error context (command, exit_code, stderr)
- Specialized: `ClaudeAuthenticationError`, `ClaudeNetworkError`, `ClaudeCLIError`, `ClaudeTimeoutError`, `ClaudeRateLimitError`, `ClaudeContentError`

### Retry Logic
- **`retry_with_backoff()`** — Decorator with exponential backoff and jitter
- **`RETRYABLE_EXCEPTIONS`** — Tuple of exceptions that trigger retry

### Dependency Validation
- **`check_claude_cli()`** — Verify Claude CLI is installed and authenticated
- **`check_dependency()`** — Check if an executable exists in PATH
- **`check_network_connectivity()`** — Test connection to Anthropic API

### Console Utilities
- **`get_console()`** — Create Rich console (plain or formatted)
- **`print_output()`** — Print text, optionally as Markdown
- **`print_error()`** — Consistent error formatting

### Claude SDK Integration
- **`generate_with_claude()`** — Async wrapper with retry and timeout
- **`generate_with_progress()`** — Sync wrapper with spinner UI
- Model selection: `"sonnet"`, `"opus"`, or `"haiku"` (configurable via `default_model` in config or `ACA_DEFAULT_MODEL` env var)

## Usage Pattern

```python
from common_utils import get_config, generate_with_progress, check_claude_cli

# Validate dependencies
check_claude_cli()

# Load config
config = get_config()

# Generate with Claude
result = generate_with_progress(
    prompt="your prompt here",
    system_prompt="system instructions",
    model="sonnet",
)
```

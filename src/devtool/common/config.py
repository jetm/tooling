"""Configuration loading from file and environment variables."""

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Threshold for switching to file-based delivery (50KB default)
# Conservative to avoid ARG_MAX issues with environment variables
PROMPT_SIZE_THRESHOLD_FOR_FILE = 50 * 1024

VALID_STRATEGIES = {"stat", "compact", "filtered", "function-context", "smart"}

_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def _load_int_env(env_name: str, current: int) -> int:
    """Load an integer from an environment variable, warning on invalid values."""
    if value := os.environ.get(env_name):
        try:
            return int(value)
        except ValueError:
            logger.warning(f"Invalid {env_name} value: {value}")
    return current


def _load_bool_env(env_name: str, current: bool) -> bool:
    """Load a boolean from an environment variable."""
    if (value := os.environ.get(env_name)) is not None:
        return value.lower() in _TRUTHY_VALUES
    return current


def _load_str_env(env_name: str, current: str) -> str:
    """Load a string from an environment variable."""
    if value := os.environ.get(env_name):
        return value
    return current


@dataclass
class ACAConfig:
    """ACA configuration loaded from config file and environment."""

    retry_attempts: int = 3
    initial_delay: float = 2.0
    backoff_factor: float = 2.0
    max_delay: float = 30.0
    timeout: int = 120
    log_level: str = "WARNING"
    editor: str | None = None
    default_model: str = "sonnet"
    commit_model: str = "haiku"
    # Diff compression settings
    diff_size_threshold_bytes: int = 50_000
    diff_files_threshold: int = 100
    diff_compression_enabled: bool = True
    diff_compression_strategy: str = "compact"
    # Smart compression settings
    diff_max_priority_files: int = 15
    diff_token_limit: int = 100_000
    diff_smart_priority_enabled: bool = True
    # Prompt file-based delivery settings
    prompt_file_threshold_bytes: int = 50_000
    prompt_file_enabled: bool = True

    def _load_from_toml(self, data: dict) -> None:
        """Apply values from parsed TOML data."""
        self.retry_attempts = data.get("retry_attempts", self.retry_attempts)
        self.initial_delay = data.get("initial_delay", self.initial_delay)
        self.backoff_factor = data.get("backoff_factor", self.backoff_factor)
        self.max_delay = data.get("max_delay", self.max_delay)
        self.timeout = data.get("timeout", self.timeout)
        self.log_level = data.get("log_level", self.log_level)
        self.editor = data.get("editor", self.editor)
        self.default_model = data.get("default_model", self.default_model)
        self.commit_model = data.get("commit_model", self.commit_model)
        self.diff_size_threshold_bytes = data.get("diff_size_threshold_bytes", self.diff_size_threshold_bytes)
        self.diff_files_threshold = data.get("diff_files_threshold", self.diff_files_threshold)
        self.diff_compression_enabled = data.get("diff_compression_enabled", self.diff_compression_enabled)
        self.diff_max_priority_files = data.get("diff_max_priority_files", self.diff_max_priority_files)
        self.diff_token_limit = data.get("diff_token_limit", self.diff_token_limit)
        self.diff_smart_priority_enabled = data.get("diff_smart_priority_enabled", self.diff_smart_priority_enabled)
        self.prompt_file_threshold_bytes = data.get("prompt_file_threshold_bytes", self.prompt_file_threshold_bytes)
        self.prompt_file_enabled = data.get("prompt_file_enabled", self.prompt_file_enabled)

        # Strategy requires validation
        strategy = data.get("diff_compression_strategy", self.diff_compression_strategy)
        if strategy in VALID_STRATEGIES:
            self.diff_compression_strategy = strategy
        else:
            logger.warning(
                f"Invalid diff_compression_strategy '{strategy}' in config, "
                f"using default 'compact'. Valid options: {VALID_STRATEGIES}"
            )

    def _load_from_env(self) -> None:
        """Apply environment variable overrides."""
        self.timeout = _load_int_env("ACA_TIMEOUT", self.timeout)
        self.retry_attempts = _load_int_env("ACA_RETRY_ATTEMPTS", self.retry_attempts)
        self.log_level = _load_str_env("ACA_LOG_LEVEL", self.log_level).upper()
        self.default_model = _load_str_env("ACA_DEFAULT_MODEL", self.default_model)
        self.commit_model = _load_str_env("ACA_COMMIT_MODEL", self.commit_model)

        # Diff compression
        self.diff_size_threshold_bytes = _load_int_env("ACA_DIFF_SIZE_THRESHOLD", self.diff_size_threshold_bytes)
        self.diff_files_threshold = _load_int_env("ACA_DIFF_FILES_THRESHOLD", self.diff_files_threshold)

        # Compression enabled: long form takes precedence over short form
        env_long = os.environ.get("ACA_DIFF_COMPRESSION_ENABLED")
        env_short = os.environ.get("ACA_DIFF_COMPRESSION")
        if env_long is not None:
            self.diff_compression_enabled = env_long.lower() in _TRUTHY_VALUES
        elif env_short is not None:
            self.diff_compression_enabled = env_short.lower() in _TRUTHY_VALUES

        # Strategy with validation
        if env_strategy := os.environ.get("ACA_DIFF_COMPRESSION_STRATEGY"):
            if env_strategy in VALID_STRATEGIES:
                self.diff_compression_strategy = env_strategy
            else:
                logger.warning(
                    f"Invalid ACA_DIFF_COMPRESSION_STRATEGY '{env_strategy}', "
                    f"using default 'compact'. Valid options: {VALID_STRATEGIES}"
                )

        # Smart compression
        self.diff_max_priority_files = _load_int_env("ACA_DIFF_MAX_PRIORITY_FILES", self.diff_max_priority_files)
        self.diff_token_limit = _load_int_env("ACA_DIFF_TOKEN_LIMIT", self.diff_token_limit)
        self.diff_smart_priority_enabled = _load_bool_env(
            "ACA_DIFF_SMART_PRIORITY_ENABLED", self.diff_smart_priority_enabled
        )

        # Prompt file-based delivery
        self.prompt_file_threshold_bytes = _load_int_env("ACA_PROMPT_FILE_THRESHOLD", self.prompt_file_threshold_bytes)
        self.prompt_file_enabled = _load_bool_env("ACA_PROMPT_FILE_ENABLED", self.prompt_file_enabled)

    def _validate(self) -> None:
        """Validate and clamp configuration values."""
        if self.diff_max_priority_files < 1 or self.diff_max_priority_files > 50:
            logger.warning(
                f"diff_max_priority_files={self.diff_max_priority_files} outside valid range "
                f"[1, 50], clamping to valid range"
            )
            self.diff_max_priority_files = max(1, min(50, self.diff_max_priority_files))

        if self.diff_token_limit < 10_000:
            logger.warning(f"diff_token_limit={self.diff_token_limit} too small (minimum 10000), using default 100000")
            self.diff_token_limit = 100_000

        if self.prompt_file_threshold_bytes < 10_000:
            logger.warning(
                f"prompt_file_threshold_bytes={self.prompt_file_threshold_bytes} too small "
                f"(minimum 10000), using default 50000"
            )
            self.prompt_file_threshold_bytes = 50_000

    @classmethod
    def load(cls) -> ACAConfig:
        """Load configuration from file and environment variables."""
        config = cls()

        config_path = Path.home() / ".config" / "aca" / "config.toml"
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
                config._load_from_toml(data)
            except Exception as e:
                logger.warning(f"Failed to load config file {config_path}: {e}")

        config._load_from_env()
        config._validate()
        return config


_config: ACAConfig | None = None


def get_config() -> ACAConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = ACAConfig.load()
    return _config

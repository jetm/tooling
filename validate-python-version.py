#!/usr/bin/env python3
"""
Validate Python version in uv scripts.

This script validates that all Python scripts in the repository use the correct
uv script format and the latest stable Python version. It is intended to be used
as a pre-commit hook.

Usage:
    python3 validate-python-version.py <file1.py> [file2.py ...]

The script checks:
1. Shebang matches #!/usr/bin/env -S uv run --script
2. The requires-python version in the # /// script block matches the latest stable version
"""

import re
import subprocess
import sys
from pathlib import Path

# Cache for the detected Python version
_cached_python_version: str | None = None


def get_latest_python_version() -> str:
    """
    Query uv for the latest stable Python version.

    Returns the latest stable Python version available via uv.
    Caches the result to avoid repeated subprocess calls.

    Raises:
        RuntimeError: If uv is not available or no stable version found.
    """
    global _cached_python_version

    if _cached_python_version is not None:
        return _cached_python_version

    try:
        result = subprocess.run(
            ["uv", "python", "list", "--all-versions"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "uv is not installed. Please install uv: https://docs.astral.sh/uv/getting-started/installation/"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timed out querying uv for Python versions")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to query uv for Python versions: {e.stderr}")

    # Parse version numbers from uv output
    # uv outputs lines like: cpython-3.14.0a3-linux-x86_64-gnu (pre-release)
    # or: cpython-3.14.2-linux-x86_64-gnu (stable)
    # Pre-release pattern: version followed by a/b/rc and digits (e.g., 3.15.0a4)
    prerelease_pattern = re.compile(r"cpython-\d+\.\d+\.\d+(?:a|b|rc)\d+")
    version_pattern = re.compile(r"cpython-(\d+\.\d+)(?:\.\d+)?-")

    versions: set[str] = set()
    for line in result.stdout.splitlines():
        # Skip pre-release versions (alpha, beta, rc) and freethreaded builds
        if prerelease_pattern.search(line) or "freethreaded" in line:
            continue

        match = version_pattern.search(line)
        if match:
            versions.add(match.group(1))

    if not versions:
        raise RuntimeError(
            "No stable Python versions found via uv. "
            "Please ensure uv is working correctly."
        )

    # Sort versions and get the highest one
    sorted_versions = sorted(
        versions,
        key=lambda v: tuple(int(x) for x in v.split(".")),
        reverse=True,
    )

    _cached_python_version = sorted_versions[0]
    return _cached_python_version


def parse_script_metadata(filepath: Path) -> dict[str, str | None]:
    """
    Parse the script metadata from a Python file.

    Reads the first 20 lines to extract shebang and requires-python from
    the # /// script block.

    Args:
        filepath: Path to the Python file.

    Returns:
        Dictionary with 'shebang' and 'requires_python' keys.
    """
    result: dict[str, str | None] = {
        "shebang": None,
        "requires_python": None,
    }

    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    lines = content.splitlines()[:20]

    # Extract shebang (first line)
    if lines and lines[0].startswith("#!"):
        result["shebang"] = lines[0]

    # Extract requires-python from # /// script block
    in_script_block = False
    for line in lines:
        if line.strip() == "# /// script":
            in_script_block = True
            continue
        if in_script_block:
            if line.strip() == "# ///":
                break
            # Look for requires-python = ">=X.Y"
            match = re.match(r'#\s*requires-python\s*=\s*"([^"]+)"', line)
            if match:
                result["requires_python"] = match.group(1)
                break

    return result


def is_uv_script(filepath: Path) -> bool:
    """
    Check if a file is a uv script (has the uv script metadata block).

    Args:
        filepath: Path to the Python file.

    Returns:
        True if the file contains a uv script metadata block.
    """
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    # Look for the script metadata block as a standalone line (not in a string/comment)
    # The block starts with "# /// script" on its own line
    for line in content.splitlines():
        if line.strip() == "# /// script":
            return True
    return False


def validate_file(filepath: Path, expected_version: str) -> list[str] | None:
    """
    Validate a Python file's shebang and requires-python version.

    Args:
        filepath: Path to the Python file.
        expected_version: Expected Python version (e.g., "3.14").

    Returns:
        List of error messages (empty if validation passes), or None if the file
        is not a uv script and should be skipped.
    """
    # Skip files that are not uv scripts
    if not is_uv_script(filepath):
        return None

    errors: list[str] = []
    expected_shebang = "#!/usr/bin/env -S uv run --script"
    expected_requires_python = f">={expected_version}"

    metadata = parse_script_metadata(filepath)

    # Validate shebang
    if metadata["shebang"] is None:
        errors.append(f"  Missing shebang. Expected: {expected_shebang}")
    elif metadata["shebang"] != expected_shebang:
        errors.append(f"  Expected shebang: {expected_shebang}")
        errors.append(f"  Found: {metadata['shebang']}")

    # Validate requires-python
    if metadata["requires_python"] is None:
        errors.append(
            f"  Missing requires-python in # /// script block. Expected: {expected_requires_python}"
        )
    elif metadata["requires_python"] != expected_requires_python:
        errors.append(f"  Expected requires-python: {expected_requires_python}")
        errors.append(f"  Found: {metadata['requires_python']}")

    return errors


def main(files: list[str]) -> int:
    """
    Main entry point for the validation script.

    Args:
        files: List of Python file paths to validate.

    Returns:
        Exit code (0 if all files pass, 1 if any fail).
    """
    if not files:
        print("No files to validate.")
        return 0

    # Get the latest Python version from uv
    try:
        expected_version = get_latest_python_version()
    except RuntimeError as e:
        print(f"❌ {e}")
        return 1

    failed_files: dict[str, list[str]] = {}

    for file_path in files:
        filepath = Path(file_path)

        if not filepath.exists():
            failed_files[file_path] = [f"  File not found: {file_path}"]
            continue

        errors = validate_file(filepath, expected_version)
        if errors is None:
            # Not a uv script, skip
            continue
        if errors:
            failed_files[file_path] = errors

    if failed_files:
        print(f"❌ Python version validation failed for {len(failed_files)} file(s):\n")

        for file_path, errors in failed_files.items():
            print(f"File: {file_path}")
            for error in errors:
                print(error)
            print()

        expected_requires_python = f">={expected_version}"
        print("To fix:")
        print("1. Update shebang to: #!/usr/bin/env -S uv run --script")
        print(
            f"2. Update requires-python to: {expected_requires_python} in the # /// script block"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

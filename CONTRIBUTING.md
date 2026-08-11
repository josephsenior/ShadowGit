# Contributing to ShadowGit

Thank you for helping make filesystem checkpoints safer and more portable.

## Development setup

1. Create a Python 3.12+ virtual environment.
2. Run `python -m pip install -e ".[test]"`.
3. Run `ruff check src tests`, `ruff format --check src tests`, and `pytest`.

Changes to snapshot or restore behavior should include round-trip tests. Tests
that depend on POSIX modes or symlink privileges must use an explicit platform
skip rather than weakening the assertion.

## Compatibility

Public APIs and JSON fields should remain backward compatible within a major
version. A pruning change must document whether commit SHAs are rewritten.

## Pull requests

Keep changes focused, explain the failure mode being addressed, and include a
test that would have caught it. By contributing, you agree that your work is
licensed under the repository's MIT License.

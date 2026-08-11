"""Git-compatible ignore matching for ShadowGit snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pathspec

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    '.git/',
    '.shadowgit/',
    '.venv/',
    'venv/',
    'env/',
    '.mypy_cache/',
    '.pytest_cache/',
    '.ruff_cache/',
    '__pycache__/',
    'node_modules/',
    'logs/',
    'build/',
    'dist/',
    '*.pyc',
    '*.pyo',
    '*.pyd',
    '.DS_Store',
)


def build_pathspec(
    workspace_root: str | Path,
    *,
    default_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
) -> pathspec.PathSpec:
    """Build a GitWildMatch pathspec from defaults and repository excludes."""
    root = Path(workspace_root).resolve()
    lines = list(default_patterns)
    for source in (root / '.gitignore', root / '.git' / 'info' / 'exclude'):
        try:
            if source.is_file():
                lines.extend(
                    source.read_text(encoding='utf-8', errors='ignore').splitlines()
                )
        except OSError:
            continue
    return pathspec.GitIgnoreSpec.from_lines(lines)


def build_ignore_matcher(
    workspace_root: str | Path,
    *,
    default_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
):
    """Return a ``(relative_path, is_dir) -> bool`` snapshot ignore callback."""
    spec = build_pathspec(workspace_root, default_patterns=default_patterns)

    def ignored(relative_path: str, is_dir: bool) -> bool:
        candidate = relative_path.rstrip('/') + ('/' if is_dir else '')
        return spec.match_file(candidate)

    return ignored


__all__ = [
    'DEFAULT_IGNORE_PATTERNS',
    'build_ignore_matcher',
    'build_pathspec',
]

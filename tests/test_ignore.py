from __future__ import annotations

from pathlib import Path

from shadowgit import build_ignore_matcher


def test_ignore_matcher_combines_safe_defaults_and_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("custom-cache/\n*.secret\n", encoding="utf-8")
    ignored = build_ignore_matcher(tmp_path)

    assert ignored(".git", True)
    assert ignored(".shadowgit", True)
    assert ignored("node_modules", True)
    assert ignored("custom-cache", True)
    assert ignored("nested/token.secret", False)
    assert not ignored("src/app.py", False)

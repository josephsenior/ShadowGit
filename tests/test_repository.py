from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pygit2
import pytest
from shadowgit import ShadowRepo, ShadowRepoError

pytest.importorskip('pygit2')


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'app.py').write_bytes(b"print('one')\r\n")
    (workspace / 'src').mkdir()
    (workspace / 'src' / 'util.py').write_text('VALUE = 1\n', encoding='utf-8')
    return workspace


def test_snapshot_list_inspect_and_verify(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace, tmp_path / 'store')

    sha = repo.snapshot('baseline')

    snapshots = repo.list_snapshots()
    assert [item.sha for item in snapshots] == [sha]
    assert snapshots[0].label == 'baseline'
    assert snapshots[0].file_count == 2
    assert repo.inspect(sha) == snapshots[0]
    assert repo.verify().valid is True
    assert repo.verify().snapshots_checked == 1


def test_diff_reports_added_modified_and_deleted_paths(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace)
    before = repo.snapshot('before')

    (workspace / 'app.py').write_text("print('two')\n", encoding='utf-8')
    (workspace / 'src' / 'util.py').unlink()
    (workspace / 'new.txt').write_text('new\n', encoding='utf-8')
    after = repo.snapshot('after')

    diff = repo.diff(before, after)
    assert diff.added == ('new.txt',)
    assert diff.modified == ('app.py',)
    assert diff.deleted == ('src/util.py',)


def test_restore_is_byte_exact_and_quarantines_extras(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace)
    sha = repo.snapshot('baseline')

    (workspace / 'app.py').write_bytes(b'changed\n')
    (workspace / 'extra.txt').write_text('preserve me', encoding='utf-8')
    quarantine = repo.restore(sha)

    assert (workspace / 'app.py').read_bytes() == b"print('one')\r\n"
    assert quarantine is not None
    assert (quarantine / 'extra.txt').read_text(encoding='utf-8') == 'preserve me'


def test_default_store_is_never_snapshotted_or_restored(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace)
    sha = repo.snapshot()

    assert repo.store_path == workspace / '.shadowgit' / 'shadow_repo'
    assert not any(path.startswith('.shadowgit/') for path in repo.files(sha))
    marker = repo.store_path / 'marker'
    marker.write_text('safe', encoding='utf-8')
    repo.restore(sha)
    assert marker.read_text(encoding='utf-8') == 'safe'


def test_custom_store_inside_workspace_is_never_snapshotted(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace, workspace / '.private-checkpoints')

    sha = repo.snapshot()

    assert not any(path.startswith('.private-checkpoints/') for path in repo.files(sha))


def test_store_initialization_never_discovers_a_parent_repository(
    tmp_path: Path,
) -> None:
    parent = tmp_path / 'parent'
    parent.mkdir()
    parent_repo = pygit2.init_repository(str(parent), bare=False)
    workspace = parent / 'workspace'
    workspace.mkdir()
    (workspace / 'file.txt').write_text('content', encoding='utf-8')

    repo = ShadowRepo(workspace)
    sha = repo.snapshot('isolated')

    assert repo.store_path == workspace / '.shadowgit' / 'shadow_repo'
    assert repo.inspect(sha).file_count == 1
    assert parent_repo.references.get('refs/heads/shadow') is None


def test_custom_ignore_callback_controls_snapshot_contents(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / 'ignored.log').write_text('noise', encoding='utf-8')
    repo = ShadowRepo(
        workspace,
        ignore=lambda path, _is_dir: path.endswith('.log'),
    )

    sha = repo.snapshot()

    assert 'ignored.log' not in repo.files(sha)


def test_unknown_snapshot_has_domain_error(tmp_path: Path) -> None:
    repo = ShadowRepo(_workspace(tmp_path))
    with pytest.raises(ShadowRepoError, match='Snapshot not found'):
        repo.inspect('a' * 40)


def test_snapshot_and_restore_symbolic_link(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    link = workspace / 'app-link.py'
    try:
        link.symlink_to('app.py')
    except OSError as exc:
        pytest.skip(f'symlink creation unavailable: {exc}')
    repo = ShadowRepo(workspace)

    sha = repo.snapshot('with link')
    link.unlink()
    link.write_text('not a link', encoding='utf-8')
    repo.restore(sha)

    assert link.is_symlink()
    assert os.readlink(link) == 'app.py'
    assert link.read_bytes() == b"print('one')\r\n"


def test_restore_never_writes_through_symlinked_parent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    nested = workspace / 'nested'
    nested.mkdir()
    (nested / 'inside.txt').write_text('snapshot data', encoding='utf-8')
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'inside.txt'
    sentinel.write_text('outside data', encoding='utf-8')
    repo = ShadowRepo(workspace)
    sha = repo.snapshot()
    shutil.rmtree(nested)
    try:
        nested.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'symlink creation unavailable: {exc}')

    quarantine = repo.restore(sha)

    assert not nested.is_symlink()
    assert (nested / 'inside.txt').read_text(encoding='utf-8') == 'snapshot data'
    assert sentinel.read_text(encoding='utf-8') == 'outside data'
    assert quarantine is not None
    assert (quarantine / 'nested').is_symlink()


@pytest.mark.skipif(os.name == 'nt', reason='Windows has no portable executable bit')
def test_snapshot_restore_and_diff_preserve_executable_mode(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    script = workspace / 'run.sh'
    script.write_text('#!/bin/sh\n', encoding='utf-8')
    script.chmod(0o755)
    repo = ShadowRepo(workspace)

    executable = repo.snapshot('executable')
    script.chmod(0o644)
    plain = repo.snapshot('plain')

    assert repo.diff(executable, plain).modified == ('run.sh',)
    repo.restore(executable)
    assert script.stat().st_mode & stat.S_IXUSR
    repo.restore(plain)
    assert not script.stat().st_mode & stat.S_IXUSR


def test_restore_leaves_ignored_files_untouched(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ignored = workspace / 'runtime.log'
    ignored.write_text('before', encoding='utf-8')
    repo = ShadowRepo(workspace, ignore=lambda path, _is_dir: path.endswith('.log'))
    sha = repo.snapshot()
    ignored.write_text('after', encoding='utf-8')

    assert repo.restore(sha) is None
    assert ignored.read_text(encoding='utf-8') == 'after'


def test_interrupted_restore_can_recover_pre_restore_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace)
    baseline = repo.snapshot('baseline')
    (workspace / 'app.py').write_text('pre-restore working state', encoding='utf-8')

    def interrupt(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(repo, '_restore_entry', interrupt)
    with pytest.raises(KeyboardInterrupt):
        repo.restore(baseline)

    recovery = repo.pending_recovery
    assert recovery is not None
    with pytest.raises(ShadowRepoError, match='requires recovery first'):
        repo.snapshot()

    (workspace / 'app.py').write_text('partially restored damage', encoding='utf-8')
    reopened = ShadowRepo(workspace)
    assert reopened.pending_recovery == recovery
    reopened.recover()

    assert reopened.pending_recovery is None
    assert (workspace / 'app.py').read_text(
        encoding='utf-8'
    ) == 'pre-restore working state'


def test_quarantine_failure_fails_closed_and_preserves_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace)
    baseline = repo.snapshot()
    extra = workspace / 'extra.txt'
    extra.write_text('must survive', encoding='utf-8')

    def fail_move(*_args, **_kwargs) -> None:
        raise OSError('simulated move failure')

    monkeypatch.setattr(shutil, 'move', fail_move)
    with pytest.raises(ShadowRepoError, match='Restore was interrupted'):
        repo.restore(baseline)

    assert extra.read_text(encoding='utf-8') == 'must survive'
    assert repo.pending_recovery is not None
    repo.recover()
    assert repo.pending_recovery is None


def test_prune_rewrites_only_retained_snapshot_history(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = ShadowRepo(workspace)
    first = repo.snapshot('first')
    (workspace / 'app.py').write_text('second version', encoding='utf-8')
    second = repo.snapshot('second')
    (workspace / 'app.py').write_text('third version', encoding='utf-8')
    third = repo.snapshot('third')

    result = repo.prune({first, third})

    assert result.snapshots_before == 3
    assert result.snapshots_after == 2
    assert result.removed == 1
    assert set(result.rewritten) == {first, third}
    retained = repo.list_snapshots()
    assert [item.label for item in retained] == ['third', 'first']
    assert [item.sha for item in retained] == [
        result.rewritten[third],
        result.rewritten[first],
    ]
    assert second not in {item.sha for item in retained}
    assert repo.verify().valid


def test_prune_rejects_unknown_sha_and_can_clear_history(tmp_path: Path) -> None:
    repo = ShadowRepo(_workspace(tmp_path))
    repo.snapshot()
    with pytest.raises(ShadowRepoError, match='unknown snapshot'):
        repo.prune({'f' * 40})

    result = repo.prune(set())
    assert result.snapshots_after == 0
    assert repo.list_snapshots() == []

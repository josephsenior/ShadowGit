"""pygit2-backed shadow repository for fast, unified workspace checkpoints.

Provides a private bare git object-store (``ShadowRepo``) that lives in
``~/.grinta/workspaces/<id>/rollback/shadow_repo/`` -- completely independent of any ``.git`` the
workspace project may or may not have.  Every checkpoint is a pygit2
commit; no subprocess is ever spawned.

Key design decisions
--------------------
* **Stat-cache** (``_stat_cache`` dict) -- ``snapshot()`` calls
  ``os.stat()`` on every workspace file but only re-reads and re-hashes
  the blob when ``(mtime_ns, size)`` changed.  Unchanged files reuse
  the previously stored OID from the cached tree.  The cache is persisted
  as a JSON sidecar so it survives process restarts.
* **No line-ending normalisation** -- ``core.autocrlf`` is forced to
  ``false`` so CRLF content on Windows is stored and restored byte-for-byte.
* **Cross-platform** -- pygit2 ships precompiled wheels (with libgit2
  statically bundled) for Windows (x86/x64/arm64), macOS (Intel + Apple
  Silicon) and Linux (manylinux/musllinux, x86_64/aarch64/ppc64le).
  No C compiler or system ``git`` binary is required.
* **Filesystem fidelity** -- regular/executable files and symbolic links use
  Git's native tree modes and are restored without following workspace links.
* **Thread-safety** -- a ``threading.Lock`` guards index and stat-cache.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Reserved workspace roots that must never be snapshotted or touched
# during restore -- must stay in sync with workspace_checkpoint._RESERVED_ROOTS.
_RESERVED_ROOTS: frozenset[str] = frozenset({'.git', '.shadowgit'})

_STAT_CACHE_FILENAME = 'stat_cache.json'
_RESTORE_JOURNAL_FILENAME = 'restore_journal.json'
_SHADOW_DIR_NAME = 'shadow_repo'
_SHADOW_REF = 'refs/heads/shadow'
_RECOVERY_REF = 'refs/shadowgit/recovery'


class ShadowRepoError(RuntimeError):
    """Raised when a shadow-repo operation fails unrecoverably."""


@dataclass(frozen=True)
class SnapshotInfo:
    """Metadata describing one immutable workspace snapshot."""

    sha: str
    label: str
    timestamp: int
    file_count: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotDiff:
    """Path-level content changes between two snapshots."""

    before: str
    after: str
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]

    def to_dict(self) -> dict[str, str | list[str]]:
        return {
            'before': self.before,
            'after': self.after,
            'added': list(self.added),
            'modified': list(self.modified),
            'deleted': list(self.deleted),
        }


@dataclass(frozen=True)
class VerificationResult:
    """Integrity result for the reachable ShadowGit snapshot history."""

    valid: bool
    snapshots_checked: int
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, bool | int | list[str]]:
        return {
            'valid': self.valid,
            'snapshots_checked': self.snapshots_checked,
            'errors': list(self.errors),
        }


@dataclass(frozen=True)
class RestoreRecovery:
    """Durable record of a restore that did not complete."""

    target_sha: str
    backup_sha: str
    quarantine_dir: str
    started_at: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class PruneResult:
    """Result of rewriting the retained snapshot history."""

    snapshots_before: int
    snapshots_after: int
    removed: int
    rewritten: dict[str, str]

    def to_dict(self) -> dict[str, int | dict[str, str]]:
        return {
            'snapshots_before': self.snapshots_before,
            'snapshots_after': self.snapshots_after,
            'removed': self.removed,
            'rewritten': dict(self.rewritten),
        }


@dataclass(frozen=True)
class _SnapshotEntry:
    data: bytes
    mode: int


class ShadowRepo:
    """Private in-process git object store for workspace checkpoints.

    Args:
        workspace_root: Absolute path to the workspace being snapshotted.
        shadow_dir: Directory that will hold the bare pygit2 repository and
            the stat-cache sidecar.  Defaults to
            ``<workspace_root>/.grinta/shadow_repo``.

    Example::

        repo = ShadowRepo(workspace_root="/my/project")
        sha = repo.snapshot(label="before change")
        # ... agent makes changes ...
        repo.restore(sha)

    """

    def __init__(
        self,
        workspace_root: str | Path,
        shadow_dir: str | Path | None = None,
        *,
        ignore: Callable[[str, bool], bool] | None = None,
        reserved_roots: Iterable[str] = (),
    ) -> None:
        import pygit2  # local import keeps module importable even if pygit2 absent

        self._pygit2 = pygit2
        self._workspace_root = Path(workspace_root).resolve()
        self._shadow_dir = (
            Path(shadow_dir).resolve()
            if shadow_dir is not None
            else self._workspace_root / '.shadowgit' / _SHADOW_DIR_NAME
        )
        self._ignore = ignore
        self._reserved_roots = _RESERVED_ROOTS | frozenset(reserved_roots)
        self._shadow_dir.mkdir(parents=True, exist_ok=True)

        self._stat_cache_path = self._shadow_dir / _STAT_CACHE_FILENAME
        self._stat_cache: dict[str, tuple[int, int, int]] = self._load_stat_cache()
        self._blob_cache: dict[str, tuple[Any, int]] = {}
        self._restore_journal_path = self._shadow_dir / _RESTORE_JOURNAL_FILENAME

        self._lock = threading.Lock()
        self._repo = self._open_or_init_repo()
        logger.debug('ShadowRepo ready at %s', self._shadow_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def workspace_root(self) -> Path:
        """Absolute directory whose contents are snapshotted."""
        return self._workspace_root

    @property
    def store_path(self) -> Path:
        """Absolute path of the private bare Git object store."""
        return self._shadow_dir

    def snapshot(self, label: str = '') -> str:
        """Snapshot the current workspace state and return a commit SHA.

        Only files whose ``(mtime_ns, size)`` changed since the last call
        are re-read and re-hashed -- everything else reuses the cached blob
        OID.  This makes incremental snapshots very fast even for large repos.

        Args:
            label: Optional human-readable label embedded in the commit message.

        Returns:
            Hex commit SHA string (40 chars).

        Raises:
            ShadowRepoError: If the pygit2 commit creation fails.

        """
        with self._lock:
            self._require_no_pending_recovery('create a snapshot')
            try:
                return self._snapshot_locked(label)
            except self._pygit2.GitError as exc:
                raise ShadowRepoError(f'pygit2 snapshot failed: {exc}') from exc

    def restore(
        self,
        commit_sha: str,
        *,
        quarantine_dir: str | Path | None = None,
    ) -> Path | None:
        """Restore workspace to the state captured in *commit_sha*.

        Files absent from the snapshot are **quarantined** (moved aside)
        rather than deleted. Reserved roots such as ``.git`` and
        ``.shadowgit`` are never touched.

        Args:
            commit_sha: SHA of a commit previously returned by :meth:`snapshot`.
            quarantine_dir: Directory to move extra files into.  If *None*,
                a timestamped sibling inside ``.grinta`` is created.

        Returns:
            Path to the quarantine directory, or *None* if nothing was quarantined.

        Raises:
            ShadowRepoError: If the commit SHA cannot be resolved or restore fails.

        """
        with self._lock:
            self._require_no_pending_recovery('start another restore')
            try:
                return self._restore_locked(commit_sha, quarantine_dir=quarantine_dir)
            except self._pygit2.GitError as exc:
                raise ShadowRepoError(f'pygit2 restore failed: {exc}') from exc

    @property
    def pending_recovery(self) -> RestoreRecovery | None:
        """Return an interrupted-restore record, if one exists."""
        return self._load_restore_journal()

    def recover(self) -> Path | None:
        """Roll back an interrupted restore to its pre-restore snapshot.

        Recovery is explicit: opening a repository never mutates the workspace.
        Until recovery completes, snapshot, restore, and prune operations fail
        closed so the durable backup cannot accidentally become unreachable.
        """
        with self._lock:
            recovery = self._load_restore_journal()
            if recovery is None:
                return None
            quarantine = Path(recovery.quarantine_dir)
            try:
                result = self._apply_snapshot(
                    recovery.backup_sha,
                    quarantine_dir=quarantine,
                )
            except Exception as exc:  # noqa: BLE001
                raise ShadowRepoError(
                    f'Restore recovery failed; journal preserved: {exc}'
                ) from exc
            self._finish_restore_transaction()
            return result

    def prune(self, keep_shas: set[str]) -> PruneResult:
        """Retain only selected snapshots by rewriting their parent chain.

        Git commits include their parent IDs, so removing snapshots from the
        middle of a linear history necessarily changes the SHAs of descendants.
        The returned ``rewritten`` mapping makes that change explicit.

        Args:
            keep_shas: Snapshot SHAs from the current shadow history to retain.

        """
        with self._lock:
            self._require_no_pending_recovery('prune snapshots')
            snapshots = self.list_snapshots()
            known = {snapshot.sha for snapshot in snapshots}
            unknown = keep_shas - known
            if unknown:
                raise ShadowRepoError(
                    f'Cannot prune: unknown snapshot SHA(s): {", ".join(sorted(unknown))}'
                )

            if not keep_shas:
                ref = self._repo.references.get(_SHADOW_REF)
                if ref is not None:
                    ref.delete()
                self._stat_cache = {}
                self._blob_cache = {}
                self._persist_stat_cache({})
                return PruneResult(len(snapshots), 0, len(snapshots), {})

            retained = [
                self._resolve_commit(item.sha)
                for item in reversed(snapshots)
                if item.sha in keep_shas
            ]
            rewritten: dict[str, str] = {}
            parent_ids: list[Any] = []
            for commit in retained:
                new_oid = self._repo.create_commit(
                    None,
                    commit.author,
                    commit.committer,
                    commit.message or '',
                    commit.tree_id,
                    parent_ids,
                )
                rewritten[str(commit.id)] = str(new_oid)
                parent_ids = [new_oid]

            ref = self._repo.references.get(_SHADOW_REF)
            if ref is None:
                self._repo.references.create(_SHADOW_REF, parent_ids[0])
            else:
                ref.set_target(parent_ids[0])
            self._reload_head_cache()
            return PruneResult(
                snapshots_before=len(snapshots),
                snapshots_after=len(retained),
                removed=len(snapshots) - len(retained),
                rewritten=rewritten,
            )

    def list_snapshots(self, *, limit: int | None = None) -> list[SnapshotInfo]:
        """Return snapshots from newest to oldest along the shadow branch."""
        snapshots: list[SnapshotInfo] = []
        ref = self._repo.references.get(_SHADOW_REF)
        if ref is None:
            return snapshots
        commit = ref.peel(self._pygit2.Commit)
        while commit is not None and (limit is None or len(snapshots) < limit):
            tree = commit.peel(self._pygit2.Tree)
            snapshots.append(
                SnapshotInfo(
                    sha=str(commit.id),
                    label=self._label_from_message(commit.message or ''),
                    timestamp=int(commit.commit_time),
                    file_count=self._count_tree_files(tree),
                )
            )
            commit = commit.parents[0] if commit.parents else None
        return snapshots

    def inspect(self, commit_sha: str) -> SnapshotInfo:
        """Return metadata for one snapshot SHA."""
        commit = self._resolve_commit(commit_sha)
        tree = commit.peel(self._pygit2.Tree)
        return SnapshotInfo(
            sha=str(commit.id),
            label=self._label_from_message(commit.message or ''),
            timestamp=int(commit.commit_time),
            file_count=self._count_tree_files(tree),
        )

    def files(self, commit_sha: str) -> tuple[str, ...]:
        """Return the sorted relative file paths stored in a snapshot."""
        return tuple(sorted(self._snapshot_file_oids(commit_sha)))

    def diff(self, before_sha: str, after_sha: str) -> SnapshotDiff:
        """Return path-level changes between two snapshots."""
        before = self._snapshot_file_oids(before_sha)
        after = self._snapshot_file_oids(after_sha)
        before_paths = set(before)
        after_paths = set(after)
        return SnapshotDiff(
            before=before_sha,
            after=after_sha,
            added=tuple(sorted(after_paths - before_paths)),
            modified=tuple(
                sorted(
                    path
                    for path in before_paths & after_paths
                    if before[path] != after[path]
                )
            ),
            deleted=tuple(sorted(before_paths - after_paths)),
        )

    def verify(self) -> VerificationResult:
        """Verify that every reachable commit, tree, and blob can be read."""
        errors: list[str] = []
        snapshots = self.list_snapshots()
        for snapshot in snapshots:
            try:
                self._snapshot_file_oids(snapshot.sha)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{snapshot.sha}: {exc}')
        return VerificationResult(
            valid=not errors,
            snapshots_checked=len(snapshots),
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------
    # Snapshot internals
    # ------------------------------------------------------------------

    def _snapshot_locked(self, label: str) -> str:
        """Core snapshot logic -- must be called with ``_lock`` held."""
        return self._create_snapshot_commit(label, update_ref=_SHADOW_REF)

    def _create_snapshot_commit(self, label: str, *, update_ref: str) -> str:
        """Create a snapshot commit at *update_ref*."""
        repo = self._repo
        pygit2 = self._pygit2

        index = pygit2.Index()
        new_cache: dict[str, tuple[int, int, int]] = {}

        for abs_path_str, rel_posix in self._iter_workspace_entries():
            try:
                st = os.lstat(abs_path_str)
            except OSError:
                continue

            mtime_ns = st.st_mtime_ns
            size = st.st_size
            mode = self._git_filemode(st.st_mode)
            new_cache[rel_posix] = (mtime_ns, size, mode)

            cached = self._stat_cache.get(rel_posix)
            if cached is not None and cached == (mtime_ns, size, mode):
                blob_entry = self._blob_cache.get(rel_posix)
                if blob_entry is not None and blob_entry[1] == mode:
                    entry = pygit2.IndexEntry(rel_posix, blob_entry[0], mode)
                    index.add(entry)
                    continue

            try:
                if mode == pygit2.GIT_FILEMODE_LINK:
                    data = os.readlink(os.fsencode(abs_path_str))
                else:
                    # Explicit bytes avoid Git filters and newline conversion.
                    data = Path(abs_path_str).read_bytes()
                blob_oid = repo.create_blob(data)
            except (pygit2.GitError, OSError) as exc:
                logger.warning(
                    'Skipping file %s in shadow snapshot: %s', rel_posix, exc
                )
                continue

            entry = pygit2.IndexEntry(rel_posix, blob_oid, mode)
            index.add(entry)

        self._stat_cache = new_cache
        self._persist_stat_cache(new_cache)

        tree_oid = index.write_tree(repo)

        parents: list[Any] = []
        try:
            head_ref = repo.references.get(_SHADOW_REF)
            if head_ref is not None:
                parents = [head_ref.peel(pygit2.Commit).id]
        except Exception:  # noqa: BLE001
            pass

        sig = pygit2.Signature('ShadowGit', 'snapshot@shadowgit.local')
        msg = f'[shadowgit] {label}' if label else '[shadowgit] snapshot'
        commit_oid = repo.create_commit(
            update_ref,
            sig,
            sig,
            msg,
            tree_oid,
            parents,
        )
        commit_sha = str(commit_oid)

        # Refresh blob cache from the committed tree for future stat-cache hits.
        try:
            tree = repo.get(str(tree_oid))
            self._blob_cache = {}
            if tree is not None:
                self._collect_entries(tree, '', self._blob_cache)
        except Exception:  # noqa: BLE001
            self._blob_cache = {}

        logger.debug('Shadow snapshot created: %s', commit_sha)
        return commit_sha

    # ------------------------------------------------------------------
    # Restore internals
    # ------------------------------------------------------------------

    def _restore_locked(
        self,
        commit_sha: str,
        *,
        quarantine_dir: str | Path | None,
    ) -> Path | None:
        """Core restore logic -- must be called with ``_lock`` held."""
        target = self._resolve_commit(commit_sha)
        if quarantine_dir is None:
            transaction_id = f'{int(time.time() * 1000)}-{os.getpid()}'
            qdir = self._shadow_dir.parent / f'restore_quarantine_{transaction_id}'
        else:
            qdir = Path(quarantine_dir).expanduser().resolve()

        backup_sha = self._create_snapshot_commit(
            f'recovery before restore {str(target.id)[:12]}',
            update_ref=_RECOVERY_REF,
        )
        recovery = RestoreRecovery(
            target_sha=str(target.id),
            backup_sha=backup_sha,
            quarantine_dir=str(qdir),
            started_at=int(time.time()),
        )
        try:
            self._persist_restore_journal(recovery)
        except Exception:
            recovery_ref = self._repo.references.get(_RECOVERY_REF)
            if recovery_ref is not None:
                recovery_ref.delete()
            raise
        try:
            result = self._apply_snapshot(str(target.id), quarantine_dir=qdir)
        except Exception as exc:  # noqa: BLE001
            raise ShadowRepoError(
                'Restore was interrupted; run recover() or `shadowgit recover` '
                f'before continuing: {exc}'
            ) from exc
        self._finish_restore_transaction()
        return result

    def _apply_snapshot(
        self,
        commit_sha: str,
        *,
        quarantine_dir: Path,
    ) -> Path | None:
        commit = self._resolve_commit(commit_sha)
        tree = commit.peel(self._pygit2.Tree)
        entries: dict[str, _SnapshotEntry] = {}
        self._walk_tree(tree, '', entries, self._repo)
        used_quarantine = self._quarantine_extras(entries, quarantine_dir)
        for rel_posix, entry in entries.items():
            prepared_quarantine = self._prepare_destination(
                rel_posix,
                entry,
                quarantine_dir,
            )
            if prepared_quarantine is not None:
                used_quarantine = prepared_quarantine
            self._restore_entry(self._workspace_root / rel_posix, entry)
        self._stat_cache = {}
        self._blob_cache = {}
        self._persist_stat_cache({})
        return used_quarantine

    def _walk_tree(
        self,
        tree: Any,
        prefix: str,
        out: dict[str, _SnapshotEntry],
        repo: Any,
    ) -> None:
        """Recursively collect snapshot entries from a tree."""
        for entry in tree:
            rel = f'{prefix}{entry.name}' if prefix else entry.name
            if entry.type_str == 'blob':
                blob = repo.get(entry.id)
                if blob is not None:
                    out[rel] = _SnapshotEntry(bytes(blob.data), int(entry.filemode))
            elif entry.type_str == 'tree':
                subtree = repo.get(entry.id)
                if subtree is not None:
                    self._walk_tree(subtree, f'{rel}/', out, repo)

    def _resolve_commit(self, commit_sha: str) -> Any:
        try:
            commit = self._repo.get(commit_sha)
        except Exception as exc:  # noqa: BLE001
            raise ShadowRepoError(
                f'Cannot resolve snapshot {commit_sha!r}: {exc}'
            ) from exc
        if commit is None:
            raise ShadowRepoError(f'Snapshot not found: {commit_sha!r}')
        try:
            return commit.peel(self._pygit2.Commit)
        except Exception as exc:  # noqa: BLE001
            raise ShadowRepoError(f'Object is not a snapshot: {commit_sha!r}') from exc

    def _snapshot_file_oids(self, commit_sha: str) -> dict[str, tuple[str, int]]:
        commit = self._resolve_commit(commit_sha)
        tree = commit.peel(self._pygit2.Tree)
        files: dict[str, tuple[str, int]] = {}

        def walk(current: Any, prefix: str = '') -> None:
            for entry in current:
                rel = f'{prefix}{entry.name}' if prefix else entry.name
                if entry.type_str == 'blob':
                    blob = self._repo.get(entry.id)
                    if blob is None:
                        raise ShadowRepoError(f'Missing blob {entry.id} for {rel}')
                    files[rel] = (str(entry.id), int(entry.filemode))
                elif entry.type_str == 'tree':
                    subtree = self._repo.get(entry.id)
                    if subtree is None:
                        raise ShadowRepoError(f'Missing tree {entry.id} for {rel}')
                    walk(subtree, f'{rel}/')

        walk(tree)
        return files

    def _count_tree_files(self, tree: Any) -> int:
        count = 0
        for entry in tree:
            if entry.type_str == 'blob':
                count += 1
            elif entry.type_str == 'tree':
                subtree = self._repo.get(entry.id)
                if subtree is not None:
                    count += self._count_tree_files(subtree)
        return count

    @staticmethod
    def _label_from_message(message: str) -> str:
        value = message.strip()
        for prefix in ('[shadowgit] ', '[Grinta] '):
            if value.startswith(prefix):
                return value[len(prefix) :]
        if value in {'[shadowgit] snapshot', '[Grinta] snapshot'}:
            return 'snapshot'
        return value

    def _quarantine_extras(
        self,
        snapshot_files: dict[str, _SnapshotEntry],
        quarantine_dir: Path | None,
    ) -> Path | None:
        """Move workspace files not in *snapshot_files* to *quarantine_dir*."""
        snapshot_posix: set[str] = set(snapshot_files.keys())
        used_quarantine: Path | None = None

        for item in sorted(
            self._workspace_root.rglob('*'),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            if not os.path.lexists(item):
                continue
            if self._is_reserved(item):
                continue
            try:
                rel = item.relative_to(self._workspace_root)
            except ValueError:
                continue
            rel_posix = rel.as_posix()

            if self._is_ignored(
                rel_posix, item.is_dir() and not self._is_linklike(item)
            ):
                continue

            if item.is_dir() and not self._is_linklike(item):
                has_child = any(sp.startswith(f'{rel_posix}/') for sp in snapshot_posix)
                if not has_child and rel_posix not in snapshot_posix:
                    quarantine_dir = self._move_to_quarantine(item, rel, quarantine_dir)
                    used_quarantine = quarantine_dir
                continue

            if rel_posix not in snapshot_posix:
                quarantine_dir = self._move_to_quarantine(item, rel, quarantine_dir)
                used_quarantine = quarantine_dir

        return used_quarantine

    def _prepare_destination(
        self,
        rel_posix: str,
        entry: _SnapshotEntry,
        quarantine_dir: Path,
    ) -> Path | None:
        """Ensure destination parents cannot redirect writes outside workspace."""
        rel = Path(rel_posix)
        current = self._workspace_root
        used_quarantine: Path | None = None
        for part in rel.parts[:-1]:
            current /= part
            if os.path.lexists(current) and (
                self._is_linklike(current) or not current.is_dir()
            ):
                parent_rel = current.relative_to(self._workspace_root)
                self._move_to_quarantine(current, parent_rel, quarantine_dir)
                used_quarantine = quarantine_dir
            current.mkdir(exist_ok=True)

        dest = self._workspace_root / rel
        if not os.path.lexists(dest):
            return used_quarantine
        wants_link = entry.mode == self._pygit2.GIT_FILEMODE_LINK
        type_matches = (
            dest.is_symlink()
            if wants_link
            else dest.is_file() and not self._is_linklike(dest)
        )
        if not type_matches:
            self._move_to_quarantine(dest, rel, quarantine_dir)
            used_quarantine = quarantine_dir
        return used_quarantine

    def _restore_entry(self, dest: Path, entry: _SnapshotEntry) -> None:
        """Restore one Git tree entry without following existing symlinks."""
        if entry.mode == self._pygit2.GIT_FILEMODE_LINK:
            target = os.fsdecode(entry.data)
            temp = dest.with_name(f'.{dest.name}.shadowgit-link-{os.getpid()}')
            if os.path.lexists(temp):
                temp.unlink()
            try:
                target_is_directory = (dest.parent / target).is_dir()
                os.symlink(target, temp, target_is_directory=target_is_directory)
                os.replace(temp, dest)
            finally:
                if os.path.lexists(temp):
                    temp.unlink()
            return

        self._atomic_write(dest, entry.data)
        if os.name != 'nt':
            current_mode = stat.S_IMODE(dest.stat().st_mode)
            if entry.mode == self._pygit2.GIT_FILEMODE_BLOB_EXECUTABLE:
                dest.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            else:
                dest.chmod(current_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    def _move_to_quarantine(
        self,
        source: Path,
        rel: Path,
        quarantine_dir: Path | None,
    ) -> Path:
        if quarantine_dir is None:
            ts = int(time.time())
            quarantine_dir = self._shadow_dir.parent / f'restore_quarantine_{ts}'
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            target = target.with_name(f'{target.name}.{int(time.time() * 1000)}')
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:
            raise ShadowRepoError(f'Failed to quarantine {source}: {exc}') from exc
        return quarantine_dir

    # ------------------------------------------------------------------
    # Blob cache helpers
    # ------------------------------------------------------------------

    def _collect_entries(
        self,
        tree: Any,
        prefix: str,
        out: dict[str, tuple[Any, int]],
    ) -> None:
        """Recursively collect ``{path: (blob_oid, mode)}`` from a tree."""
        for entry in tree:
            rel = f'{prefix}{entry.name}' if prefix else entry.name
            if entry.type_str == 'blob':
                out[rel] = (entry.id, int(entry.filemode))
            elif entry.type_str == 'tree':
                subtree = self._repo.get(entry.id)
                if subtree is not None:
                    self._collect_entries(subtree, f'{rel}/', out)

    # ------------------------------------------------------------------
    # Repo init
    # ------------------------------------------------------------------

    def _open_or_init_repo(self) -> Any:
        """Open an existing shadow repo or initialise a fresh bare one."""
        pygit2 = self._pygit2
        repo_path = str(self._shadow_dir)
        head_path = self._shadow_dir / 'HEAD'
        objects_path = self._shadow_dir / 'objects'
        if head_path.is_file() and objects_path.is_dir():
            repo = pygit2.Repository(repo_path)
            logger.debug('Opened existing shadow repo at %s', repo_path)
        else:
            repo = pygit2.init_repository(repo_path, bare=True)
            # Disable line-ending normalisation so CRLF files are round-tripped
            # byte-for-byte on Windows.
            repo.config['core.autocrlf'] = 'false'
            logger.debug('Initialised new shadow repo at %s', repo_path)

        # Seed blob cache from existing shadow HEAD so the very first snapshot
        # after a process restart can still use the stat-cache.
        try:
            ref = repo.references.get(_SHADOW_REF)
            if ref is not None:
                tree = ref.peel(pygit2.Commit).peel(pygit2.Tree)
                self._collect_entries(tree, '', self._blob_cache)
        except Exception:  # noqa: BLE001
            pass

        return repo

    # ------------------------------------------------------------------
    # File iteration
    # ------------------------------------------------------------------

    def _iter_workspace_entries(self):
        """Yield regular files and symlinks without traversing directory links."""
        root = str(self._workspace_root)

        for dirpath, dirnames, filenames in os.walk(root):
            kept_dirs: list[str] = []
            for dirname in dirnames:
                abs_dir = os.path.join(dirpath, dirname)
                rel_dir = Path(abs_dir).relative_to(self._workspace_root)
                if self._is_shadow_store_path(Path(abs_dir)):
                    continue
                if rel_dir.parts and rel_dir.parts[0] in self._reserved_roots:
                    continue
                if self._is_ignored(rel_dir.as_posix(), True):
                    continue
                if os.path.islink(abs_dir):
                    yield abs_dir, rel_dir.as_posix()
                    continue
                if os.path.isjunction(abs_dir):
                    logger.warning('Skipping Windows junction in snapshot: %s', rel_dir)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                if self._is_shadow_store_path(Path(abs_path)):
                    continue
                try:
                    rel = Path(abs_path).relative_to(self._workspace_root)
                except ValueError:
                    continue
                if rel.parts and rel.parts[0] in self._reserved_roots:
                    continue
                if self._is_ignored(rel.as_posix(), False):
                    continue
                yield abs_path, rel.as_posix()

    def _is_ignored(self, relative_path: str, is_dir: bool) -> bool:
        if self._ignore is None:
            return False
        try:
            return bool(self._ignore(relative_path, is_dir))
        except Exception as exc:  # noqa: BLE001
            raise ShadowRepoError(
                f'Ignore callback failed for {relative_path!r}: {exc}'
            ) from exc

    @staticmethod
    def _is_linklike(path: Path) -> bool:
        """Return whether *path* redirects traversal to another location."""
        return path.is_symlink() or path.is_junction()

    # ------------------------------------------------------------------
    # Stat-cache persistence
    # ------------------------------------------------------------------

    def _load_stat_cache(self) -> dict[str, tuple[int, int, int]]:
        if not self._stat_cache_path.exists():
            return {}
        try:
            raw: dict[str, list[int]] = json.loads(
                self._stat_cache_path.read_text(encoding='utf-8')
            )
            return {
                k: (int(v[0]), int(v[1]), int(v[2]))
                for k, v in raw.items()
                if len(v) == 3
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning('Failed to load shadow stat cache: %s', exc)
            return {}

    def _persist_stat_cache(self, cache: dict[str, tuple[int, int, int]]) -> None:
        try:
            payload = json.dumps(
                {k: list(v) for k, v in cache.items()},
                separators=(',', ':'),
            )
            tmp = self._stat_cache_path.with_suffix('.tmp')
            tmp.write_text(payload, encoding='utf-8')
            os.replace(str(tmp), str(self._stat_cache_path))
        except OSError as exc:
            logger.warning('Failed to persist shadow stat cache: %s', exc)

    # ------------------------------------------------------------------
    # Restore transaction persistence
    # ------------------------------------------------------------------

    def _require_no_pending_recovery(self, operation: str) -> None:
        recovery = self._load_restore_journal()
        if recovery is not None:
            raise ShadowRepoError(
                f'Cannot {operation}: an interrupted restore targeting '
                f'{recovery.target_sha} requires recovery first'
            )

    def _load_restore_journal(self) -> RestoreRecovery | None:
        if not self._restore_journal_path.exists():
            return None
        try:
            payload = json.loads(self._restore_journal_path.read_text(encoding='utf-8'))
            return RestoreRecovery(
                target_sha=str(payload['target_sha']),
                backup_sha=str(payload['backup_sha']),
                quarantine_dir=str(payload['quarantine_dir']),
                started_at=int(payload['started_at']),
            )
        except Exception as exc:  # noqa: BLE001
            raise ShadowRepoError(f'Restore journal is corrupt: {exc}') from exc

    def _persist_restore_journal(self, recovery: RestoreRecovery) -> None:
        payload = json.dumps(recovery.to_dict(), separators=(',', ':'))
        temp = self._restore_journal_path.with_suffix('.tmp')
        try:
            with temp.open('w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._restore_journal_path)
        finally:
            temp.unlink(missing_ok=True)

    def _finish_restore_transaction(self) -> None:
        self._restore_journal_path.unlink(missing_ok=True)
        recovery_ref = self._repo.references.get(_RECOVERY_REF)
        if recovery_ref is not None:
            recovery_ref.delete()

    def _reload_head_cache(self) -> None:
        self._stat_cache = {}
        self._blob_cache = {}
        ref = self._repo.references.get(_SHADOW_REF)
        if ref is not None:
            tree = ref.peel(self._pygit2.Commit).peel(self._pygit2.Tree)
            self._collect_entries(tree, '', self._blob_cache)
        self._persist_stat_cache({})

    def _git_filemode(self, os_mode: int) -> int:
        if stat.S_ISLNK(os_mode):
            return self._pygit2.GIT_FILEMODE_LINK
        if os.name != 'nt' and os_mode & stat.S_IXUSR:
            return self._pygit2.GIT_FILEMODE_BLOB_EXECUTABLE
        return self._pygit2.GIT_FILEMODE_BLOB

    # ------------------------------------------------------------------
    # Reserved-path guard
    # ------------------------------------------------------------------

    def _is_reserved(self, path: Path) -> bool:
        """Return True for reserved paths without resolving symlink targets."""
        try:
            absolute = Path(os.path.abspath(path))
            rel = absolute.relative_to(self._workspace_root)
        except ValueError:
            return True
        if rel.parts and rel.parts[0] in self._reserved_roots:
            return True
        # Belt-and-suspenders: explicitly protect the shadow dir.
        try:
            absolute.relative_to(self._shadow_dir)
            return True
        except ValueError:
            pass
        return False

    def _is_shadow_store_path(self, path: Path) -> bool:
        """Return whether a lexical workspace path belongs to the object store."""
        try:
            Path(os.path.abspath(path)).relative_to(self._shadow_dir)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Atomic file write
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write(dest: Path, data: bytes) -> None:
        """Write *data* to *dest* atomically via a temp file + rename."""
        import tempfile

        fd, tmp_name = tempfile.mkstemp(
            prefix=f'.{dest.name}.',
            suffix='.tmp',
            dir=str(dest.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(data)
            os.replace(str(tmp_path), str(dest))
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


__all__ = [
    'PruneResult',
    'RestoreRecovery',
    'ShadowRepo',
    'ShadowRepoError',
    'SnapshotDiff',
    'SnapshotInfo',
    'VerificationResult',
]

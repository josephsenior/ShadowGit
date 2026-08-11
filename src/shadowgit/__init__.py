"""Public API for ShadowGit."""

from shadowgit.ignore import (
    DEFAULT_IGNORE_PATTERNS,
    build_ignore_matcher,
    build_pathspec,
)
from shadowgit.repository import (
    PruneResult,
    RestoreRecovery,
    ShadowRepo,
    ShadowRepoError,
    SnapshotDiff,
    SnapshotInfo,
    VerificationResult,
)

__all__ = [
    'PruneResult',
    'RestoreRecovery',
    'ShadowRepo',
    'ShadowRepoError',
    'SnapshotDiff',
    'SnapshotInfo',
    'VerificationResult',
    'DEFAULT_IGNORE_PATTERNS',
    'build_ignore_matcher',
    'build_pathspec',
]

__version__ = '0.1.0'

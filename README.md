# ShadowGit

**Invisible, Git-backed checkpoints for any directory.**

[![CI](https://github.com/josephsenior/shadowgit/actions/workflows/ci.yml/badge.svg)](https://github.com/josephsenior/shadowgit/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ShadowGit creates content-addressed filesystem snapshots in a private Git
object store. It does not touch the workspace's `.git`, invoke the Git CLI, or
require the directory to be a repository.

It is designed for coding agents, migration tools, refactoring systems, IDEs,
and automation that needs a fast recovery point before changing files.

## Why ShadowGit?

- **Invisible:** the default store lives under `.shadowgit`, separate from the
  workspace's own repository.
- **Conservative:** restore quarantines unexpected files instead of deleting
  them.
- **Crash recoverable:** a durable journal and private recovery commit are
  written before restore changes the workspace.
- **Filesystem faithful:** byte content, symbolic links, and executable modes
  are represented using native Git tree entries.
- **Incremental:** unchanged files reuse blobs through a persistent stat cache.
- **Embeddable:** the Python API never spawns a subprocess.
- **Machine friendly:** every CLI operation supports structured JSON output.

## Install

```bash
pipx install shadowgit
```

For library use:

```bash
python -m pip install shadowgit
```

ShadowGit requires Python 3.12 or newer.

## Command line

```bash
# Capture and inspect checkpoints
shadowgit -C ./project snapshot --label "before migration"
shadowgit -C ./project list
shadowgit -C ./project diff <before-sha> <after-sha>
shadowgit -C ./project verify

# Restore only after explicit confirmation
shadowgit -C ./project restore <sha> --yes

# Inspect or recover an interrupted restore
shadowgit -C ./project status --json
shadowgit -C ./project recover --yes

# Retain only the newest ten snapshots
shadowgit -C ./project prune --keep-last 10 --yes --json
```

Use `--store PATH` to put the private object store outside the workspace and
`--json` for stable structured output.

## Python API

```python
from shadowgit import ShadowRepo

repo = ShadowRepo("./project")
before = repo.snapshot("before refactor")

# Make changes, then restore if needed.
quarantine = repo.restore(before)

if repo.pending_recovery:
    repo.recover()
```

Custom ignore policy is supported without coupling ShadowGit to an agent:

```python
from shadowgit import ShadowRepo, build_ignore_matcher

workspace = "./project"
repo = ShadowRepo(workspace, ignore=build_ignore_matcher(workspace))
```

## Restore safety model

Before modifying the workspace, ShadowGit:

1. captures the current state in a private recovery ref;
2. fsyncs a restore journal containing the target and recovery SHAs;
3. quarantines unexpected files and path-type conflicts;
4. restores files without following symlinked parent directories;
5. removes the journal and recovery ref only after success.

If the process is interrupted, mutating operations fail closed until
`recover()` or `shadowgit recover --yes` restores the pre-operation state.
Opening a repository never performs recovery automatically.

Ignored paths and reserved roots such as `.git` and `.shadowgit` are never
modified during restore.

## Pruning semantics

Git commit IDs include their parent IDs. Removing snapshots from the middle of
history therefore requires rewriting retained descendants. `prune()` returns a
`PruneResult.rewritten` mapping from every retained old SHA to its current SHA;
the CLI emits the same mapping as JSON. Pruning removes snapshots from the
reachable ShadowGit history. Unreachable Git objects may remain until the
underlying object database is compacted by a future storage-maintenance pass.

## Platform notes

Git tracks one portable permission bit: executable versus non-executable.
ShadowGit preserves that bit on POSIX systems. Windows does not expose an
equivalent chmod bit, so content is restored while executable-mode application
is skipped. Symlink creation on Windows requires an account or Developer Mode
configuration permitted to create symbolic links.

## Scope

ShadowGit is a local filesystem checkpoint engine. It intentionally does not
implement remotes, merges, branches for collaboration, or a replacement Git
CLI. Its object store is private implementation state and should not be used as
the workspace's normal Git repository.

## Development

```bash
python -m pip install -e ".[test]"
ruff check src tests
ruff format --check src tests
pytest
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Origin and license

ShadowGit originated in and is battle-tested by
[Grinta](https://github.com/josephsenior/Grinta-Coding-Agent). It is released
under the [MIT License](LICENSE).

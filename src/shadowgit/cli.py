"""Command-line interface for ShadowGit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from shadowgit import ShadowRepo, ShadowRepoError


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print(f"{item.get('sha', '')}  {item.get('label', '')}")
            else:
                print(item)
        return
    print(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadowgit",
        description="Git-object-backed snapshots for arbitrary directories.",
    )
    parser.add_argument(
        "--workspace",
        "-C",
        default=".",
        help="Directory to snapshot (default: current directory)",
    )
    parser.add_argument(
        "--store",
        help="Bare object-store directory (default: <workspace>/.shadowgit/shadow_repo)",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Initialize the private object store")

    snapshot = commands.add_parser("snapshot", help="Capture the current directory")
    snapshot.add_argument("--label", default="", help="Human-readable snapshot label")

    listing = commands.add_parser("list", help="List snapshots newest first")
    listing.add_argument("--limit", type=int)

    inspect = commands.add_parser("inspect", help="Inspect one snapshot")
    inspect.add_argument("sha")

    diff = commands.add_parser("diff", help="Compare two snapshots")
    diff.add_argument("before")
    diff.add_argument("after")

    restore = commands.add_parser("restore", help="Restore a snapshot conservatively")
    restore.add_argument("sha")
    restore.add_argument(
        "--quarantine", help="Directory for files absent from snapshot"
    )
    restore.add_argument(
        "--yes",
        action="store_true",
        help="Confirm that workspace files may be rewritten or quarantined",
    )

    recover = commands.add_parser("recover", help="Roll back an interrupted restore")
    recover.add_argument(
        "--yes", action="store_true", help="Confirm workspace recovery"
    )

    commands.add_parser("status", help="Show interrupted-restore status")

    prune = commands.add_parser("prune", help="Rewrite history to retained snapshots")
    retention = prune.add_mutually_exclusive_group(required=True)
    retention.add_argument(
        "--keep",
        action="append",
        metavar="SHA",
        help="Snapshot SHA to retain (repeatable)",
    )
    retention.add_argument("--keep-last", type=int, metavar="N", help="Retain newest N")
    retention.add_argument("--all", action="store_true", help="Remove all snapshots")
    prune.add_argument("--yes", action="store_true", help="Confirm history rewrite")

    commands.add_parser("verify", help="Verify reachable commits, trees, and blobs")
    return parser


def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ShadowRepoError(f"Workspace is not a directory: {workspace}")
    store = Path(args.store).expanduser().resolve() if args.store else None
    repo = ShadowRepo(workspace, store)

    if args.command == "init":
        _emit(
            {"workspace": str(workspace), "store": str(repo.store_path)},
            as_json=args.json,
        )
    elif args.command == "snapshot":
        info = repo.inspect(repo.snapshot(args.label))
        _emit(info.to_dict(), as_json=args.json)
    elif args.command == "list":
        _emit(
            [item.to_dict() for item in repo.list_snapshots(limit=args.limit)],
            as_json=args.json,
        )
    elif args.command == "inspect":
        _emit(repo.inspect(args.sha).to_dict(), as_json=args.json)
    elif args.command == "diff":
        _emit(repo.diff(args.before, args.after).to_dict(), as_json=args.json)
    elif args.command == "restore":
        if not args.yes:
            raise ShadowRepoError("restore requires --yes")
        quarantine = repo.restore(args.sha, quarantine_dir=args.quarantine)
        _emit(
            {
                "restored": args.sha,
                "quarantine": str(quarantine) if quarantine else None,
            },
            as_json=args.json,
        )
    elif args.command == "recover":
        if not args.yes:
            raise ShadowRepoError("recover requires --yes")
        quarantine = repo.recover()
        _emit(
            {
                "recovered": quarantine is not None,
                "quarantine": str(quarantine) if quarantine else None,
            },
            as_json=args.json,
        )
    elif args.command == "status":
        recovery = repo.pending_recovery
        _emit(
            {
                "recovery_required": recovery is not None,
                "recovery": recovery.to_dict() if recovery else None,
            },
            as_json=args.json,
        )
    elif args.command == "prune":
        if not args.yes:
            raise ShadowRepoError("prune requires --yes")
        if args.keep is not None:
            keep = set(args.keep)
        elif args.keep_last is not None:
            if args.keep_last < 0:
                raise ShadowRepoError("--keep-last must be non-negative")
            keep = {item.sha for item in repo.list_snapshots(limit=args.keep_last)}
        else:
            keep = set()
        _emit(repo.prune(keep).to_dict(), as_json=args.json)
    elif args.command == "verify":
        result = repo.verify()
        _emit(result.to_dict(), as_json=args.json)
        return 0 if result.valid else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ShadowRepoError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"shadowgit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shadowgit.cli import main

pytest.importorskip('pygit2')


def test_cli_snapshot_list_and_verify(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'file.txt').write_text('content', encoding='utf-8')

    assert main(['-C', str(workspace), '--json', 'snapshot', '--label', 'first']) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot['label'] == 'first'

    assert main(['-C', str(workspace), '--json', 'list']) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing[0]['sha'] == snapshot['sha']

    assert main(['-C', str(workspace), '--json', 'verify']) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification == {'errors': [], 'snapshots_checked': 1, 'valid': True}


def test_cli_restore_requires_explicit_confirmation(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'file.txt').write_text('content', encoding='utf-8')
    assert main(['-C', str(workspace), '--json', 'snapshot']) == 0
    sha = json.loads(capsys.readouterr().out)['sha']

    assert main(['-C', str(workspace), '--json', 'restore', sha]) == 2
    assert 'requires --yes' in json.loads(capsys.readouterr().out)['error']


def test_cli_status_and_prune_keep_last(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    file_path = workspace / 'file.txt'
    file_path.write_text('one', encoding='utf-8')
    assert main(['-C', str(workspace), '--json', 'snapshot', '--label', 'one']) == 0
    capsys.readouterr()
    file_path.write_text('version two', encoding='utf-8')
    assert main(['-C', str(workspace), '--json', 'snapshot', '--label', 'two']) == 0
    capsys.readouterr()

    assert main(['-C', str(workspace), '--json', 'status']) == 0
    assert json.loads(capsys.readouterr().out) == {
        'recovery': None,
        'recovery_required': False,
    }

    assert main(['-C', str(workspace), '--json', 'prune', '--keep-last', '1']) == 2
    assert 'requires --yes' in json.loads(capsys.readouterr().out)['error']
    assert (
        main(
            [
                '-C',
                str(workspace),
                '--json',
                'prune',
                '--keep-last',
                '1',
                '--yes',
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result['snapshots_before'] == 2
    assert result['snapshots_after'] == 1
    assert result['removed'] == 1

"""Explicit local revocation command, never auto-run or exposed to the model.

No account, path, message, key or exception details are printed. Success means
persistent disable plus cleanup finished; timeout/error is reported as pending.
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from src.wechat_import_fence import ImportFence

_PROJECT = Path(__file__).resolve().parents[2]
_BOT = '1111111111'
_OWNER = '2222222222'


def _check_component(path: Path, *, regular=False):
    if path.is_symlink() or path.is_junction():
        raise ValueError('invalid path')
    if path.exists() and regular:
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('invalid file')


def _paths(root: Path):
    root = root.resolve(strict=True)
    config_dir = root / 'config'
    config = config_dir / 'settings.json'
    runtime = root / 'runtime'
    directory = runtime / 'owner-private'
    for entry in (config_dir, runtime, directory):
        _check_component(entry)
    _check_component(config, regular=True)
    _check_component(directory / 'binding.json', regular=True)
    for name in ('wechat.sqlite3', 'import-control.sqlite3'):
        for suffix in ('', '-wal', '-shm', '-journal'):
            _check_component(directory / (name + suffix), regular=True)
    if directory.exists() and not directory.resolve(strict=True).is_relative_to(root):
        raise ValueError('invalid directory')
    return config, directory


def _scope(config: Path):
    with config.open('r', encoding='utf-8-sig') as file:
        value = json.load(file)
    instance, bot, owner = value.get('instance_id'), value.get('bot_qq'), value.get('owner_qq')
    if (not isinstance(instance, str) or not instance or instance != instance.strip()
            or ':' in instance or any(ord(c) < 32 for c in instance)
            or bot != _BOT or owner != _OWNER):
        raise ValueError('invalid owner configuration')
    return f'{instance}:{bot}:{owner}'


def _binding_identity(directory: Path, scope: str):
    path = directory / 'binding.json'
    _check_component(path, regular=True)
    if not path.exists():
        return None
    before = path.stat()
    with path.open('r', encoding='utf-8-sig') as file:
        value = json.load(file)
    after = path.stat()
    def identity(stat):
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    if (identity(before) != identity(after) or not isinstance(value, dict)
            or value.get('schema') != 1 or type(value.get('schema')) is not int
            or value.get('owner_scope') != scope):
        raise ValueError('invalid binding')
    return identity(after)


def _clean_existing(root: Path, directory: Path, scope: str):
    # Revalidate paths at cleanup; never delete a binding with another scope.
    _paths(root)
    binding_identity = _binding_identity(directory, scope)
    path = directory / 'wechat.sqlite3'
    if path.exists():
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT OR IGNORE INTO wx_private_tombstone SELECT scope,conversation,message FROM wx_private_message WHERE scope=?', (scope,))
                db.execute('DELETE FROM wx_private_chunk WHERE scope=?', (scope,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wx_private_source_meta'").fetchone():
                    db.execute('DELETE FROM wx_private_source_meta WHERE scope=?', (scope,))
                db.execute('DELETE FROM wx_private_message WHERE scope=?', (scope,))
                db.execute("UPDATE wx_private_batch SET state='revoked' WHERE scope=?", (scope,))
                db.execute('UPDATE wx_private_control SET enabled=0,epoch=epoch+1 WHERE scope=?', (scope,))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        finally:
            db.close()
    if binding_identity is not None:
        if _binding_identity(directory, scope) != binding_identity:
            raise ValueError('binding changed')
        (directory / 'binding.json').unlink()


def _run_for_root(project_root: Path, *, timeout: float = 30.0):
    """Internal fixture seam; the public command always uses _PROJECT."""
    try:
        root = Path(project_root).resolve(strict=True)
        config, directory = _paths(root)
        scope = _scope(config)
        target = directory / 'wechat.sqlite3'
        control = directory / 'import-control.sqlite3'
        binding = directory / 'binding.json'
        if not target.exists() and not control.exists() and not binding.exists():
            return 'NO_DATA'
        # Existing control is never silently recreated if malformed.
        fence = ImportFence(root, create=not control.exists())
        future = fence.request_revoke(lambda: _clean_existing(root, directory, scope))
        future.result(timeout=timeout)
        return 'REVOKED'
    except (FutureTimeout, Exception):
        # A pending/unknown operation must not be misrepresented as complete.
        return 'PENDING'


def main():
    result = _run_for_root(_PROJECT)
    if result == 'NO_DATA':
        print('未发现需要撤销的微信导入。')
        return 0
    if result == 'REVOKED':
        print('微信导入已禁用，已清理相关记录。')
        return 0
    print('撤销尚未确认完成，请稍后重新运行。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Synthetic-only multi-source composition. No real process, CLI, or bot API.

All inputs and destinations must reside in the project .tmp tree; memory must be
our data-only SyntheticMemory. Claimed identity is synthetic and never proof of a
real WeChat account. Keys and source connections never appear in the result.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import probe
from same_process_research import SyntheticMemory
from src.wechat_import_fence import ImportFence, ImportToken
from src.wechat_private_store import WeChatPrivateStore
from src.wechat_synthetic_import import SyntheticShard, import_single_synthetic_session

_PROJECT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, repr=False)
class SyntheticBinding:
    """Fixture assertions only: no actual window or account verification."""

    account_id: str
    target_id: str
    window_ok: bool
    independent_account_ok: bool


def _inside_fake_tree(path, *, directory=False):
    tmp = _PROJECT / ".tmp"
    if tmp.is_symlink() or tmp.is_junction():
        raise ValueError("synthetic tree link rejected")
    root = tmp.resolve(strict=True)
    if not root.is_relative_to(_PROJECT.resolve(strict=True)):
        raise ValueError("synthetic tree outside project")
    item = Path(path)
    if not item.is_absolute():
        raise ValueError("synthetic absolute path required")
    current = item
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise ValueError("synthetic path link rejected")
        if current == current.parent:
            raise ValueError("synthetic path outside tree")
        current = current.parent
    result = item.resolve(strict=True)
    if not result.is_relative_to(root) or result == root:
        raise ValueError("synthetic path outside tree")
    if directory and not result.is_dir():
        raise ValueError("synthetic directory required")
    if not directory and (not result.is_file() or result.suffix.lower() != ".db"):
        raise ValueError("synthetic database required")
    return result


def _open_synthetic_source(memory, file, stack):
    """Internal same-process handoff; do not return or print the key."""
    import sqlcipher3

    before = file.stat()
    with file.open("rb") as stream:
        page = stream.read(4096)
    if len(page) != 4096:
        raise ValueError("synthetic page unavailable")
    key = probe._find_page_key(memory, page)
    if not key:
        raise ValueError("synthetic key unavailable")
    conn = None
    try:
        conn = sqlcipher3.connect(file.as_uri() + "?mode=ro", uri=True)
        conn.set_key(b"x'" + key.hex().encode("ascii") + b"'")
        if not conn.execute("PRAGMA cipher_version").fetchone():
            raise ValueError("synthetic cipher unavailable")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        # Force key validation before allowing the destination transaction.
        conn.execute("SELECT count(*) FROM main.sqlite_schema").fetchone()
        after = file.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("synthetic file changed")
        stack.callback(conn.close)
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        raise
    finally:
        key = None


def run_synthetic_pipeline(
    *,
    memory,
    contact_db,
    message_dbs,
    fixture_root,
    binding,
    contact_label,
    owner_display_id,
    target_display_id,
    store,
    auth,
    fence,
    token,
    batch_id,
):
    """Only return synthetic counts and completeness; never rows/IDs/keys.

    On any failure return an opaque unknown outcome rather than falsely claiming
    a destination commit was rolled back. This is a test-only orchestration, not
    a real process or source identity authenticator.
    """
    try:
        if type(memory) is not SyntheticMemory or type(binding) is not SyntheticBinding:
            raise ValueError("synthetic fixtures required")
        if (
            binding.window_ok is not True
            or binding.independent_account_ok is not True
            or not binding.account_id.startswith("fake-")
            or not binding.target_id.startswith("fake-")
        ):
            raise ValueError("synthetic binding unavailable")
        if not isinstance(fence, ImportFence) or not isinstance(token, ImportToken):
            raise ValueError("synthetic fence required")
        root = _inside_fake_tree(fixture_root, directory=True)
        if type(store) is not WeChatPrivateStore:
            raise ValueError("synthetic destination required")
        destination = Path(
            store.db.execute("PRAGMA database_list").fetchone()[2]
        ).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("synthetic destination required")
        if (
            store.account_id != binding.account_id
            or store.conversation_id != binding.target_id
        ):
            raise ValueError("synthetic destination binding mismatch")
        if (
            not isinstance(message_dbs, (tuple, list))
            or not message_dbs
            or len(message_dbs) > 32
        ):
            raise ValueError("synthetic shards required")
        contact = _inside_fake_tree(contact_db)
        paths = [_inside_fake_tree(path) for path in message_dbs]
        if any(not p.is_relative_to(root) for p in [contact, *paths]):
            raise ValueError("synthetic source outside fixture")
        if len({os.path.normcase(str(p)) for p in [contact, *paths]}) != len(paths) + 1:
            raise ValueError("synthetic sources not unique")
        fence.check(token)
        with ExitStack() as stack:
            contact_conn = _open_synthetic_source(memory, contact, stack)
            shards = [
                SyntheticShard(
                    f"shard-{i}", _open_synthetic_source(memory, path, stack)
                )
                for i, path in enumerate(paths)
            ]
            summary = import_single_synthetic_session(
                contact_conn=contact_conn,
                shards=shards,
                contact_label=contact_label,
                verified_owner=binding.account_id,
                owner_display_id=owner_display_id,
                target_display_id=target_display_id,
                store=store,
                auth=auth,
                snapshot=store.snapshot(**auth),
                batch_id=batch_id,
                fence=fence,
                token=token,
            )
        return {
            "outcome": "synthetic_imported",
            "counts": summary,
            "history_complete": False,
            "media_inspected": False,
        }
    except Exception:
        return {
            "outcome": "not_confirmed",
            "history_complete": False,
            "media_inspected": False,
        }

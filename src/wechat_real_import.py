"""Explicit trusted-local WeChat single-session import; never a model tool.

Default entry is Windows only and remains unconfigured/not deployed. No paths,
account IDs, keys or source rows are returned or logged. Identity confidence is
practical user-confirmed evidence, not cryptographic proof; uncertain is stopped.
"""

from __future__ import annotations

import ntpath
import re
from pathlib import Path

from .wechat_import_fence import ImportCancelled, ImportFence
from .wechat_local_binding import write_binding
from .wechat_private_store import AccessDenied, WeChatPrivateStore
from .wechat_single_session import Shard, stage_all
from .wechat_synthetic_import import (
    _AtomicStoreStage,
    _SnapshotNames,
    _valid_identifier,
)


class IdentityUnconfirmed(ValueError):
    """Selected sources do not support an unambiguous practical binding."""


def _resolve_profiles(contact, owner_display_id, target_display_id, target_label):
    """Query only two exact identifiers in one contact snapshot; no data returned externally."""
    if not all(
        _valid_identifier(value) for value in (owner_display_id, target_display_id)
    ):
        raise IdentityUnconfirmed("invalid identity")
    if owner_display_id == target_display_id:
        raise IdentityUnconfirmed("ambiguous identity")
    if target_label is not None and not _valid_identifier(target_label):
        raise IdentityUnconfirmed("invalid identity")
    if contact.in_transaction or contact.execute("PRAGMA query_only").fetchone() != (
        1,
    ):
        raise IdentityUnconfirmed("source unavailable")
    sql = (
        "SELECT username,nick_name,remark FROM main.contact "
        "WHERE username COLLATE BINARY=? OR alias COLLATE BINARY=?"
    )
    contact.execute("BEGIN")
    try:
        own = contact.execute(sql, (owner_display_id, owner_display_id)).fetchall()
        other = contact.execute(sql, (target_display_id, target_display_id)).fetchall()
        if len(own) != 1 or len(other) != 1:
            raise IdentityUnconfirmed("identity not unique")
        owner, target = own[0][0], other[0][0]
        if (
            not _valid_identifier(owner)
            or not _valid_identifier(target)
            or owner == target
            or owner.endswith("@chatroom")
            or target.endswith("@chatroom")
            or owner == "unknown"
            or target == "unknown"
        ):
            raise IdentityUnconfirmed("identity mismatch")
        if target_label is not None and target_label not in (other[0][1], other[0][2]):
            raise IdentityUnconfirmed("identity mismatch")
        return owner, target
    finally:
        contact.rollback()


def _check_directory_owner(account_dir, owner):
    """Compare only directly comparable wxid-style directories; never derive identity."""
    name = ntpath.basename(ntpath.normpath(account_dir))
    if name.startswith("wxid_") and owner.startswith("wxid_"):
        if name == owner or re.fullmatch(re.escape(owner) + r"_[A-Za-z0-9]{4}", name):
            return "matched"
        raise IdentityUnconfirmed("directory mismatch")
    return "not_comparable"


def _paths_match_account(account_dir, contact_path, message_paths):
    if not all(
        isinstance(value, str) and ntpath.isabs(value)
        for value in (account_dir, contact_path, *message_paths)
    ):
        return False

    def normalized(value):
        return ntpath.normcase(ntpath.normpath(value))

    expected_contact = ntpath.join(account_dir, "db_storage", "contact", "contact.db")
    expected_message_dir = normalized(ntpath.join(account_dir, "db_storage", "message"))
    if normalized(contact_path) != normalized(expected_contact):
        return False
    seen = set()
    for path in message_paths:
        if normalized(ntpath.dirname(path)) != expected_message_dir or not re.fullmatch(
            r"message_[0-9]+\.db", ntpath.basename(path), re.I
        ):
            return False
        value = normalized(path)
        if value in seen:
            return False
        seen.add(value)
    return True


class _RealStage(_AtomicStoreStage):
    def __init__(self, *args, source_session, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_session = source_session

    def commit(self):
        def checked_commit():
            if self.source_session.revalidate() is not True:
                raise IdentityUnconfirmed("source changed")
            self.store.db.commit()

        self.fence.publish(self.token, checked_commit)
        self.started = False


def import_selected_conversation(
    *,
    project_root,
    owner_scope,
    hwnd,
    contact_path,
    message_paths,
    account_dir,
    signer_thumbprint,
    owner_display_id,
    target_display_id,
    window_confirmed,
    target_label=None,
    initialize_new_store=False,
    _test_source_runner=None,
):
    """Trusted-local explicit action; output only fixed status and aggregate counts.

    The caller must have obtained the user's confirmation of the current window,
    owner profile and target conversation; `window_confirmed` is an attestation,
    not proof. No model routing, command-line args, environment variables, or
    automatic activation are provided. Gate must already be explicitly enabled.
    """
    if (
        window_confirmed is not True
        or type(hwnd) is not int
        or hwnd <= 0
        or not isinstance(owner_scope, str)
        or ":group:" in owner_scope
        or not owner_scope.endswith(":1111111111:2222222222")
        or not isinstance(message_paths, (tuple, list))
        or not message_paths
        or len(message_paths) > 32
        or type(initialize_new_store) is not bool
        or not isinstance(account_dir, str)
        or not account_dir
    ):
        return {"status": "needs_confirmation", "history_complete": False}
    if not _paths_match_account(account_dir, contact_path, message_paths):
        return {"status": "needs_confirmation", "history_complete": False}
    try:
        root = Path(project_root).resolve(strict=True)
        if _test_source_runner is not None:
            # Dependency injection is available only to explicitly isolated fixtures.
            test_root = (Path(__file__).resolve().parents[1] / ".tmp").resolve(
                strict=True
            )
            if not root.is_relative_to(test_root) or root == test_root:
                raise ValueError("test runner restricted")
            runner = _test_source_runner
        else:
            from tools.wechat_readonly_probe.real_source_session import (
                with_real_sources,
            )

            runner = with_real_sources
        fence = ImportFence(root, create=False)
        token = fence.token()
        auth = dict(scope=owner_scope, actor="2222222222", verified=True)
        binding_path = root / "runtime" / "owner-private" / "binding.json"
        # Never expose a previously active binding to a partially validated import.
        # Incremental reimport needs a separate suspension/recovery protocol.
        if binding_path.exists() or binding_path.is_symlink() or binding_path.is_junction():
            return {"status": "needs_setup", "history_complete": False}
        resolved = []

        def operation(source):
            if source.revalidate() is not True:
                raise IdentityUnconfirmed("source changed")
            owner, target = _resolve_profiles(
                source.contact, owner_display_id, target_display_id, target_label
            )
            directory = _check_directory_owner(account_dir, owner)
            resolved.append((owner, target))
            if not source.shards or len(source.shards) != len(message_paths):
                raise IdentityUnconfirmed("source incomplete")
            labels = [name for name, _ in source.shards]
            if len(set(labels)) != len(labels):
                raise IdentityUnconfirmed("source incomplete")
            store = WeChatPrivateStore(
                root,
                owner_scope=owner_scope,
                owner_qq="2222222222",
                account_id=owner,
                conversation_id=target,
                owner_sender_id=owner,
                contact_sender_id=target,
            )
            try:
                if store.created_binding and initialize_new_store:
                    fence.check(token)
                    store.set_access(**auth, enabled=True)
                epoch = store.snapshot(**auth)
                maps, shards = {}, []
                for name, conn in source.shards:
                    if conn.in_transaction or conn.execute(
                        "PRAGMA query_only"
                    ).fetchone() != (1,):
                        raise IdentityUnconfirmed("source unavailable")
                    names = _SnapshotNames(conn, owner, target)
                    maps[name] = names
                    shards.append(Shard(name, conn, names))
                from uuid import uuid4

                batch_id = "local-import-" + uuid4().hex
                stage = _RealStage(
                    store,
                    auth,
                    epoch,
                    batch_id,
                    maps,
                    fence,
                    token,
                    source_session=source,
                )
                stage_all(shards, target, stage, page_size=100)
                return {
                    "status": "imported",
                    "counts": {
                        "imported": stage.imported,
                        "duplicates": stage.duplicates,
                        "redacted": stage.redacted,
                        "media_uninspected": stage.media_uninspected,
                    },
                    "directory_check": directory,
                    "history_complete": False,
                    "media_inspected": False,
                }
            finally:
                store.close()

        def guarded_operation(source):
            try:
                return operation(source)
            except IdentityUnconfirmed:
                return {"status": "needs_confirmation", "history_complete": False}
            except ImportCancelled:
                return {
                    "status": "cancelled_or_access_disabled",
                    "history_complete": False,
                }
            except (AccessDenied, FileNotFoundError):
                return {"status": "needs_setup", "history_complete": False}
            except Exception:
                return {
                    "status": "not_confirmed",
                    "commit_status": "unknown",
                    "history_complete": False,
                }

        result = runner(
            hwnd,
            contact_path,
            message_paths,
            signer_thumbprint,
            operation=guarded_operation,
        )
        if result.get("status") == "imported":
            if len(resolved) != 1:
                raise IdentityUnconfirmed("identity unavailable")
            owner, target = resolved[0]
            # Publish only after the source runner has completed its final check.
            # The fence serializes publication with revocation.
            fence.publish(token, lambda: write_binding(root, owner_scope, owner, target))
        return result
    except IdentityUnconfirmed:
        return {"status": "needs_confirmation", "history_complete": False}
    except ImportCancelled:
        return {"status": "cancelled_or_access_disabled", "history_complete": False}
    except (AccessDenied, FileNotFoundError):
        return {"status": "needs_setup", "history_complete": False}
    except Exception:
        # A failure after commit can be ambiguous; never claim rollback or log details.
        return {
            "status": "not_confirmed",
            "commit_status": "unknown",
            "history_complete": False,
        }

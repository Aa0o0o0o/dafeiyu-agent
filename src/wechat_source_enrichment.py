"""Explicit owner-only source metadata enrichment, never a model tool.

Exactly one already-imported conversation is rescanned through the existing
read-only session. Metadata and quote excerpts publish atomically, without
changing previously imported message bodies. No keys, IDs or text are logged.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .wechat_import_fence import ImportCancelled, ImportFence
from .wechat_local_binding import read_binding
from .wechat_private_store import AccessDenied, WeChatPrivateStore
from .wechat_quote_parser import extract_quote, image_digest_hint
from .wechat_real_import import IdentityUnconfirmed, _check_directory_owner, _paths_match_account, _resolve_profiles
from .wechat_single_session import Shard, read_pages
from .wechat_synthetic_import import _SnapshotNames


_KEYS = ('account_id', 'conversation_id', 'owner_sender_id', 'contact_sender_id')


def _message_id(msg):
    if msg.server_id:
        return f'server:{msg.server_id}'
    return 'local:' + hashlib.sha256((msg.shard + '\\0' + str(msg.local_id)).encode('utf-8')).hexdigest()


def enrich_existing(*, project_root, owner_scope, hwnd, contact_path, message_paths,
                    account_dir, signer_thumbprint, owner_display_id, target_display_id,
                    window_confirmed, target_label='万馨彤', _test_source_runner=None):
    """Atomic per-conversation update; returns aggregate counts only."""
    if (window_confirmed is not True or type(hwnd) is not int or hwnd <= 0
            or not isinstance(owner_scope, str) or ':group:' in owner_scope
            or not owner_scope.endswith(':1111111111:2222222222')
            or not isinstance(message_paths, (list, tuple)) or not message_paths
            or len(message_paths) > 32 or not _paths_match_account(account_dir, contact_path, message_paths)):
        return {'status': 'needs_confirmation'}
    store = None
    started = False
    try:
        root = Path(project_root).resolve(strict=True)
        if _test_source_runner is not None:
            test_root = (Path(__file__).resolve().parents[1] / '.tmp').resolve(strict=True)
            if not root.is_relative_to(test_root) or root == test_root:
                raise ValueError('test runner restricted')
            runner = _test_source_runner
        else:
            from tools.wechat_readonly_probe.real_source_session import with_real_sources
            runner = with_real_sources
        binding = read_binding(root, owner_scope)
        if not binding:
            return {'status': 'needs_setup'}
        fence = ImportFence(root, create=False)
        token = fence.token()
        auth = dict(scope=owner_scope, actor='2222222222', verified=True)
        store = WeChatPrivateStore(root, owner_scope=owner_scope, owner_qq='2222222222',
                                   **{key: binding[key] for key in _KEYS})
        snapshot = store.snapshot(**auth)
        counts = {'updated': 0, 'unchanged': 0, 'not_in_import': 0,
                  'quotes': 0, 'quote_unparsed': 0, 'images_with_hint': 0}

        def operation(source):
            nonlocal started
            if source.revalidate() is not True:
                raise IdentityUnconfirmed('source changed')
            owner, target = _resolve_profiles(source.contact, owner_display_id, target_display_id, target_label)
            if owner != binding['account_id'] or target != binding['conversation_id']:
                raise IdentityUnconfirmed('binding mismatch')
            _check_directory_owner(account_dir, owner)
            if not source.shards or len(source.shards) != len(message_paths):
                raise IdentityUnconfirmed('source incomplete')
            if len({name for name, _ in source.shards}) != len(source.shards):
                raise IdentityUnconfirmed('source incomplete')
            shards, maps = [], {}
            for name, conn in source.shards:
                if conn.in_transaction or conn.execute('PRAGMA query_only').fetchone() != (1,):
                    raise IdentityUnconfirmed('source unavailable')
                names = _SnapshotNames(conn, owner, target)
                maps[name] = names
                shards.append(Shard(name, conn, names))
            fence.check(token)
            store.db.execute('BEGIN IMMEDIATE')
            started = True
            store._guard(snapshot)
            for page in read_pages(shards, target, 100):
                fence.check(token)
                store._guard(snapshot)
                for msg in page:
                    mid = _message_id(msg)
                    old = store.db.execute(
                        "SELECT m.sender,m.occurred,m.kind FROM wx_private_message m "
                        "JOIN wx_private_batch b ON b.scope=m.scope AND b.conversation=m.conversation AND b.batch=m.batch "
                        "WHERE m.scope=? AND m.conversation=? AND m.message=? AND b.state='active'",
                        (owner_scope, target, mid),
                    ).fetchone()
                    if old is None:
                        counts['not_in_import'] += 1
                        continue
                    sender = maps[msg.shard].get(msg.real_sender_id, 'unknown')
                    if old[0] != sender or (old[1] is not None and old[1] != float(msg.create_time)):
                        raise IdentityUnconfirmed('message identity changed')
                    quote = extract_quote(msg.local_type, msg.message_content, msg.compress_content)
                    # Only other placeholders can gain a quote excerpt; never overwrite a text message.
                    if old[2] != 'other' and quote.status == 'decoded':
                        quote = type(quote)('not_quote', '')
                    digest, digest_status = image_digest_hint(msg.local_type, msg.message_content,
                                                                msg.packed_info_data, msg.compress_content)
                    values = (msg.shard, msg.local_id, msg.server_id, msg.local_type, msg.create_time,
                              digest, digest_status, quote.status, quote.text)
                    previous = store.db.execute(
                        'SELECT shard,local_id,server_id,raw_type,created_at,image_digest,image_digest_status,quote_status,quote_text '
                        'FROM wx_private_source_meta WHERE scope=? AND conversation=? AND message=?',
                        (owner_scope, target, mid),
                    ).fetchone()
                    if previous == values:
                        counts['unchanged'] += 1
                    else:
                        store.db.execute(
                            'INSERT INTO wx_private_source_meta VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                            'ON CONFLICT(scope,conversation,message) DO UPDATE SET '
                            'shard=excluded.shard,local_id=excluded.local_id,server_id=excluded.server_id,'
                            'raw_type=excluded.raw_type,created_at=excluded.created_at,image_digest=excluded.image_digest,'
                            'image_digest_status=excluded.image_digest_status,quote_status=excluded.quote_status,quote_text=excluded.quote_text',
                            (owner_scope, target, mid, *values),
                        )
                        counts['updated'] += 1
                    if quote.status == 'decoded':
                        counts['quotes'] += 1
                    elif quote.status in ('unsupported', 'unavailable', 'redacted'):
                        counts['quote_unparsed'] += 1
                    if digest_status == 'heuristic_unverified':
                        counts['images_with_hint'] += 1
            return True

        if runner(hwnd, contact_path, message_paths, signer_thumbprint, operation=operation) is not True:
            raise IdentityUnconfirmed('source session failed')
        def commit():
            store._guard(snapshot)
            current = read_binding(root, owner_scope)
            if not current or any(current[key] != binding[key] for key in _KEYS):
                raise IdentityUnconfirmed('binding changed')
            if counts['updated']:
                store.db.execute('UPDATE wx_private_control SET epoch=epoch+1 WHERE scope=? AND conversation=?', (owner_scope, target_id))
            store.db.commit()

        target_id = binding['conversation_id']
        fence.publish(token, commit)
        started = False
        return {'status': 'updated', **counts, 'history_complete': False}
    except (ImportCancelled, AccessDenied):
        return {'status': 'cancelled_or_disabled'}
    except IdentityUnconfirmed:
        return {'status': 'needs_confirmation'}
    except Exception:
        return {'status': 'not_confirmed'}
    finally:
        if store is not None:
            if started:
                store.db.rollback()
            store.close()
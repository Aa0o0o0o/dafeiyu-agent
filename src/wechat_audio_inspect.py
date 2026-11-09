"""Count locally available and decodable voice for the authorized conversation.

The source must already be a verified, query-only session. Audio bytes are
passed to a short-lived local decoder over a pipe and never stored or logged.
This module does not perform ASR, publish memories, or send messages.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

MAX_BLOB = 8 * 1024 * 1024
MAX_IDS = 1000


def inspect_voices(session, target_id, server_ids, decode):
    if not isinstance(target_id, str) or not target_id or target_id.endswith('@chatroom'):
        raise ValueError('invalid conversation')
    if not isinstance(server_ids, (tuple, list)) or len(server_ids) > MAX_IDS:
        raise ValueError('invalid message ids')
    if any(type(v) is not int or v <= 0 or v > 9223372036854775807 for v in server_ids):
        raise ValueError('invalid server id')
    if len(set(server_ids)) != len(server_ids):
        raise ValueError('duplicate server id')
    if session.revalidate() is not True:
        raise ValueError('source changed')
    counts = {'messages': len(server_ids), 'available': 0, 'decodable': 0,
              'missing': 0, 'conflicting': 0, 'oversized': 0, 'decoder_error': 0,
              'unknown_source': 0}
    found = {server_id: [] for server_id in server_ids}
    for _, conn in session.shards:
        if conn.in_transaction or conn.execute('PRAGMA query_only').fetchone() != (1,):
            raise ValueError('source is not read only')
        conn.execute('BEGIN')
        try:
            rows = conn.execute('SELECT rowid FROM main.Name2Id WHERE user_name=? LIMIT 2', (target_id,)).fetchall()
            if len(rows) > 1:
                raise ValueError('ambiguous conversation mapping')
            if not rows:
                continue
            chat_id = rows[0][0]
            for server_id in server_ids:
                lengths = conn.execute(
                    'SELECT length(voice_data) FROM main.VoiceInfo WHERE chat_name_id=? AND svr_id=? LIMIT 2',
                    (chat_id, server_id),
                ).fetchall()
                if len(lengths) > 1:
                    found[server_id].append(('conflict', None))
                    continue
                if not lengths or lengths[0][0] is None or lengths[0][0] == 0:
                    continue
                size = lengths[0][0]
                if type(size) is not int or size < 0 or size > MAX_BLOB:
                    found[server_id].append(('oversized', None))
                    continue
                row = conn.execute(
                    'SELECT voice_data FROM main.VoiceInfo WHERE chat_name_id=? AND svr_id=? LIMIT 1',
                    (chat_id, server_id),
                ).fetchone()
                blob = row[0] if row else None
                if not isinstance(blob, bytes) or len(blob) != size:
                    found[server_id].append(('conflict', None))
                    continue
                found[server_id].append(('ok', blob))
        finally:
            conn.rollback()
    for values in found.values():
        if not values:
            counts['missing'] += 1
            continue
        if any(v[0] == 'conflict' for v in values):
            counts['conflicting'] += 1
            continue
        if any(v[0] == 'oversized' for v in values):
            counts['oversized'] += 1
            continue
        digests = {hashlib.sha256(v[1]).digest() for v in values}
        if len(digests) != 1:
            counts['conflicting'] += 1
            continue
        counts['available'] += 1
        try:
            if decode(values[0][1]) is True:
                counts['decodable'] += 1
            else:
                counts['decoder_error'] += 1
        except Exception:
            counts['decoder_error'] += 1
    if session.revalidate() is not True:
        raise ValueError('source changed')
    return counts


def decode_locally(blob, project_root):
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_BLOB:
        return False
    executable = Path(project_root) / '.venv' / 'Scripts' / 'python.exe'
    program = ('import io,sys,pysilk; b=sys.stdin.buffer.read(8388609); '
               'assert 0<len(b)<=8388608; out=io.BytesIO(); '
               'pysilk.decode(io.BytesIO(b),out,24000); '
               'n=out.tell(); assert 0<n<=72000000; print("DECODE_OK")')
    try:
        result = subprocess.run([str(executable), '-B', '-c', program], input=blob,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        return result.returncode == 0 and result.stdout.strip() == b'DECODE_OK'
    except (OSError, subprocess.SubprocessError):
        return False

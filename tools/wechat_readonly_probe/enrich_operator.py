"""One-time owner-authorized source metadata enrichment. No auto-run or model tool."""
from __future__ import annotations

import json
import sys

from src.wechat_source_enrichment import enrich_existing
from . import import_operator, secure_input


def execute(hwnd, owner_display_id, target_display_id):
    try:
        contact = import_operator.discover_contact()
        shards = import_operator.discover_shards(contact) if contact else None
        signer = import_operator.trusted_signer()
        if not contact or not shards or not signer:
            return {'status': 'needs_confirmation'}
        account = import_operator.ntpath.dirname(import_operator.ntpath.dirname(import_operator.ntpath.dirname(contact)))
        return enrich_existing(project_root=import_operator._PROJECT,
            owner_scope='dafeiyu-local-01:1111111111:2222222222', hwnd=hwnd,
            contact_path=contact, message_paths=shards, account_dir=account,
            signer_thumbprint=signer, owner_display_id=owner_display_id,
            target_display_id=target_display_id, window_confirmed=True)
    except Exception:
        return {'status': 'not_confirmed'}


def main():
    if len(sys.argv) != 2:
        print(json.dumps({'status': 'invalid_input'}))
        return 2
    try:
        hwnd = int(sys.argv[1])
        owner, target = secure_input.read_two()
        result = execute(hwnd, owner, target)
        owner = target = None
        print(json.dumps(result, ensure_ascii=True))
    except BaseException:
        print(json.dumps({'status': 'not_confirmed'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
"""Private owner-scoped local binding; never prints or exposes identifiers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_KEYS = ("account_id", "owner_sender_id", "contact_sender_id", "conversation_id")


def _path(project_root):
    root = Path(project_root).resolve(strict=True)
    directory = root / "runtime" / "owner-private"
    target = directory / "binding.json"
    for path in (root / "runtime", directory, target):
        if path.is_symlink() or path.is_junction():
            raise ValueError("invalid local binding path")
    if not directory.is_dir() or not directory.resolve().is_relative_to(root):
        raise ValueError("invalid local binding directory")
    return target


def read_binding(project_root, owner_scope):
    try:
        target = _path(project_root)
        if not target.is_file() or target.stat().st_size > 8192:
            return None
        record = json.loads(target.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or record.get("schema") != 1
            or record.get("enabled") is not True
            or record.get("owner_scope") != owner_scope
            or record.get("source_kind") != "verified_local_db"
            or any(not isinstance(record.get(k), str) or not record[k] for k in _KEYS)
            or record["account_id"] != record["owner_sender_id"]
            or record["conversation_id"] != record["contact_sender_id"]
            or record["owner_sender_id"] == record["contact_sender_id"]
        ):
            return None
        return {key: record[key] for key in (*_KEYS, "source_kind")} | {
            "enabled": True,
            "contact_label": record.get("contact_label", "万馨彤"),
        }
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def write_binding(project_root, owner_scope, owner_id, target_id):
    """Trusted local importer only; call inside an enabled publication fence."""
    if (
        not isinstance(owner_scope, str)
        or not owner_scope.endswith(":1111111111:2222222222")
        or not isinstance(owner_id, str)
        or not owner_id
        or not isinstance(target_id, str)
        or not target_id
        or owner_id == target_id
    ):
        raise ValueError("invalid local binding")
    target = _path(project_root)
    record = {
        "schema": 1,
        "enabled": True,
        "owner_scope": owner_scope,
        "account_id": owner_id,
        "owner_sender_id": owner_id,
        "contact_sender_id": target_id,
        "conversation_id": target_id,
        "contact_label": "万馨彤",
        "source_kind": "verified_local_db",
    }
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix="binding-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

"""Only explicitly managed snapshots expire; never traverse a reparse directory."""

import json
import os
import math
import shutil
from pathlib import Path


def prune_managed_backups(project_root, now):
    project_root = Path(project_root).resolve()
    backup_root = (project_root / "backups").resolve()
    if not backup_root.is_relative_to(project_root) or backup_root == project_root:
        raise ValueError("Backup root escaped project")
    if not backup_root.exists():
        return []
    removed = []
    for candidate in backup_root.iterdir():
        marker = candidate / "MANIFEST.json"
        if not candidate.is_dir() or not marker.is_file():
            continue
        if marker.is_symlink() or not marker.resolve().is_relative_to(
            candidate.resolve()
        ):
            continue
        if (
            candidate.is_symlink()
            or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400
            or not candidate.resolve().is_relative_to(backup_root)
        ):
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if (
            not isinstance(data, dict)
            or data.get("memory_v2_managed") is not True
            or not isinstance(data.get("expires_at"), (int, float))
            or not math.isfinite(data["expires_at"])
            or now < data["expires_at"]
        ):
            continue
        resolved = candidate.resolve()
        if resolved == backup_root or not resolved.is_relative_to(backup_root):
            raise ValueError("Unsafe snapshot target")
        for directory, subdirs, files in os.walk(candidate, followlinks=False):
            for name in subdirs + files:
                part = Path(directory) / name
                if (
                    part.is_symlink()
                    or getattr(part.lstat(), "st_file_attributes", 0) & 0x400
                ):
                    raise ValueError("Reparse item in managed snapshot")
        shutil.rmtree(resolved)
        removed.append(candidate.name)
    return removed


def adopt_legacy_memory_backups(project_root, now):
    """User-authorized 7-day retention: known root SQLite artifacts only."""
    root = Path(project_root).resolve()
    backup_root = root / "backups"
    if backup_root.is_symlink() or backup_root.resolve().parent != root:
        raise ValueError("Unsafe backup root")
    adopted = []
    for folder in backup_root.iterdir():
        if (
            not folder.is_dir()
            or folder.is_symlink()
            or getattr(folder.lstat(), "st_file_attributes", 0) & 0x400
        ):
            continue
        marker = folder / "MEMORY_DATA_RETENTION.json"
        if marker.exists():
            continue
        artifacts = []
        for name in ["state.sqlite3", "pre-deploy.sqlite3"]:
            part = folder / name
            if (
                part.is_file()
                and not part.is_symlink()
                and part.resolve().parent == folder.resolve()
            ):
                artifacts.append(
                    dict(
                        name=name, expires_at=min(now, part.stat().st_mtime) + 7 * 86400
                    )
                )
        if artifacts:
            marker.write_text(
                json.dumps(
                    dict(version=1, memory_data_only=True, artifacts=artifacts),
                    indent=2,
                ),
                encoding="utf-8",
            )
            adopted.append(folder.name)
    return adopted


def prune_legacy_memory_data(project_root, now):
    root = Path(project_root).resolve()
    backup_root = (root / "backups").resolve()
    if backup_root.parent != root:
        raise ValueError("Unsafe backup root")
    removed = []
    if not backup_root.exists():
        return removed
    for folder in backup_root.iterdir():
        if (
            not folder.is_dir()
            or folder.is_symlink()
            or getattr(folder.lstat(), "st_file_attributes", 0) & 0x400
        ):
            continue
        marker = folder / "MEMORY_DATA_RETENTION.json"
        if (
            not marker.is_file()
            or marker.is_symlink()
            or getattr(marker.lstat(), "st_file_attributes", 0) & 0x400
            or marker.resolve().parent != folder.resolve()
        ):
            continue
        try:
            manifest = json.loads(marker.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("version") != 1
                or manifest.get("memory_data_only") is not True
            ):
                continue
            for artifact in manifest["artifacts"]:
                name, expires = artifact["name"], artifact["expires_at"]
                if (
                    name not in {"state.sqlite3", "pre-deploy.sqlite3"}
                    or not isinstance(expires, (int, float))
                    or not math.isfinite(expires)
                    or now < expires
                ):
                    continue
                for suffix in ["", "-wal", "-shm"]:
                    part = folder / (name + suffix)
                    if not part.exists():
                        continue
                    if (
                        part.is_symlink()
                        or getattr(part.lstat(), "st_file_attributes", 0) & 0x400
                        or part.resolve().parent != folder.resolve()
                    ):
                        raise ValueError("Unsafe backup data target")
                    part.unlink()
                    removed.append(str(part.relative_to(root)))
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    return removed

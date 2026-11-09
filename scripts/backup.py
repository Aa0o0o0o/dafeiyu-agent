"""Consistent state backup; excludes secrets, QQ session files and software binaries."""

import json
import shutil
import sqlite3
import sys
from datetime import datetime
import time
from pathlib import Path
from contextlib import closing

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from src.stickers import backup_library  # noqa: E402

destination = root / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
destination.mkdir(parents=True)
source = root / "runtime/state.sqlite3"
if source.exists():
    with (
        closing(sqlite3.connect(source)) as active,
        closing(sqlite3.connect(destination / "state.sqlite3")) as backup,
    ):
        active.backup(backup)
shutil.copy(root / "config/settings.json", destination / "settings.json")
sticker_result = backup_library(root / "runtime/stickers", destination / "stickers")
(destination / "MANIFEST.json").write_text(
    json.dumps(
        {
            "contains": [p.name for p in destination.iterdir()],
            "excludes": [
                "DeepSeek Key",
                "OneBot token",
                "QQ login data",
                "software binaries",
            ],
            "sensitive": "May contain conversation text and explicit memories",
            "sticker_snapshot": sticker_result,
            "memory_v2_managed": True,
            "expires_at": time.time() + 7 * 86400,
            "restore_requires_latest_deletion_registry": True,
        },
        indent=2,
    ),
    encoding="utf-8",
)
print(
    (
        "备份完成："
        if sticker_result["complete"]
        else "备份已生成但表情文件有缺失，请重试："
    )
    + str(destination)
)
print("包含聊天上下文与明确记忆，请留在本地。凭据和 QQ 登录数据不在备份中。")
print("表情库索引及收藏原文件已纳入备份。")

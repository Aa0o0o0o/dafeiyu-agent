"""Foreground project entry point. No WebUI or administrative bot commands."""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["ASTRBOT_ROOT"] = str(ROOT / "runtime/astrbot")
os.environ["TEMP"] = os.environ["TMP"] = str(ROOT / ".tmp")
sys.path.insert(0, str(ROOT / "vendor/AstrBot"))

# Write the safe upstream core configuration before the first AstrBot import.
data = ROOT / "runtime/astrbot/data"
data.mkdir(parents=True, exist_ok=True)
config_file = data / "cmd_config.json"
upstream_config = (
    json.loads(config_file.read_text(encoding="utf-8-sig"))
    if config_file.exists()
    else {}
)
upstream_config.setdefault("dashboard", {}).update(enable=False, host="127.0.0.1")
upstream_config.setdefault("provider_settings", {}).update(
    enable=False, computer_use_runtime="none"
)
upstream_config.update(
    platform=[],
    provider=[],
    plugin_set=[],
    admins_id=[],
    disable_builtin_commands=True,
    disable_metrics=True,
    log_level="CRITICAL",
    log_file_enable=False,
    trace_enable=False,
)
config_file.write_text(
    json.dumps(upstream_config, ensure_ascii=False, indent=2), encoding="utf-8"
)

if __name__ == "__main__":
    from src.astrbot_runtime import run

    try:
        asyncio.run(run(ROOT))
    except KeyboardInterrupt:
        print("大肥鱼聊天服务已停止。")
    except Exception as error:
        # Do not print provider errors or raw payloads which can contain secrets.
        print(
            "启动失败。请先配置密钥，并运行 04_诊断.cmd。错误类型："
            + type(error).__name__
        )
        sys.exit(1)

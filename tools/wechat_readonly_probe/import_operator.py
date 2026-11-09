"""Explicit local-only operator for a selected WeChat single-session import.

Not imported by the bot or scheduled. Running scans process memory after the
operator confirms the selected window; no data or IDs are printed. After a verified first import, an owner-only local binding is published.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from src.wechat_import_fence import ImportFence
from src.wechat_real_import import import_selected_conversation
from . import metadata_review, secure_input

_PROJECT = Path(__file__).resolve().parents[2]
_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\x00\r\n\"<>|*?]{1,500}")


def _normal_path(candidate):
    if type(candidate) is not str:
        return None
    candidate = candidate.strip().replace("/", "\\")
    drive, tail = ntpath.splitdrive(candidate)
    if (
        not re.fullmatch(r"[A-Za-z]:", drive)
        or not tail.startswith("\\")
        or ":" in tail
    ):
        return None
    parts = tail[1:].split("\\")
    if any(not part or part in (".", "..") or part[-1] in (" ", ".") for part in parts):
        return None
    return ntpath.normpath(candidate)


def discover_contact():
    base = os.environ.get("APPDATA")
    if not base:
        return None
    config_dir = os.path.join(base, "Tencent", "xwechat", "config")
    try:
        with os.scandir(config_dir) as iterator:
            ini_files = [
                entry for entry in iterator if entry.name.lower().endswith(".ini")
            ]
    except OSError:
        return None
    if len(ini_files) > 128:
        return None
    roots = set()
    for entry in ini_files:
        try:
            stat = entry.stat(follow_symlinks=False)
            if (
                not entry.is_file(follow_symlinks=False)
                or stat.st_size > 65536
                or getattr(stat, "st_file_attributes", 0) & 0x400
            ):
                return None
            with open(entry.path, "rb") as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                return None
            try:
                text = raw.decode(
                    "utf-16"
                    if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                    else "utf-8-sig"
                )
            except UnicodeError:
                text = raw.decode("gb18030")
        except (OSError, UnicodeError):
            return None
        for match in _PATH_PATTERN.finditer(text):
            root = _normal_path(match.group(0))
            if root:
                roots.add(root)
            if len(roots) > 128:
                return None
    candidates = {}
    for root in roots:
        for base in (
            root,
            *(
                ntpath.join(root, name)
                for name in ("xwechat_files", "WeChat Files", "xwechat_files_data")
            ),
        ):
            if not os.path.isdir(base):
                continue
            try:
                with os.scandir(base) as iterator:
                    entries = list(iterator)
                if len(entries) > 2048:
                    return None
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    target = ntpath.join(
                        base, entry.name, "db_storage", "contact", "contact.db"
                    )
                    if os.path.isfile(target):
                        candidates[ntpath.normcase(ntpath.normpath(target))] = target
                        if len(candidates) > 1:
                            return None
            except OSError:
                return None
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def discover_shards(contact_path):
    root = ntpath.dirname(ntpath.dirname(contact_path))
    directory = ntpath.join(root, "message")
    try:
        with os.scandir(directory) as entries:
            paths = []
            for entry in entries:
                if not re.fullmatch(r"message_[0-9]+\.db", entry.name, re.I):
                    continue
                info = entry.stat(follow_symlinks=False)
                if (
                    not entry.is_file(follow_symlinks=False)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    return None
                paths.append(ntpath.join(directory, entry.name))
                if len(paths) > 32:
                    return None
    except OSError:
        return None
    return tuple(sorted(paths, key=ntpath.normcase)) if paths else None


def trusted_signer():
    root = os.environ.get("SystemRoot")
    if not root:
        return None
    shell = os.path.join(
        root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    env = os.environ.copy()
    env["DFY_SIGN_FILE"] = metadata_review._EXPECTED_EXE
    command = (
        r"Import-Module -Name (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop;"
        "$s=Get-AuthenticodeSignature -LiteralPath $env:DFY_SIGN_FILE;"
        "if($s.Status -ne 'Valid' -or $null -eq $s.SignerCertificate){exit 1};"
        "$n=$s.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false);"
        "if($n -cne 'Tencent Technology (Shenzhen) Company Limited'){exit 1};"
        "[Console]::Write($s.SignerCertificate.Thumbprint)"
    )
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=env,
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return (
        out.decode("ascii")
        if result.returncode == 0 and re.fullmatch(rb"[0-9a-fA-F]{40}", out)
        else None
    )


def foreground_window():
    if os.name != "nt":
        return 0
    window = ctypes.WinDLL("user32", use_last_error=True)
    window.GetForegroundWindow.argtypes = []
    window.GetForegroundWindow.restype = ctypes.c_void_p
    return int(window.GetForegroundWindow() or 0)


def execute(
    *,
    hwnd,
    owner_display_id,
    target_display_id,
    confirmed,
    root=_PROJECT,
    contact_finder=discover_contact,
    shard_finder=discover_shards,
    signer=trusted_signer,
    importer=import_selected_conversation,
):
    """Trusted local caller only; returns a fixed status/counts, no identifiers."""
    if confirmed is not True or type(hwnd) is not int or hwnd <= 0:
        return {"status": "needs_confirmation", "history_complete": False}
    try:
        contact = contact_finder()
        shards = shard_finder(contact) if contact else None
        thumbprint = signer()
        if not contact or not shards or not thumbprint:
            return {"status": "needs_confirmation", "history_complete": False}
        account_dir = ntpath.dirname(ntpath.dirname(ntpath.dirname(contact)))
        # Explicit local execution is the user's authorization to initiate import.
        # This cannot enable a control DB with unfinished revocation cleanup.
        fence = ImportFence(root)
        fence.enable()
        return importer(
            project_root=root,
            owner_scope="dafeiyu-local-01:1111111111:2222222222",
            hwnd=hwnd,
            contact_path=contact,
            message_paths=shards,
            account_dir=account_dir,
            signer_thumbprint=thumbprint,
            owner_display_id=owner_display_id,
            target_display_id=target_display_id,
            window_confirmed=True,
            target_label="万馨彤",
            initialize_new_store=True,
        )
    except Exception:
        return {
            "status": "not_confirmed",
            "commit_status": "unknown",
            "history_complete": False,
        }


def main():
    if os.name != "nt" or not sys.stdin.isatty():
        print("仅支持在本机 Windows 控制台交互运行。")
        return 2
    print(
        "此操作只读取所选微信会话，扫描进程内存可能触及其他会话数据；不发送微信消息。"
    )
    print("请先在微信确认本人账号，并打开与目标联系人的聊天。")
    print(
        "接下来输入两行：第一行是本人资料页微信号，第二行是目标资料页微信号；输入时不显示。"
    )
    try:
        owner, target = secure_input.read_two()
        input("按回车后请在5秒内切回已确认的微信窗口……")
        time.sleep(5)
        hwnd = foreground_window()
        result = execute(
            hwnd=hwnd, owner_display_id=owner, target_display_id=target, confirmed=True
        )
    except (EOFError, KeyboardInterrupt):
        print("已取消。")
        return 1
    except Exception:
        print("未确认，未能完成本次操作；请检查本机条件。")
        return 1
    finally:
        owner = target = None
    if result.get("status") == "imported":
        counts = result.get("counts", {})
        print(
            f"本机导入完成：新增 {counts.get('imported', 0)} 条，已存在 {counts.get('duplicates', 0)} 条。"
        )
        print(
            "历史是否完整尚未核实；图片和语音内容未解析。已创建主人私聊绑定，请重载聊天服务并验证。"
        )
        return 0
    print("导入未确认，提交结果可能未知；请查看项目状态，勿重复操作猜测。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

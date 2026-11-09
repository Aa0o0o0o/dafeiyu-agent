"""One owned subprocess, server-bound actors, cancellation and scoped artifacts."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4
from .policy import BrowserError, validate_command


class BrowserBroker:
    def __init__(self, root, *, test_ports=(), worker_root=None):
        self.root = Path(root).resolve()
        self.data = (
            Path(worker_root).resolve()
            if worker_root
            else self.root / "runtime/browser"
        )
        self.test_ports = test_ports
        self.process = None
        self.reader = None
        self.pending = {}
        self.artifacts = {}
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.ready = None
        self.last_error = None
        self.version = None
        self.data.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(scope, actor):
        if (
            not isinstance(scope, str)
            or len(scope) > 250
            or not re.fullmatch("[1-9][0-9]{4,19}", str(actor))
        ):
            raise BrowserError("浏览器身份无效")
        return hashlib.sha256((scope + "\0" + actor).encode()).hexdigest()

    async def start(self):
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                return
            python = self.root / "tools/browser/python/Scripts/python.exe"
            if not python.is_file():
                raise BrowserError("浏览器组件尚未安装，请联系主人")
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in {
                    "SYSTEMROOT",
                    "SYSTEMDRIVE",
                    "USERNAME",
                    "USERDOMAIN",
                    "WINDIR",
                    "COMSPEC",
                    "PROCESSOR_ARCHITECTURE",
                    "NUMBER_OF_PROCESSORS",
                    "OS",
                    "PATHEXT",
                }
            }
            home = self.data / "work/home"
            home.mkdir(parents=True, exist_ok=True)
            (home / "AppData/Roaming").mkdir(parents=True, exist_ok=True)
            (home / "AppData/Local").mkdir(parents=True, exist_ok=True)
            temp = self.data / "work/tmp"
            temp.mkdir(parents=True, exist_ok=True)
            env.update(
                PATH=str(python.parent)
                + ";"
                + str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"),
                USERPROFILE=str(home),
                HOME=str(home),
                APPDATA=str(home / "AppData/Roaming"),
                LOCALAPPDATA=str(home / "AppData/Local"),
                TEMP=str(temp),
                TMP=str(temp),
                PLAYWRIGHT_BROWSERS_PATH=str(self.root / "tools/browser/browsers"),
                PLAYWRIGHT_NODEJS_PATH=str(
                    self.root / "tools/node-v24.22.1-win-x64/node.exe"
                ),
                PYTHONUTF8="1",
            )
            args = [
                str(python),
                "-u",
                "-X",
                "utf8",
                str(self.root / "src/browser/worker.py"),
                "--root",
                str(self.data),
            ]
            for port in self.test_ports:
                args += ["--test-port", str(port)]
            self.ready = asyncio.get_running_loop().create_future()
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=str(self.root),
                limit=8 * 1024 * 1024,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.reader = asyncio.create_task(self.read_loop(self.process))
            try:
                await asyncio.wait_for(asyncio.shield(self.ready), 35)
            except (Exception, asyncio.CancelledError):
                await self.stop()
                raise BrowserError("浏览器启动未完成，请检查项目组件") from None

    async def read_loop(self, process):
        try:
            while line := await process.stdout.readline():
                reply = json.loads(line)
                if reply.get("ready"):
                    self.version = reply.get("version")
                    if not self.ready.done():
                        self.ready.set_result(True)
                elif reply.get("id") in self.pending:
                    future = self.pending.pop(reply["id"])
                    if not future.done():
                        future.set_result(reply)
        except (ValueError, OSError):
            pass
        finally:
            if self.ready and not self.ready.done():
                self.ready.set_exception(BrowserError("浏览器worker退出"))
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(BrowserError("浏览器连接中断"))
            self.pending.clear()

    async def write(self, value):
        async with self.write_lock:
            self.process.stdin.write(
                (json.dumps(value, ensure_ascii=False) + "\n").encode()
            )
            await self.process.stdin.drain()

    async def execute(self, scope, actor, command):
        command = validate_command(command)
        key = self.key(scope, actor)
        await self.start()
        rid = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.write({"id": rid, "key": key, "command": command})
            reply = await asyncio.wait_for(asyncio.shield(future), 55)
            if "error" in reply:
                raise BrowserError(reply["error"])
            result = reply["result"]
            artifact = result.get("artifact")
            if artifact:
                path = Path(artifact["path"])
                expected = self.data / "artifacts" / key
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.resolve().parent != expected.resolve()
                    or path.stat().st_size > 32 * 1024 * 1024
                ):
                    raise BrowserError("浏览器文件归属或大小不符合要求")
                artifact["sha256"] = await asyncio.to_thread(
                    lambda: hashlib.sha256(path.read_bytes()).hexdigest()
                )
                self.artifacts[(key, artifact["id"])] = dict(artifact)
                result["artifact"] = {
                    k: v for k, v in artifact.items() if k not in {"path", "sha256"}
                }
            self.last_error = None
            return result
        except (asyncio.CancelledError, TimeoutError):
            if self.process and self.process.returncode is None:
                await self.write({"cancel": rid})
            raise
        except BrowserError as error:
            self.last_error = str(error)
            raise
        finally:
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()

    def artifact(self, scope, actor, token):
        key = self.key(scope, actor)
        item = self.artifacts.get((key, token))
        if not item or item["expires_at"] < time.time():
            raise BrowserError("文件不属于当前会话或已过期")
        path = Path(item["path"])
        if (
            not path.is_file()
            or path.is_symlink()
            or path.resolve().parent != (self.data / "artifacts" / key).resolve()
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise BrowserError("浏览器文件校验失败")
        return dict(item)

    def status(self):
        return {
            "enabled": True,
            "worker_running": bool(self.process and self.process.returncode is None),
            "version": self.version,
            "pending_actions": len(self.pending),
            "last_error": self.last_error,
            "sessions": "scope+actual_actor_isolated",
            "profile": "anonymous_ephemeral",
            "artifact_retention_hours": 24,
        }

    async def stop(self):
        if self.process and self.process.returncode is None:
            try:
                await self.write({"shutdown": True})
                await asyncio.wait_for(self.process.wait(), 8)
            except (Exception, asyncio.CancelledError):
                if self.process.returncode is None:
                    self.process.terminate()
                    await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.process = None
        self.reader = None

    async def close(self):
        await self.stop()

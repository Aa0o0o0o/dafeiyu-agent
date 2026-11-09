"""Autonomous public tools use an isolated bot browser and receipt-only persistence."""

import asyncio
import hashlib
import json
import time

from .browser.policy import ACTIONS, PublicPolicy, validate_command
from .memory_v2.admission import safe_source
from .web_tools import validate_web_call, WEB_RESERVE_CNY


class AutonomousTools:
    def __init__(self, autonomy, run, epoch):
        self.a, self.c, self.run, self.epoch = autonomy, autonomy.c, run, epoch
        self.scope = (
            autonomy.scope
            + ":autonomy:"
            + hashlib.sha256(epoch.encode()).hexdigest()[:16]
        )
        self.actor = self.c.config["bot_qq"]
        self.receipts, self.images = [], []
        self.attempted = []
        self.count = 0
        self.browser_used = False

    def save(self):
        # Keep actual observations before reflection; no image bytes, disk paths or thoughts.
        with self.a.db:
            self.a.db.execute(
                "UPDATE autonomy_runs SET receipt=? WHERE id=? AND scope=?",
                (
                    json.dumps({"actions": self.receipts}, ensure_ascii=False),
                    self.run,
                    self.a.scope,
                ),
            )

    @staticmethod
    def observation(value):
        def clean(item):
            if isinstance(item, dict):
                return {
                    k: clean(v)
                    for k, v in item.items()
                    if k not in {"images", "path", "cookies", "headers"}
                }
            if isinstance(item, list):
                return [clean(v) for v in item[:50]]
            return item

        result = json.dumps(clean(value), ensure_ascii=False)
        if any(
            not safe_source(result[max(0, i - 100) : i + 2800])
            for i in range(0, len(result), 2800)
        ):
            return {"redacted": True, "reason": "potential_sensitive_content"}
        return {
            "excerpt": result[:16000],
            "truncated": len(result) > 16000,
            "verified_ok": isinstance(value, dict) and value.get("ok") is True,
        }

    def check(self):
        self.a.check(self.epoch)
        if self.count >= 8:
            raise ValueError("本次自主探索工具步骤已完成，留待下次继续")

    async def execute(self, plan):
        action = plan.get("action")
        if action in {"search", "fetch"}:
            name = "web_search" if action == "search" else "web_fetch"
            if name not in self.a.permissions()["tools"]:
                raise ValueError("自主联网工具未启用")
            field = "query" if action == "search" else "url"
            args = {field: plan.get(field)}
            encoded = json.dumps(args, ensure_ascii=False)
            validate_web_call(name, encoded)
            if field == "url":
                PublicPolicy().url(args[field])
            self.check()
            self.count += 1
            self.attempted.append(name)
            usage = self.c.store.reserve(
                "autonomy-tool:" + self.run,
                WEB_RESERVE_CNY,
                self.c.config["budget_cny"],
                self.c.config["max_requests"],
            )
            if usage is None:
                raise ValueError("共享账目阻止联网请求")
            try:
                raw = await asyncio.wait_for(
                    self.c.web_tools.execute(name, encoded), 45
                )
                self.c.store.record_usage(usage, "completed")
            except BaseException:
                self.c.store.record_usage(usage, "unknown_cancelled_or_error")
                raise
            self.a.check(self.epoch)
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("无效联网回执")
            if name == "web_search" and not isinstance(result.get("results"), list):
                raise ValueError("无效搜索回执")
            if name == "web_fetch" and not isinstance(result.get("content"), str):
                raise ValueError("无效网页回执")
            self.receipts.append(
                {
                    "tool": name,
                    "request": args,
                    "observed_at": time.time(),
                    "result": self.observation(result),
                }
            )
            self.save()
        elif action == "browser":
            if "browser" not in self.a.permissions()["tools"]:
                raise ValueError("自主浏览器未启用")
            commands = plan.get("commands")
            if (
                not isinstance(commands, list)
                or not 1 <= len(commands) <= 8 - self.count
            ):
                raise ValueError("自主浏览步骤格式或数量不正确")
            for raw in commands:
                command = validate_command(raw)
                if command["action"] not in ACTIONS - {"share"} or command.get("share"):
                    raise ValueError("自主探索不向QQ分享文件或消息")
                if "url" in command:
                    PublicPolicy().url(command["url"])
                self.check()
                self.count += 1
                self.attempted.append("browser")
                self.browser_used = True
                result = await self.c.browser_tools.execute(
                    self.scope, self.actor, command
                )
                self.a.check(self.epoch)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ValueError("无效浏览器回执")
                self.images.extend(result.get("images", []))
                self.receipts.append(
                    {
                        "tool": "browser",
                        "request": command,
                        "observed_at": time.time(),
                        "result": self.observation(result),
                    }
                )
                self.save()
        else:
            raise ValueError("Unsupported autonomous action")

    def review_content(self, interest, root_topic="", criteria=""):
        payload = {
            "interest": interest,
            "root_topic": root_topic,
            "proposed_success_criteria": criteria,
            "permissions": self.a.permissions(),
            "remaining_tool_actions": 8 - self.count,
            "untrusted_tool_receipts": self.receipts,
        }
        return [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ] + self.images

    async def close(self):
        if self.browser_used:
            # Close only this bot-owned context, including playing video, even on cancellation.
            await asyncio.wait_for(
                self.c.browser_tools.execute(
                    self.scope, self.actor, {"action": "close_session"}
                ),
                10,
            )

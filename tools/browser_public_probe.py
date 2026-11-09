"""Read public GitHub and the MDN CC0 video demo; no model, QQ or personal account."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.browser.broker import BrowserBroker  # noqa: E402
from src.browser.policy import BrowserError  # noqa: E402


async def main():
    broker = BrowserBroker(ROOT, worker_root=ROOT / ".tmp/browser-public-probe")
    report = {
        "mode": "production_public_policy_no_local_test_ports",
        "deepseek_calls": 0,
        "qq_messages": 0,
    }
    scope, actor = "public-probe", "12345001"
    try:
        for label, url in [
            ("github", "https://github.com/microsoft/playwright"),
            (
                "video",
                "https://interactive-examples.mdn.mozilla.net/pages/tabbed/video.html",
            ),
        ]:
            try:
                result = await broker.execute(
                    scope, actor, {"action": "open", "url": url}
                )
                report[label] = {
                    "opened": True,
                    "url": result["url"],
                    "title": result["title"],
                    "visible_text_characters": sum(
                        len(f.get("text", "")) for f in result["frames"]
                    ),
                }
                if label == "video":
                    await broker.execute(scope, actor, {"action": "wait", "seconds": 2})
                    video = await broker.execute(
                        scope, actor, {"action": "video_observe", "seconds": 3}
                    )
                    report[label].update(
                        metadata=video["video"],
                        actual_frames=len(video["images"]),
                        observations=video["observations"],
                        coverage=video["coverage"],
                    )
                    await broker.execute(scope, actor, {"action": "video_pause"})
            except Exception as error:
                report[label] = {
                    "opened": False,
                    "error": str(error)
                    if isinstance(error, BrowserError)
                    else type(error).__name__,
                }
        try:
            await broker.execute(
                scope, actor, {"action": "open", "url": "http://127.0.0.1:6200"}
            )
            report["localhost_blocked"] = False
        except BrowserError:
            report["localhost_blocked"] = True
        report["browser_version"] = broker.version
    finally:
        await broker.close()
    (ROOT / "evidence/browser-public-probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

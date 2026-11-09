import io
import time
import unittest
import tempfile
from pathlib import Path
from PIL import Image

from src.stickers import StickerLibrary
from src.sticker_memory import DAY
from tests.test_conversation import ROOT


class StickerMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.lib = StickerLibrary(
            Path(self.tmp.name) / "stickers",
            memory_options={
                "active_limit": 2,
                "unused_days": 180,
                "half_life_days": 45,
            },
        )
        self.group = "12345001"
        self.now = time.time()

    async def asyncTearDown(self):
        await self.lib.close()
        self.tmp.cleanup()

    def add(self, color, days=0, uses=0):
        out = io.BytesIO()
        Image.new("RGB", (12, 12), color).save(out, format="PNG")
        raw = out.getvalue()
        token = self.lib.add_bytes(self.group, raw, "合成表情")
        when = self.now - days * DAY
        with self.lib.db:
            self.lib.db.execute(
                "UPDATE stickers SET created=?,last_used=0 WHERE id=?", (when, token)
            )
            self.lib.db.execute(
                "UPDATE sticker_activity SET last_seen=?,uses=? WHERE token=?",
                (when, uses, token),
            )
        return token, raw

    async def test_active_capacity_archives_old_but_keeps_files(self):
        old, _ = self.add("red", 40)
        new, _ = self.add("blue", 0)
        common, _ = self.add("green", 20, 20)
        self.lib.memory.maintain(self.now, force=True)
        active = {x["id"] for x in self.lib.catalog([self.group])}
        self.assertEqual(active, {new, common})
        self.assertTrue(self.lib.resolve(old, [self.group])[0].exists())
        stats = self.lib.memory.stats([self.group])
        self.assertEqual((stats["active"], stats["archived"]), (2, 1))
        self.assertFalse(stats["cloud_delete"])

    async def test_old_owner_label_protected_and_unknown_old_cold(self):
        pinned, _ = self.add("red", 200)
        cold, _ = self.add("blue", 200)
        self.lib.rename(pinned, [self.group], "主人保留的表情")
        self.lib.memory.maintain(self.now, force=True)
        self.assertEqual({x["id"] for x in self.lib.catalog([self.group])}, {pinned})
        self.assertEqual(self.lib.resolve(cold, [self.group])[0].is_file(), True)

    async def test_explicit_recall_and_actual_use_reactivate_without_wrong_scope(self):
        cold, _ = self.add("red", 200)
        self.lib.memory.maintain(self.now, force=True)
        self.assertEqual(self.lib.catalog([self.group]), [])
        found = self.lib.catalog([self.group], "合成", include_archived=True)
        self.assertEqual(found[0]["id"], cold)
        self.assertEqual(found[0]["memory_state"], "archived")
        self.assertEqual(self.lib.catalog(["12345002"], include_archived=True), [])
        self.lib.mark_used(cold, [self.group])
        self.assertEqual(self.lib.catalog([self.group])[0]["id"], cold)
        self.assertEqual(
            self.lib.db.execute(
                "SELECT uses FROM sticker_activity WHERE token=?", (cold,)
            ).fetchone()[0],
            1,
        )

    async def test_receiving_again_restores_memory_without_duplicate(self):
        cold, raw = self.add("red", 200)
        self.lib.memory.maintain(self.now, force=True)
        self.assertEqual(self.lib.catalog([self.group]), [])
        self.assertEqual(self.lib.add_bytes(self.group, raw, "再次收到"), cold)
        self.assertEqual(self.lib.stats([self.group])[0], 1)
        self.assertEqual(self.lib.catalog([self.group])[0]["id"], cold)

    async def test_reads_do_not_reinforce_or_reanchor(self):
        token, _ = self.add("red", 20)
        before = self.lib.db.execute(
            "SELECT last_seen,uses FROM sticker_activity WHERE token=?", (token,)
        ).fetchone()
        self.lib.catalog([self.group])
        self.lib.memory.stats([self.group])
        self.lib.catalog([self.group])
        after = self.lib.db.execute(
            "SELECT last_seen,uses FROM sticker_activity WHERE token=?", (token,)
        ).fetchone()
        self.assertEqual(before, after)

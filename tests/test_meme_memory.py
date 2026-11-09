import unittest
from tests import test_memory_v2 as fixtures


class MemeMemoryTests(unittest.TestCase):
    setUp = fixtures.V2Tests.setUp
    tearDown = fixtures.V2Tests.tearDown
    make = fixtures.V2Tests.make
    source = fixtures.V2Tests.source
    item = fixtures.V2Tests.item

    def propose(self, value, **changes):
        source = self.source(value)
        item = self.item(source, value)
        item.update(
            kind="semantic",
            predicate="meme_explanation",
            assertion_mode="observation",
            temporal_class="episode",
        )
        item.update(changes)
        return self.memory.apply(
            {"candidates": [item]}, self.memory.snapshot(), allowed_source_ids={source}
        )

    def test_clear_meme_explanation_is_attributed_semantics_not_personal_fact(self):
        result = self.propose("三Q是thank you的谐音，表示谢谢")
        self.assertEqual(result["accepted"], 1)
        row = self.db.execute(
            "SELECT predicate,assertion_mode,temporal_class,scope_id FROM mv2_memories WHERE id=?",
            (result["ids"][0],),
        ).fetchone()
        self.assertEqual(row[:3], ("meme_explanation", "observation", "episode"))
        self.assertEqual(row[3], self.memory.scope)

    def test_guess_question_or_bare_joke_is_not_a_definition(self):
        for value in ["三Q可能是thank you的谐音", "三Q是什么意思？", "栓Q哈哈哈"]:
            self.assertEqual(self.propose(value)["accepted"], 0)

    def test_definition_cannot_be_admitted_as_personality_or_self_report(self):
        self.assertEqual(
            self.propose("三Q是thank you的谐音", assertion_mode="self_report")[
                "accepted"
            ],
            0,
        )
        self.assertEqual(
            self.propose("三Q是thank you的谐音", kind="style")["accepted"], 0
        )

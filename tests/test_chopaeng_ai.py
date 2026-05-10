import unittest
from unittest.mock import AsyncMock, patch

from utils.chopaeng_ai import (
    _auto_link_channels,
    _build_model_prompt,
    _direct_faq_answer,
    _extract_live_search_candidates,
    _format_live_search_answer,
    _is_variant_ordering_question,
    _try_live_search_answer,
    get_ai_answer,
)


class ExtractLiveSearchCandidatesTests(unittest.TestCase):
    def test_explicit_villager_command(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("!villager Sprinkle"),
            [("villager", "sprinkle")],
        )

    def test_where_is_query_checks_both_villager_and_item(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("where is Sprinkle?"),
            [("villager", "sprinkle"), ("item", "sprinkle")],
        )

    def test_who_has_item_phrase(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("who has bells"),
            [("item", "bells")],
        )

    def test_which_islands_have_item_phrase(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("which islands have bells"),
            [("item", "bells")],
        )

    def test_which_island_is_subject_on_checks_both(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("which island is Sprinkle on"),
            [("villager", "sprinkle"), ("item", "sprinkle")],
        )

    def test_does_any_island_have_phrase(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("does any island have bells"),
            [("item", "bells")],
        )

    def test_can_i_find_on_any_island_phrase(self) -> None:
        self.assertEqual(
            _extract_live_search_candidates("can I find bells on any island"),
            [("item", "bells")],
        )


class FormatLiveSearchAnswerTests(unittest.TestCase):
    def test_formats_villager_found_response(self) -> None:
        payload = {
            "found": True,
            "results": {
                "free": ["Tadhana", "Matahom"],
                "sub": ["Alapaap"],
            },
            "suggestions": [],
        }
        self.assertEqual(
            _format_live_search_answer("villager", "sprinkle", payload),
            "I found villager SPRINKLE on these Free Islands: TADHANA | MATAHOM and on this Sub Island: ALAPAAP.",
        )

    def test_formats_item_found_response(self) -> None:
        payload = {
            "found": True,
            "results": {
                "free": ["Sinagtala", "Tinig", "Tala"],
                "sub": ["Likha", "Dalisay"],
            },
            "suggestions": [],
        }
        self.assertEqual(
            _format_live_search_answer("item", "bells", payload),
            "I found item BELLS on these Free Islands: SINAGTALA | TINIG | TALA and on these Sub Islands: LIKHA | DALISAY.",
        )

    def test_formats_item_not_found_response(self) -> None:
        payload = {
            "found": False,
            "results": {"free": [], "sub": []},
            "suggestions": [],
        }
        self.assertEqual(
            _format_live_search_answer("item", "bells", payload),
            "I couldn't find item BELLS right now. If it's not stocked, you can use the Chorder Bot flow in <#1175672083183829075>.",
        )

    def test_formats_suggestion_response(self) -> None:
        payload = {
            "found": False,
            "results": {"free": [], "sub": []},
            "suggestions": ["Sprinkle", "Sparkle"],
        }
        self.assertEqual(
            _format_live_search_answer("villager", "sprinkl", payload),
            "I couldn't find SPRINKL right now. Did you mean: Sprinkle, Sparkle?",
        )


class AutoLinkChannelsTests(unittest.TestCase):
    def test_converts_raw_channel_id_to_mention(self) -> None:
        self.assertEqual(
            _auto_link_channels("Use channel 1175771830510948442 for lookup."),
            "Use channel <#1175771830510948442> for lookup.",
        )

    def test_normalizes_hash_prefixed_channel_mention(self) -> None:
        self.assertEqual(
            _auto_link_channels("Use #<#1175771830510948442> for lookup."),
            "Use <#1175771830510948442> for lookup.",
        )

    def test_normalizes_hash_prefixed_channel_id(self) -> None:
        self.assertEqual(
            _auto_link_channels("Use #1175771830510948442 for lookup."),
            "Use <#1175771830510948442> for lookup.",
        )

    def test_links_known_channel_name_alias(self) -> None:
        self.assertEqual(
            _auto_link_channels("Go to #server-nickname to change your nickname."),
            "Go to <#1081147108612124742> to change your nickname.",
        )


class TryLiveSearchAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_first_found_live_answer(self) -> None:
        villager_payload = {
            "found": True,
            "results": {
                "free": ["Tadhana", "Matahom"],
                "sub": ["Alapaap"],
            },
            "suggestions": [],
        }

        with patch(
            "utils.chopaeng_ai._search_live_api",
            new=AsyncMock(return_value=villager_payload),
        ) as mocked_search:
            answer = await _try_live_search_answer("where is Sprinkle?")

        self.assertEqual(
            answer,
            "I found villager SPRINKLE on these Free Islands: TADHANA | MATAHOM and on this Sub Island: ALAPAAP.",
        )
        mocked_search.assert_awaited_once_with("villager", "sprinkle")

    async def test_falls_back_to_second_candidate_when_first_not_found(self) -> None:
        responses = [
            {"found": False, "results": {"free": [], "sub": []}, "suggestions": []},
            {
                "found": True,
                "results": {"free": ["Sinagtala"], "sub": ["Likha"]},
                "suggestions": [],
            },
        ]

        with patch(
            "utils.chopaeng_ai._search_live_api",
            new=AsyncMock(side_effect=responses),
        ) as mocked_search:
            answer = await _try_live_search_answer("where is bells?")

        self.assertEqual(
            answer,
            "I found item BELLS on this Free Island: SINAGTALA and on this Sub Island: LIKHA.",
        )
        self.assertEqual(mocked_search.await_count, 2)

    async def test_returns_none_when_no_search_pattern_matches(self) -> None:
        with patch(
            "utils.chopaeng_ai._search_live_api",
            new=AsyncMock(),
        ) as mocked_search:
            answer = await _try_live_search_answer("how do I customize an item")

        self.assertIsNone(answer)
        mocked_search.assert_not_awaited()


class VariantOrderingAnswerTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_clothing_variant_order_question(self) -> None:
        self.assertTrue(
            _is_variant_ordering_question("how do i order clothes in different variants?")
        )

    async def test_returns_direct_clothing_variant_order_answer(self) -> None:
        answer = await get_ai_answer("how do i order clothes in different variants?")

        self.assertIn("`!lookup <clothing name>`", answer)
        self.assertIn("`!item <HEX>`", answer)
        self.assertIn("`!customize <HEX> <variant number>`", answer)
        self.assertIn("`!order <long code>`", answer)
        self.assertIn("<#1175771830510948442>", answer)
        self.assertIn("<#1175672083183829075>", answer)


class DirectFaqAnswerTests(unittest.IsolatedAsyncioTestCase):
    def test_phone_message_answer_is_deterministic(self) -> None:
        answer = _direct_faq_answer("Someone is on the phone and I can't enter")

        self.assertIsNotNone(answer)
        self.assertIn("general connection message", answer)

    async def test_direct_faq_answer_does_not_hit_live_search(self) -> None:
        with patch(
            "utils.chopaeng_ai._search_live_api",
            new=AsyncMock(),
        ) as mocked_search:
            answer = await get_ai_answer("island is down?")

        self.assertIn("island is down", answer.lower())
        mocked_search.assert_not_awaited()

    async def test_sanrio_answer_includes_required_sequence(self) -> None:
        answer = await get_ai_answer("how do I get a Sanrio villager?")

        self.assertIn("placeholder", answer.lower())
        self.assertIn("VILLAGER INJECTED", answer)
        self.assertIn("before flying", answer.lower())


class PromptRetrievalTests(unittest.TestCase):
    def test_model_prompt_uses_relevant_kb_not_full_kb_dump(self) -> None:
        prompt = _build_model_prompt("how do I get a Sanrio villager?")

        self.assertIn("Relevant Community Guides", prompt)
        self.assertIn("Sanrio", prompt)
        self.assertNotIn("## Official Links", prompt)

    def test_model_prompt_can_include_system_for_gemini(self) -> None:
        prompt = _build_model_prompt("how do I get dodo code?", include_system_prompt=True)

        self.assertTrue(prompt.startswith("# ROLE"))


if __name__ == "__main__":
    unittest.main()

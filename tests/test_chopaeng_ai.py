import unittest
from unittest.mock import AsyncMock, patch

from utils.chopaeng_ai import (
    _REQUEST_HELP_CHANNEL,
    _extract_live_search_candidates,
    _format_live_search_answer,
    _try_live_search_answer,
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
            f"I couldn't find item BELLS right now. If it's not stocked, you can use the request flow in channel `{_REQUEST_HELP_CHANNEL}`.",
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


if __name__ == "__main__":
    unittest.main()
import io
import json
import unittest
import urllib.error
from datetime import date

import find_unfinished as mod


def row(puzzle_id, print_date, solved=False, percent=0, title="", author="A. Setter", star=None, publish="Daily"):
    return {
        "puzzle_id": puzzle_id,
        "print_date": print_date,
        "publish_type": publish,
        "title": title,
        "author": author,
        "percent_filled": percent,
        "solved": solved,
        "star": star,
    }


class ClassifyTests(unittest.TestCase):
    def test_solved_and_partial_grid(self):
        self.assertEqual(mod.classify(row(1, "2024-01-01", solved=True, percent=100), None), "solved")
        self.assertEqual(
            mod.classify(row(1, "2021-03-18", solved=True, percent=95, star="Gold"), None),
            "solved_partial_grid",
        )

    def test_in_progress_and_blank_open(self):
        self.assertEqual(mod.classify(row(1, "2024-01-02", percent=40), None), "in_progress")
        state = {"puzzle_id": 1, "game_data": {"playTimeSeconds": 12, "puzzleInfoSeen": True}}
        self.assertEqual(mod.classify(row(1, "2024-01-03", percent=0, solved=False), state), "opened_empty")

    def test_missing_state_is_never_opened(self):
        self.assertEqual(mod.classify(row(1, "2024-01-04"), None), "never_opened")
        self.assertFalse(mod.state_was_opened({"puzzle_id": 1, "game_data": {}}))

    def test_state_can_mark_a_solve_the_list_missed(self):
        state = {"puzzle_id": 1, "game_data": {"firstSolve": 1_700_000_000, "completionFraction": 1}}
        self.assertEqual(mod.classify(row(1, "2024-01-05", solved=False, percent=0), state), "solved")

    def test_solve_rate_ignores_puzzles_you_never_opened(self):
        puzzles = [
            mod.make_puzzle(row(1, "2024-01-01", solved=True, percent=100), None),
            mod.make_puzzle(row(2, "2024-01-02", solved=True, percent=95, star="Gold"), None),
            mod.make_puzzle(row(3, "2024-01-03", percent=10), None),
            mod.make_puzzle(
                row(4, "2024-01-04"),
                {"puzzle_id": 4, "game_data": {"playTimeSeconds": 3}},
            ),
            mod.make_puzzle(row(5, "2024-01-05"), None),
        ]
        result = mod.ScanResult("daily", date(2024, 1, 1), date(2024, 1, 5), puzzles)
        self.assertEqual(result.solved_count, 2)
        self.assertEqual(result.opened_count, 4)
        self.assertAlmostEqual(result.solve_rate(), 50.0)
        text = mod.render_text([result])
        self.assertIn("2024-01-03", text)
        self.assertIn("opened, no letters", text)
        self.assertIn("https://www.nytimes.com/crosswords/game/daily/2024/01/04", text)
        self.assertIn("still counts as solved", text)


class CookieTests(unittest.TestCase):
    def test_normalize_header_and_quotes(self):
        self.assertEqual(mod.normalize_cookie("  abcdefghij1234567890  "), "abcdefghij1234567890")
        self.assertEqual(
            mod.normalize_cookie('Cookie: nyt-a=1; NYT-S="session-token-value-here"; other=2'),
            "session-token-value-here",
        )


class CollectTests(unittest.TestCase):
    def test_null_and_capped_windows_are_split(self):
        calls = []

        class Fake:
            def list_puzzles(self, publish_type, start, end):
                calls.append((start, end))
                span = (end - start).days
                if span > 10:
                    return None
                if span > 3:
                    return [row(index, start.isoformat()) for index in range(mod.PAGE_CAP)]
                return [
                    row(int(start.strftime("%Y%m%d")) * 10 + offset, (start).isoformat())
                    for offset in range(span + 1)
                ]

        rows = mod.collect_puzzles(Fake(), "daily", date(2024, 1, 1), date(2024, 1, 20))
        self.assertGreater(len(calls), 1)
        self.assertTrue(rows)
        ids = [item["puzzle_id"] for item in rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_state_batches_split_after_an_error(self):
        class Fake:
            def latest_states(self, publish_type, puzzle_ids):
                if len(puzzle_ids) > 1:
                    raise mod.NytError("batch too big")
                return [{"puzzle_id": puzzle_ids[0], "game_data": {"playTimeSeconds": 1}}]

        states = mod.fetch_states_for(Fake(), "daily", [11, 12, 13])
        self.assertEqual(set(states), {11, 12, 13})


class ClientTests(unittest.TestCase):
    def test_forbidden_is_an_auth_error_without_the_cookie(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"forbidden"}'),
            )

        client = mod.NytClient("super-secret-cookie-value", opener=opener, pause_seconds=0)
        with self.assertRaises(mod.AuthError) as caught:
            client.list_puzzles("daily", date(2024, 1, 1), date(2024, 1, 2))
        self.assertNotIn("super-secret-cookie-value", str(caught.exception))

    def test_scan_rejects_anonymous_list_when_state_disagrees(self):
        class Fake:
            pause_seconds = 0

            def list_puzzles(self, publish_type, start, end):
                return [row(1, "2024-06-01", solved=False, percent=0)]

            def latest_states(self, publish_type, puzzle_ids):
                return [{"puzzle_id": 1, "game_data": {"firstSolveDate": "2024-06-01", "completionFraction": 1}}]

        with self.assertRaises(mod.AuthError):
            mod.scan(Fake(), "daily", date(2024, 6, 1), date(2024, 6, 1), log=lambda _message: None)

    def test_scan_reports_a_blank_open(self):
        class Fake:
            def list_puzzles(self, publish_type, start, end):
                return [
                    row(1, "2024-06-01", solved=True, percent=100, star="Gold"),
                    row(2, "2024-06-02", solved=False, percent=0),
                    row(3, "2024-06-03", solved=False, percent=0),
                ]

            def latest_states(self, publish_type, puzzle_ids):
                self.requested = list(puzzle_ids)
                return [{"puzzle_id": 2, "game_data": {"puzzleInfoSeen": True, "playTimeSeconds": 0}}]

        result = mod.scan(Fake(), "daily", date(2024, 6, 1), date(2024, 6, 3), log=lambda _message: None)
        self.assertEqual([puzzle.puzzle_id for puzzle in result.by_status("opened_empty")], [2])
        self.assertEqual(len(result.by_status("never_opened")), 1)
        self.assertAlmostEqual(result.solve_rate(), 50.0)
        buffer = io.StringIO()
        mod.write_csv([result], buffer)
        self.assertIn("opened_empty", buffer.getvalue())
        payload = json.loads(mod.render_json([result]))
        self.assertEqual(payload[0]["opened_empty"][0]["print_date"], "2024-06-02")


if __name__ == "__main__":
    unittest.main()

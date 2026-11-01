#!/usr/bin/env python3
"""Find NYT crossword puzzles that count against your solve rate.

The crossword stats percentage is solved puzzles divided by puzzles you
opened. Opening a puzzle and backing out with an empty grid still counts as
a miss. The site's In Progress list is short and often omits those, which is
why it can be empty while the percentage sits at 99.9.

This script reads the same puzzle list and saved-game endpoints the crossword
site uses. It needs your NYT-S session cookie, which is a login credential:
keep it in a file that is not committed, and do not pass it on the command line.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from getpass import getpass
from typing import Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

API_ROOT = "https://www.nytimes.com"
LIST_PATH = "/svc/crosswords/v3/puzzles.json"
STATE_PATH = "/svc/games/state/{game}/latests"
PAGE_CAP = 100
STATE_BATCH = 20
WINDOW_DAYS = 90
USER_AGENT = "nyt-unfinished/1.0 (personal crossword progress check)"

PUBLISH_TYPES = ("daily", "mini", "bonus")
DEFAULT_START = {
    "daily": date(1993, 11, 21),
    "mini": date(2014, 8, 21),
    "bonus": date(2009, 1, 1),
}
STATE_GAME = {
    "daily": "crossword_daily",
    "mini": "crossword_mini",
    "bonus": "crossword_bonus",
}
GAME_URL = {
    "daily": "daily",
    "mini": "mini",
    "bonus": "bonus",
}


class NytError(Exception):
    """The crossword service returned something we cannot use."""


class AuthError(NytError):
    """The NYT-S cookie was missing, expired, or rejected."""


@dataclass
class Puzzle:
    puzzle_id: int
    print_date: str
    publish_type: str
    title: str
    author: str
    percent_filled: int
    solved: bool
    star: str | None
    status: str
    weekday: str = ""

    def url(self) -> str:
        year, month, day = self.print_date.split("-")
        kind = GAME_URL[self.publish_type]
        return f"https://www.nytimes.com/crosswords/game/{kind}/{year}/{month}/{day}"


@dataclass
class ScanResult:
    publish_type: str
    start: date
    end: date
    puzzles: list[Puzzle] = field(default_factory=list)

    def by_status(self, status: str) -> list[Puzzle]:
        return [puzzle for puzzle in self.puzzles if puzzle.status == status]

    @property
    def solved_count(self) -> int:
        return len(self.by_status("solved")) + len(self.by_status("solved_partial_grid"))

    @property
    def opened_count(self) -> int:
        return self.solved_count + len(self.by_status("in_progress")) + len(self.by_status("opened_empty"))

    def solve_rate(self) -> float | None:
        opened = self.opened_count
        if opened == 0:
            return None
        return 100.0 * self.solved_count / opened


def normalize_cookie(raw: str) -> str:
    """Pull an NYT-S value out of a bare token or a pasted Cookie header."""
    text = raw.strip().lstrip("\ufeff")
    if not text:
        return ""
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    match = re.search(r"(?i)(?:^|[;\s])NYT-S=([^;\s]+)", text)
    if match:
        text = match.group(1)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text.strip()


def puzzle_url_for(publish_type: str, print_date: str) -> str:
    year, month, day = print_date.split("-")
    return f"https://www.nytimes.com/crosswords/game/{GAME_URL[publish_type]}/{year}/{month}/{day}"


def weekday_name(print_date: str) -> str:
    return date.fromisoformat(print_date).strftime("%A")


def state_game_data(state: Mapping | None) -> dict:
    if not state:
        return {}
    game = state.get("game_data")
    return game if isinstance(game, dict) else {}


def state_was_opened(state: Mapping | None) -> bool:
    """A saved game record means the puzzle was launched, even with no letters."""
    if not state:
        return False
    game = state.get("game_data")
    if game is None:
        return True
    return isinstance(game, dict) and len(game) > 0


def state_marks_solved(state: Mapping | None) -> bool:
    game = state_game_data(state)
    if game.get("firstSolve") or game.get("firstSolveDate"):
        return True
    fraction = game.get("completionFraction")
    if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and fraction >= 1:
        return True
    star = game.get("star")
    return isinstance(star, str) and star.lower() in {"gold", "blue"}


def fill_percent(puzzle: Mapping | None, state: Mapping | None) -> int:
    raw = puzzle.get("percent_filled") if puzzle else None
    list_percent = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        list_percent = max(0, min(100, int(raw)))
    # A positive list percentage is the archive's own fill, including trick
    # puzzles that are solved with a square the timer never marked.
    if list_percent is not None and list_percent > 0:
        return list_percent
    if state_marks_solved(state):
        return 100
    fraction = state_game_data(state).get("completionFraction")
    if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and fraction > 0:
        return max(1, min(100, int(round(float(fraction) * 100))))
    if list_percent is not None:
        return list_percent
    return 0


def classify(puzzle: Mapping, state: Mapping | None) -> str:
    """Label one puzzle from the list row plus an optional saved game.

    ``solved_partial_grid`` is a finished puzzle whose stored fill is under
    100. Trick grids (a rebus, or a square the timer never marked) do this
    and still count as solved.
    """
    solved = bool(puzzle.get("solved")) or state_marks_solved(state)
    percent = fill_percent(puzzle, state)
    if solved and percent < 100:
        return "solved_partial_grid"
    if solved:
        return "solved"
    if percent > 0:
        return "in_progress"
    if state_was_opened(state):
        return "opened_empty"
    return "never_opened"


def make_puzzle(row: Mapping, state: Mapping | None) -> Puzzle:
    publish = str(row.get("publish_type") or "").lower()
    if publish not in GAME_URL:
        publish = "daily"
    print_date = str(row["print_date"])
    return Puzzle(
        puzzle_id=int(row["puzzle_id"]),
        print_date=print_date,
        publish_type=publish,
        title=str(row.get("title") or ""),
        author=str(row.get("author") or ""),
        percent_filled=fill_percent(row, state),
        solved=bool(row.get("solved")) or state_marks_solved(state),
        star=row.get("star") if isinstance(row.get("star"), str) else None,
        status=classify(row, state),
        weekday=weekday_name(print_date),
    )


class NytClient:
    def __init__(
        self,
        cookie: str,
        opener: Callable[..., object] | None = None,
        pause_seconds: float = 0.1,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cookie = cookie
        self.pause_seconds = pause_seconds
        self._sleeper = sleeper
        self._opener = opener or urllib.request.urlopen
        self._calls = 0

    def list_puzzles(self, publish_type: str, start: date, end: date) -> list[dict] | None:
        query = urllib.parse.urlencode(
            {
                "publish_type": publish_type,
                "sort_order": "asc",
                "sort_by": "print_date",
                "date_start": start.isoformat(),
                "date_end": end.isoformat(),
            }
        )
        payload = self._get_json(f"{API_ROOT}{LIST_PATH}?{query}")
        results = payload.get("results")
        if results is None:
            return None
        if not isinstance(results, list):
            raise NytError(f"Puzzle list for {publish_type} {start} to {end} was not a list.")
        return results

    def latest_states(self, publish_type: str, puzzle_ids: Sequence[int]) -> list[dict]:
        if not puzzle_ids:
            return []
        game = STATE_GAME[publish_type]
        ids = ",".join(str(puzzle_id) for puzzle_id in puzzle_ids)
        query = urllib.parse.urlencode({"puzzle_ids": ids})
        payload = self._get_json(f"{API_ROOT}{STATE_PATH.format(game=game)}?{query}")
        states = payload.get("states")
        if states is None:
            return []
        if not isinstance(states, list):
            raise NytError("Saved-game response did not include a list of states.")
        return states

    def _get_json(self, url: str) -> dict:
        if self._calls and self.pause_seconds:
            self._sleeper(self.pause_seconds)
        self._calls += 1
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "Cookie": f"NYT-S={self.cookie}",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
            if status in {401, 403}:
                raise AuthError(
                    "NYT rejected the session cookie. Copy a fresh NYT-S value while you are logged in."
                ) from None
            if status == 429 or status >= 500:
                raise NytError(f"NYT returned HTTP {status}. Wait a minute and run the script again.") from None
            raise NytError(f"NYT returned HTTP {status} for a crossword request.") from None
        except urllib.error.URLError as error:
            raise NytError(f"Could not reach NYT ({error.reason}).") from None
        if status in {401, 403}:
            raise AuthError(
                "NYT rejected the session cookie. Copy a fresh NYT-S value while you are logged in."
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NytError("NYT returned a response that was not JSON.") from error
        if not isinstance(payload, dict):
            raise NytError("NYT returned JSON that was not an object.")
        return payload


def collect_puzzles(
    client: NytClient,
    publish_type: str,
    start: date,
    end: date,
    on_window: Callable[[date, date, int], None] | None = None,
) -> list[dict]:
    """Walk the archive in short windows. Oversized windows come back empty or truncated."""
    found: dict[int, dict] = {}

    def walk(window_start: date, window_end: date) -> None:
        if window_start > window_end:
            return
        rows = client.list_puzzles(publish_type, window_start, window_end)
        if rows is None or len(rows) >= PAGE_CAP:
            if window_start >= window_end:
                raise NytError(
                    f"The {publish_type} list for {window_start.isoformat()} could not be read completely."
                )
            midpoint = window_start + timedelta(days=(window_end - window_start).days // 2)
            walk(window_start, midpoint)
            walk(midpoint + timedelta(days=1), window_end)
            return
        for row in rows:
            found[int(row["puzzle_id"])] = row
        if on_window:
            on_window(window_start, window_end, len(found))

    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=WINDOW_DAYS - 1), end)
        walk(cursor, window_end)
        cursor = window_end + timedelta(days=1)

    return sorted(found.values(), key=lambda row: (str(row.get("print_date")), int(row["puzzle_id"])))


def fetch_states_for(
    client: NytClient,
    publish_type: str,
    puzzle_ids: Sequence[int],
    on_batch: Callable[[int, int], None] | None = None,
) -> dict[int, dict]:
    found: dict[int, dict] = {}

    def walk(chunk: Sequence[int]) -> None:
        if not chunk:
            return
        try:
            states = client.latest_states(publish_type, chunk)
        except AuthError:
            raise
        except NytError as error:
            # A rejected batch is split. Auth failures and NYT HTTP errors are not.
            if len(chunk) == 1 or "HTTP" in str(error) or "Could not reach" in str(error):
                raise
            middle = len(chunk) // 2
            walk(chunk[:middle])
            walk(chunk[middle:])
            return
        for state in states:
            if "puzzle_id" not in state:
                continue
            found[int(state["puzzle_id"])] = state

    total = len(puzzle_ids)
    done = 0
    for offset in range(0, total, STATE_BATCH):
        chunk = list(puzzle_ids[offset : offset + STATE_BATCH])
        walk(chunk)
        done += len(chunk)
        if on_batch:
            on_batch(done, total)
    return found


def list_ignored_cookie(rows: Sequence[Mapping], states: Mapping[int, Mapping]) -> bool:
    """True when every list row looks untouched but a saved game is solved or filled.

    A blank open looks the same on the list as a puzzle you never started, so
    those rows are not treated as proof that the cookie was ignored.
    """
    if any(row.get("solved") or fill_percent(row, None) > 0 for row in rows):
        return False
    for state in states.values():
        if state_marks_solved(state) or fill_percent({"percent_filled": None}, state) > 0:
            return True
    return False


def scan(
    client: NytClient,
    publish_type: str,
    start: date,
    end: date,
    check_opened_empty: bool = True,
    log: Callable[[str], None] = lambda _message: None,
) -> ScanResult:
    if publish_type not in PUBLISH_TYPES:
        raise NytError(f"Unknown puzzle type {publish_type!r}. Use {', '.join(PUBLISH_TYPES)}.")
    if start > end:
        raise NytError(f"Start date {start.isoformat()} is after end date {end.isoformat()}.")

    log(f"Reading {publish_type} puzzles from {start.isoformat()} through {end.isoformat()}.")
    probe_start = max(start, end - timedelta(days=6))
    probe_rows = client.list_puzzles(publish_type, probe_start, end)
    if not probe_rows and start < probe_start:
        wider_start = max(start, end - timedelta(days=60))
        probe_rows = client.list_puzzles(publish_type, wider_start, end)
    if probe_rows:
        # The list endpoint returns public "unsolved" rows when the cookie is
        # missing. Saved games return 403 instead, so this fails fast.
        client.latest_states(publish_type, [int(probe_rows[-1]["puzzle_id"])])

    def on_window(window_start: date, window_end: date, count: int) -> None:
        log(f"  {window_start.isoformat()} to {window_end.isoformat()}  ({count} puzzles)")

    rows = collect_puzzles(client, publish_type, start, end, on_window=on_window)
    if not rows:
        return ScanResult(publish_type=publish_type, start=start, end=end)

    needs_state = [
        int(row["puzzle_id"])
        for row in rows
        if not row.get("solved") and fill_percent(row, None) == 0
    ]
    states: dict[int, dict] = {}
    if check_opened_empty and needs_state:
        log(
            f"Checking {len(needs_state)} unsolved {publish_type} puzzles for an empty saved game. "
            "Those count in the solve rate and do not show as filled in the archive."
        )

        def on_batch(done: int, total: int) -> None:
            if done == total or done % (STATE_BATCH * 5) == 0:
                log(f"  saved games {done}/{total}")

        states = fetch_states_for(client, publish_type, needs_state, on_batch=on_batch)
        if list_ignored_cookie(rows, states):
            raise AuthError(
                "The puzzle list came back without your progress. The NYT-S value was not accepted on that request. "
                "Copy only the cookie value, not the whole Cookie header label, and try again."
            )

    puzzles = [make_puzzle(row, states.get(int(row["puzzle_id"]))) for row in rows]
    return ScanResult(publish_type=publish_type, start=start, end=end, puzzles=puzzles)


def format_percent(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate:.2f}%"


def render_text(results: Sequence[ScanResult]) -> str:
    lines: list[str] = [
        "Solve rate counts every puzzle you opened, including one you left blank.",
        "The archive's unfinished list often omits those blank ones.",
        "",
    ]
    for result in results:
        unfinished = result.by_status("in_progress") + result.by_status("opened_empty")
        unfinished.sort(key=lambda puzzle: puzzle.print_date)
        partial = result.by_status("solved_partial_grid")
        rate = result.solve_rate()
        lines.append(
            f"{result.publish_type}  {result.start.isoformat()} through {result.end.isoformat()}"
        )
        lines.append(f"  scanned {len(result.puzzles)}")
        lines.append(f"  solved {result.solved_count}")
        lines.append(f"  in progress, letters in the grid: {len(result.by_status('in_progress'))}")
        lines.append(f"  opened, no letters: {len(result.by_status('opened_empty'))}")
        lines.append(f"  never opened: {len(result.by_status('never_opened'))}")
        if rate is None:
            lines.append("  solve rate: no opened puzzles in this range")
        else:
            lines.append(
                f"  solve rate among puzzles you opened: {format_percent(rate)} "
                f"({result.solved_count}/{result.opened_count})"
            )
            lines.append(
                f"  that rate rounds to {rate:.1f}% at one decimal, which is how the stats page displays it"
            )
        if unfinished:
            lines.append("  unfinished:")
            for puzzle in unfinished:
                title = f"  {puzzle.title}" if puzzle.title else ""
                kind = "opened, no letters" if puzzle.status == "opened_empty" else f"{puzzle.percent_filled}% filled"
                lines.append(
                    f"    {puzzle.print_date}  {puzzle.weekday:<9}  {kind:<22}  {puzzle.author}{title}"
                )
                lines.append(f"      {puzzle.url()}")
        else:
            lines.append("  unfinished: none in this range")
        if partial:
            lines.append("  solved, grid stored under 100% (still counts as solved):")
            for puzzle in partial:
                star = f" {puzzle.star}" if puzzle.star else ""
                lines.append(
                    f"    {puzzle.print_date}  {puzzle.weekday:<9}  {puzzle.percent_filled}%{star}  {puzzle.author}"
                )
                lines.append(f"      {puzzle.url()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(results: Sequence[ScanResult]) -> str:
    payload = []
    for result in results:
        def dump(status: str) -> list[dict]:
            return [
                {
                    "print_date": puzzle.print_date,
                    "weekday": puzzle.weekday,
                    "title": puzzle.title,
                    "author": puzzle.author,
                    "puzzle_id": puzzle.puzzle_id,
                    "percent_filled": puzzle.percent_filled,
                    "star": puzzle.star,
                    "status": puzzle.status,
                    "url": puzzle.url(),
                }
                for puzzle in result.by_status(status)
            ]

        rate = result.solve_rate()
        payload.append(
            {
                "publish_type": result.publish_type,
                "start": result.start.isoformat(),
                "end": result.end.isoformat(),
                "scanned": len(result.puzzles),
                "solved": result.solved_count,
                "in_progress": dump("in_progress"),
                "opened_empty": dump("opened_empty"),
                "solved_partial_grid": dump("solved_partial_grid"),
                "never_opened": len(result.by_status("never_opened")),
                "solve_rate": None if rate is None else round(rate, 4),
            }
        )
    return json.dumps(payload, indent=2) + "\n"


def write_csv(results: Iterable[ScanResult], destination) -> None:
    writer = csv.DictWriter(
        destination,
        fieldnames=[
            "publish_type",
            "print_date",
            "weekday",
            "status",
            "percent_filled",
            "star",
            "title",
            "author",
            "puzzle_id",
            "url",
        ],
    )
    writer.writeheader()
    for result in results:
        interesting = [
            puzzle
            for puzzle in result.puzzles
            if puzzle.status in {"in_progress", "opened_empty", "solved_partial_grid"}
        ]
        for puzzle in interesting:
            writer.writerow(
                {
                    "publish_type": puzzle.publish_type,
                    "print_date": puzzle.print_date,
                    "weekday": puzzle.weekday,
                    "status": puzzle.status,
                    "percent_filled": puzzle.percent_filled,
                    "star": puzzle.star or "",
                    "title": puzzle.title,
                    "author": puzzle.author,
                    "puzzle_id": puzzle.puzzle_id,
                    "url": puzzle.url(),
                }
            )


def today_in_new_york() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def load_cookie(cookie_file: str | None) -> str:
    import os

    if cookie_file:
        with open(cookie_file, encoding="utf-8") as handle:
            cookie = normalize_cookie(handle.read())
    elif os.environ.get("NYT_S"):
        cookie = normalize_cookie(os.environ["NYT_S"])
    elif sys.stdin.isatty():
        cookie = normalize_cookie(getpass("NYT-S cookie (input hidden): "))
    else:
        raise AuthError(
            "Set NYT_S or pass --cookie-file. The cookie is not read from the command line, so it stays out of shell history."
        )
    if len(cookie) < 20:
        raise AuthError("That NYT-S value is too short to be a session cookie.")
    return cookie


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List NYT crossword puzzles you opened but did not finish.",
    )
    parser.add_argument(
        "--types",
        default="daily",
        help="Comma-separated puzzle types: daily, mini, bonus. Default: daily. The stats-page solve rate is the daily.",
    )
    parser.add_argument("--start", help="First print date, YYYY-MM-DD. Default depends on the puzzle type.")
    parser.add_argument("--end", help="Last print date, YYYY-MM-DD. Default: today in New York.")
    parser.add_argument(
        "--cookie-file",
        help="File containing the NYT-S cookie. Overrides the NYT_S environment variable.",
    )
    parser.add_argument(
        "--skip-empty-check",
        action="store_true",
        help="Only report puzzles with letters in the grid. Skips the slower check for puzzles you opened and left blank.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument("--csv", help="Also write unfinished and partial-grid rows to this CSV path.")
    parser.add_argument("--pause", type=float, default=0.1, help="Seconds to wait between NYT requests. Default: 0.1.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    types = [part.strip().lower() for part in args.types.split(",") if part.strip()]
    unknown = [name for name in types if name not in PUBLISH_TYPES]
    if unknown or not types:
        print(
            f"Unknown puzzle type. Use {', '.join(PUBLISH_TYPES)}.",
            file=sys.stderr,
        )
        return 2
    try:
        end = date.fromisoformat(args.end) if args.end else today_in_new_york()
        cookie = load_cookie(args.cookie_file)
        client = NytClient(cookie, pause_seconds=max(0.0, args.pause))
        results = []
        for publish_type in types:
            start = date.fromisoformat(args.start) if args.start else DEFAULT_START[publish_type]
            results.append(
                scan(
                    client,
                    publish_type,
                    start,
                    end,
                    check_opened_empty=not args.skip_empty_check,
                    log=lambda message: print(message, file=sys.stderr),
                )
            )
    except AuthError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (NytError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(render_json(results))
    else:
        sys.stdout.write(render_text(results))
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as handle:
            write_csv(results, handle)
        print(f"Wrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

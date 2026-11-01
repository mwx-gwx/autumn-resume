# Unfinished NYT crosswords

The crossword stats percentage is not “share of the archive you have solved.” It is the share of puzzles **you opened** that you then finished. A puzzle you open and leave blank still counts as unfinished. It does not get a color in the archive, and the site’s In Progress list often leaves it out. That is how the list can be empty while the percentage sits at 99.9.

`find_unfinished.py` reads your saved crossword progress and prints the puzzles that are still open: grids with letters in them, and puzzles you launched but never filled.

## Setup

Python 3.11 or newer. No packages to install.

The script needs the `NYT-S` cookie from a browser where you are logged into your NYT account. That cookie is a login credential. It lasts for months. Do not commit it, paste it into a chat, or pass it as a command-line argument (shell history keeps those).

1. Open [nytimes.com/crosswords](https://www.nytimes.com/crosswords) while logged in.
2. Open the browser developer tools, choose the Network tab, and refresh the page.
3. Select any request to `nytimes.com`. In the request headers, find the `Cookie` line and copy only the value of `NYT-S` (the long string after `NYT-S=`).
4. Save it in a file only you can read:

```bash
umask 077
printf '%s' 'paste-the-value-here' > nyt-s.cookie
```

## Run

Daily puzzles, from the start of the digital archive (21 November 1993) through today:

```bash
python3 find_unfinished.py --cookie-file nyt-s.cookie
```

You can also set `NYT_S` in the environment instead of a file. If neither is set and you are at a terminal, the script prompts without echoing.

Mini and bonus puzzles are separate from the daily solve rate:

```bash
python3 find_unfinished.py --cookie-file nyt-s.cookie --types daily,mini,bonus
```

A recent window, plus a CSV of the puzzles worth opening:

```bash
python3 find_unfinished.py --cookie-file nyt-s.cookie --start 2020-01-01 --csv unfinished.csv
```

`--json` prints the same findings as JSON. `--skip-empty-check` only reports puzzles that already have letters in the grid. That is faster, and it misses the blank opens that move the percentage.

A full daily scan is a few hundred requests, paced at about ten per second. Narrow `--start` if you only care about recent years.

## How to read the result

- **in progress** — you entered letters and did not get a solve. These usually show up in the archive.
- **opened, no letters** — NYT has a saved game and the grid is empty. These count against the solve rate and look the same as a puzzle you never touched.
- **never opened** — not part of the percentage.
- **solved, grid stored under 100%** — the puzzle already counts as solved. Some Sunday tricks and special entries store this way. Leaving them alone does not raise the percentage.

The solve rate line is `solved / opened`. The stats page shows that number to one decimal place, so 99.94% displays as 99.9%.

Each unfinished row includes a link that opens that puzzle while you are logged in.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use fake responses. They do not call NYT.

# jev-joust

Two [TypeSafe Jev](https://typesafe.ai) players, one per controller, in NES Joust. Requires `rom/joust.nes`.

![Jev vs Jev](runs/jev-vs-jev-20260919-144016.gif)

One match per pairing, 2026-09-19, `jev-latest`, 2-player game A, up to 7200 frames:

| player 1 | player 2 | winner | frames | respawns left | score | Jev calls | cost |
|---|---|---|---|---|---|---|---|
| jev | jev | player 2 | 3648 | 0 / 1 | 500 / 2750 | 608 | $0.018 |
| jev | rules | player 2 (rules) | 2148 | 0 / 3 | 250 / 1000 | 179 | $0.006 |
| rules | jev | player 2 (jev) | 1092 | 0 / 4 | 1000 / 1000 | 91 | $0.003 |
| rules | rules | player 1 | 4020 | 0 / 0 | 0 / 3000 | 0 | $0 |

Median Jev latency 0.19 s, about 720 input tokens per call.

## Run

```bash
cp .env.example .env        # TYPESAFE_API_KEY
uv sync
uv run python joust.py --play --p1 jev --p2 jev       # a match; bots are jev, rules or idle
uv run python joust.py --play --p1 jev --p2 rules --max-frames 7200
uv run python joust.py --scan       # hold each input on each controller, print RAM addresses that move with it
uv run python joust.py --frames     # scripted inputs on both controllers, writes runs/smoke.gif
```

A match writes `runs/<p1>-vs-<p2>-<stamp>.gif`, a `.json` with every state, answer and controller plan, and a line in `runs/results.jsonl`. It ends when a player is out of lives or at `--max-frames` (default 3600, one minute).

## Notes

- Every 12 frames each Jev player gets its own view of the game as JSON (itself, then every other rider nearest first, as a sentence plus numbers) and answers one Choice: `attack`, `climb` or `evade`. Code turns the tactic into direction and flaps. Both requests run concurrently; the emulator waits for them.
- `rules` picks among the same three tactics with fixed thresholds on the nearest rider and shares the executor, so a match between `jev` and `rules` compares the choice alone.
- Platforms and eggs are not in the state.
- RAM map (no published one; found with `--scan`, by matching bytes against sprite positions, and by reading the HUD):

| bytes | meaning |
|---|---|
| `0x54+p`, `0x58+p` | player x, y; y = 240 while dead |
| `0xE9+p` | respawns left |
| `0xEB..0xED`, `0xEE..0xF0` | score, BCD, low byte first |
| `0x720+i`, `0x72E+i`, i < 7 | enemy x, y; y = 240 is an empty slot |

- Boot: the title appears near frame 600; Select three times moves the cursor to `2 PLAYER GAME A`, then Start.
- nes-py wires only controller 0 through `step()`; `Joust.step(p1, p2)` writes controller 1's buffer before stepping.
- Snapshot/restore via nes-py's `_backup`/`_restore`, as in [jev-mario](https://github.com/4esv/jev-mario).

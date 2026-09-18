# jev-joust

Two [TypeSafe Jev](https://typesafe.ai) players, one on each controller, in NES Joust. Bring your own ROM: `rom/joust.nes`, never committed.

Work in progress.

```bash
cp .env.example .env
uv sync
uv run python joust.py --scan            # find RAM offsets by moving each player and diffing RAM
```

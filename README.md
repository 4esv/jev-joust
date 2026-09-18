# jev-joust

Two [TypeSafe Jev](https://typesafe.ai) players, one per controller, in NES Joust. Requires `rom/joust.nes`.

Work in progress.

```bash
cp .env.example .env
uv sync
uv run python joust.py --scan            # find RAM offsets by moving each player and diffing RAM
```

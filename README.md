# jev-joust

Two [TypeSafe Jev](https://typesafe.ai) players, one per controller, in NES Joust. Requires `rom/joust.nes`. Work in progress.

## Run

```bash
cp .env.example .env        # TYPESAFE_API_KEY
uv sync
uv run python joust.py --scan       # hold each input on each controller, print RAM addresses that move with it
uv run python joust.py --frames     # scripted inputs on both controllers, writes runs/smoke.gif
```

## Notes

- nes-py wires only controller 0 through `step()`; `Joust.step(p1, p2)` writes controller 1's buffer before stepping.
- Snapshot/restore via nes-py's `_backup`/`_restore`, as in [jev-mario](https://github.com/4esv/jev-mario).
- No published RAM map; `--scan` locates player and enemy bytes by differencing RAM under held inputs.

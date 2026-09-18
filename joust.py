"""NES Joust with two controllers driven by code. Bring your own ROM at rom/joust.nes.

    uv run python joust.py --scan     # interactive RAM scan: find the bytes that track each player
    uv run python joust.py --frames   # step a few hundred frames with scripted inputs and save a GIF

The scan drives one controller at a time and reports RAM addresses whose values move with the input,
which is how the player x/y, lives and enemy slots get located without a published RAM map.
"""

import argparse
import contextlib
import io
import warnings
from pathlib import Path

import imageio.v2 as imageio
import numpy as np

warnings.filterwarnings("ignore")
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from nes_py import NESEnv

ROM = Path(__file__).resolve().parent / "rom" / "joust.nes"
RUNS = Path(__file__).resolve().parent / "runs"

# Controller bits in nes-py's byte: right, left, down, up, start, select, B, A (bit 7 .. bit 0 as listed here)
BTN = {"right": 0x80, "left": 0x40, "down": 0x20, "up": 0x10, "start": 0x08, "select": 0x04, "B": 0x02, "A": 0x01}


def press(*names: str) -> int:
    return sum(BTN[n] for n in names)


class Joust:
    """Two-controller stepping over nes-py, which only wires controller 0 through step()."""

    def __init__(self):
        if not ROM.exists():
            raise SystemExit(f"no ROM at {ROM}; see rom/README.md")
        self.env = NESEnv(str(ROM))
        self.env.reset()
        self.ram = self.env.ram

    def step(self, p1: int, p2: int):
        self.env.controllers[1][:] = p2
        obs, _, done, info = self.env.step(p1)
        return obs, done

    def snapshot(self):
        self.env._backup()

    def rewind(self):
        self.env._restore()
        self.env.done = False


def scan(game: Joust, presses: dict[str, int], frames: int = 60) -> None:
    """For each input, hold it for `frames` and print RAM addresses that changed in a consistent direction."""
    base = game.ram.copy()
    for _ in range(120):
        game.step(0, 0)
    for name, (p1, p2) in presses.items():
        game.snapshot()
        before = game.ram.copy()
        deltas = np.zeros(len(before), dtype=int)
        prev = before.copy()
        for _ in range(frames):
            game.step(p1, p2)
            cur = game.ram.copy()
            deltas += np.sign(cur.astype(int) - prev.astype(int))
            prev = cur
        moved = [(i, int(before[i]), int(prev[i]), int(deltas[i])) for i in range(len(before))
                 if abs(deltas[i]) >= frames // 3 and before[i] != prev[i]]
        game.rewind()
        print(f"{name}: {len(moved)} addresses moved consistently")
        for i, b, a, d in moved[:24]:
            print(f"  0x{i:04X}: {b:3d} -> {a:3d}  (trend {d:+d})")


def frames_gif(game: Joust, n: int = 300) -> None:
    RUNS.mkdir(exist_ok=True)
    frames = []
    for i in range(n):
        p1 = press("right", "A") if (i // 10) % 2 == 0 else press("right")
        p2 = press("left", "A") if (i // 15) % 2 == 0 else press("left")
        obs, done = game.step(p1, p2)
        if i % 2 == 0:
            frames.append(obs.copy())
        if done:
            break
    imageio.mimsave(RUNS / "smoke.gif", frames, duration=1 / 30, loop=0)
    print(f"wrote runs/smoke.gif, {len(frames)} frames")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--frames", action="store_true")
    a = ap.parse_args()
    game = Joust()
    for _ in range(300):  # title screen: press start for two players
        game.step(press("start") if _ % 30 == 0 else 0, 0)
    if a.scan:
        scan(game, {
            "p1 right": (press("right"), 0), "p1 left": (press("left"), 0), "p1 flap": (press("A"), 0),
            "p2 right": (0, press("right")), "p2 left": (0, press("left")), "p2 flap": (0, press("A")),
        })
    if a.frames:
        frames_gif(game)

"""NES Joust with two controllers driven by code. Bring your own ROM at rom/joust.nes.

    uv run python joust.py --scan     # interactive RAM scan: find the bytes that track each player
    uv run python joust.py --frames   # step a few hundred frames with scripted inputs and save a GIF
    uv run python joust.py --play --p1 jev --p2 jev     # a match; bots are jev, rules or idle

The scan drives one controller at a time and reports RAM addresses whose values move with the input,
which is how the player x/y, lives and enemy slots get located without a published RAM map.
"""

import argparse
import contextlib
import io
import json
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

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
        self.boot()

    def boot(self):
        """Title (up at ~frame 600) -> menu -> cursor to '2 PLAYER GAME A' -> start."""
        for name, wait in (("select", 620), ("select", 40), ("select", 40), ("start", 40)):
            for _ in range(wait):
                self.step(0, 0)
            for _ in range(4):
                self.step(press(name), 0)
        for _ in range(600):  # lives are set ~70 frames after start, riders spawn ~130
            if 0 < int(self.ram[0x58]) < 240 and 0 < int(self.ram[0x59]) < 240:
                return
            self.step(0, 0)
        raise SystemExit("two-player game did not start")

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
        for f in range(frames):
            tap = name.endswith("flap") and f % 8 >= 4  # flapping is per press, not per hold
            game.step(0 if tap else p1, 0 if tap else p2)
            cur = game.ram.copy()
            deltas += np.sign(cur.astype(int) - prev.astype(int))
            prev = cur
        moved = [(i, int(before[i]), int(prev[i]), int(deltas[i])) for i in range(len(before))
                 if abs(deltas[i]) >= frames // 3 and before[i] != prev[i]]
        game.rewind()
        print(f"{name}: {len(moved)} addresses moved consistently")
        for i, b, a, d in moved[:24]:
            print(f"  0x{i:04X}: {b:3d} -> {a:3d}  (trend {d:+d})")


# RAM map, located with --scan and by matching bytes against sprite positions and the HUD.
P_X, P_Y, P_LIVES = 0x54, 0x58, 0xE9  # + player index
E_X, E_Y, E_SLOTS, OFF = 0x720, 0x72E, 7, 240  # enemy slot arrays; y == 240 is off screen: empty slot, or a dead player
P_SCORE = (0xEB, 0xEE)  # three BCD bytes each, low byte first

JEV_URL = "https://api.typesafe.ai/v1/systemone"
USD_PER_TOKEN = 0.042 / 1e6
DECIDE = 12  # frames per decision; one flap is one press of A
FLAP_FRAMES = {0: (), 1: (0, 1, 2), 2: (0, 1, 2, 6, 7, 8)}
TACTICS = {
    "attack": "a rider is clearly below you and close enough to reach: fly into it from above",
    "climb": "the nearest rider is level with you or above you: gain height before engaging anyone",
    "evade": "a rider above you is close or closing in: get away from it sideways while gaining height",
}
INSTRUCTIONS = {
    "goal": "You ride a flying bird in Joust. When two riders touch, the higher one wins and the lower one "
            "is killed. The rival player is a target like the enemies. Choose the tactic for the next fifth "
            "of a second.",
    "state": "`others` lists every other rider, nearest first, as a sentence plus numbers: dx is pixels to "
             "your right (negative is left; the screen wraps), dy is pixels above you (negative is below).",
}


def wrap(d: int) -> int:
    return (d + 128) % 256 - 128


def riders(ram) -> list[dict]:
    out = [{"who": f"player {p + 1}", "x": int(ram[P_X + p]), "y": int(ram[P_Y + p]), "lives": int(ram[P_LIVES + p])}
           for p in (0, 1)]
    out += [{"who": f"enemy {i + 1}", "x": int(ram[E_X + i]), "y": int(ram[E_Y + i])}
            for i in range(E_SLOTS) if int(ram[E_Y + i]) != OFF]
    return out


def out_of_game(r: dict) -> bool:
    """Lives count respawns left; a player with none who is off screen is not coming back."""
    return r["lives"] == 0 and r["y"] == OFF


def score(ram, p: int) -> int:
    return int("".join(f"{int(ram[P_SCORE[p] + k]):02x}" for k in (2, 1, 0)))


def view(now: list[dict], before: list[dict], me: int, last: dict) -> dict:
    """The state one player sees: itself, and everyone else relative to it."""
    prev = {r["who"]: r for r in before}
    def vel(r):
        q = prev.get(r["who"], r)
        return wrap(r["x"] - q["x"]), q["y"] - r["y"]
    m = now[me]
    mvx, mvy = vel(m)
    others = []
    for r in now:
        if r is m or r["y"] == OFF:
            continue
        dx, dy = wrap(r["x"] - m["x"]), m["y"] - r["y"]
        vx, vy = vel(r)
        others.append({"who": "rival player" if r["who"].startswith("player") else r["who"],
                       "dx": dx, "dy": dy, "distance": abs(dx) + abs(dy), "vx": vx, "vy": vy,
                       "closing": abs(wrap(dx + vx - mvx)) + abs(dy + vy - mvy) < abs(dx) + abs(dy)})
        o = others[-1]
        o["summary"] = (f"{o['who']} is {abs(dy)} px {'above' if dy > 6 else 'below' if dy < -6 else 'level with'} you"
                        .replace("is 0 px level", "is level").replace(f"{abs(dy)} px level with", "level with")
                        + f", {abs(dx)} px to your {'right' if dx > 0 else 'left'}, "
                        + ("closing in" if o["closing"] else "moving away"))
    others.sort(key=lambda o: o["distance"])
    if m["y"] == OFF:
        return {"you": "dead, waiting to respawn", "others": others, "last_decision": last}
    return {"you": {"height_above_floor": 196 - m["y"], "vx": mvx, "vy": mvy, "lives": m["lives"]},
            "others": others, "last_decision": last}


def ask_jev(client: httpx.Client, state: dict) -> tuple[dict, int, float]:
    body = {"state": state, "model": "jev-latest", "questions": {
        "tactic": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": TACTICS}}}
    t0 = time.perf_counter()
    r = client.post(JEV_URL, headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}, json=body)
    lat = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    a = d["answers"]["tactic"]
    return {"tactic": a["choice"], "p": {k: round(v, 3) for k, v in a["probabilities"].items()}}, d["usage"]["input_tokens"], lat


def rules(state: dict) -> dict:
    """Reference bot: the same three tactics picked by fixed thresholds on the nearest rider."""
    o = (state["others"] or [{"dy": -99, "distance": 999}])[0]
    if o["dy"] > 6 and o["distance"] < 64:
        return {"tactic": "evade"}
    return {"tactic": "attack" if o["dy"] < -8 else "climb"}


def execute(tactic: str, state: dict) -> dict:
    """Tactic -> controller plan for one decision. Code flies; the bot only chooses what to do."""
    you, others = state["you"], state["others"]
    if not isinstance(you, dict) or not others:
        return {"move": "none", "flap": 1}
    near = others[0]
    side = lambda o, toward: ("right" if o["dx"] > 0 else "left") if toward else ("left" if o["dx"] > 0 else "right")
    if tactic == "evade":
        above = [o for o in others if o["dy"] > 6] or [near]
        return {"move": side(above[0], False), "flap": 2}
    if tactic == "attack":
        below = [o for o in others if o["dy"] < -6] or [near]
        t = below[0]
        return {"move": side(t, True), "flap": 0 if t["dy"] < -16 and you["vy"] >= -2 else 1}
    return {"move": side(near, False) if near["distance"] < 48 else "none", "flap": 2}


def play(game: Joust, bots: tuple[str, str], max_frames: int) -> dict:
    RUNS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    client, pool = httpx.Client(timeout=30), ThreadPoolExecutor(2)
    frames, log, tokens, lats = [], [], 0, []
    before = riders(game.ram)
    last = [{"tactic": "climb"}, {"tactic": "climb"}]
    plan = [{"move": "none", "flap": 0}] * 2
    start_lives = [r["lives"] for r in before[:2]]
    frame = 0

    def decide(p: int, state: dict) -> tuple[dict, int, float]:
        if bots[p] == "jev":
            return ask_jev(client, state)
        return (rules(state) if bots[p] == "rules" else {"tactic": "idle"}), 0, 0.0

    while frame < max_frames:
        now = riders(game.ram)
        if any(out_of_game(r) for r in now[:2]):
            break
        states = [view(now, before, p, last[p]) for p in (0, 1)]
        answers = list(pool.map(decide, (0, 1), states))
        for p, (act, tok, lat) in enumerate(answers):
            last[p] = {"tactic": act["tactic"]}
            plan[p] = execute(act["tactic"], states[p]) if act["tactic"] != "idle" else {"move": "none", "flap": 0}
            tokens += tok
            if lat:
                lats.append(lat)
        log.append({"frame": frame, "states": states, "answers": [a for a, _, _ in answers], "plans": list(plan)})
        before = now
        for f in range(DECIDE):
            pads = [(press(a["move"]) if a["move"] != "none" else 0) | (press("A") if f in FLAP_FRAMES[a["flap"]] else 0)
                    for a in plan]
            obs, _ = game.step(*pads)
            frame += 1
            if frame % 3 == 0:
                frames.append(obs.copy())

    end = riders(game.ram)[:2]
    outs = [out_of_game(r) for r in end]
    winner = None if outs[0] == outs[1] else bots[outs.index(False)] + f" (player {outs.index(False) + 1})"
    result = {"bots": bots, "frames": frame, "winner": winner, "lives": [r["lives"] for r in end], "start_lives": start_lives,
              "score": [score(game.ram, 0), score(game.ram, 1)], "decisions": len(log),
              "jev_calls": len(lats), "input_tokens": tokens, "cost_usd": round(tokens * USD_PER_TOKEN, 5),
              "latency_p50_s": round(float(np.median(lats)), 3) if lats else None}
    name = f"{bots[0]}-vs-{bots[1]}-{stamp}"
    imageio.mimsave(RUNS / f"{name}.gif", frames, duration=1 / 20, loop=0)
    (RUNS / f"{name}.json").write_text(json.dumps({"result": result, "log": log}))
    with (RUNS / "results.jsonl").open("a") as fh:
        fh.write(json.dumps({"run": name, **result}) + "\n")
    print(json.dumps(result))
    print(f"wrote runs/{name}.gif and .json")
    return result


def load_env() -> None:
    env = Path(__file__).resolve().parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


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
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--p1", default="jev", choices=["jev", "rules", "idle"])
    ap.add_argument("--p2", default="jev", choices=["jev", "rules", "idle"])
    ap.add_argument("--max-frames", type=int, default=3600)
    a = ap.parse_args()
    game = Joust()
    if a.scan:
        scan(game, {
            "p1 right": (press("right"), 0), "p1 left": (press("left"), 0), "p1 flap": (press("A"), 0),
            "p2 right": (0, press("right")), "p2 left": (0, press("left")), "p2 flap": (0, press("A")),
        })
    if a.frames:
        frames_gif(game)
    if a.play:
        load_env()
        if "jev" in (a.p1, a.p2) and not os.environ.get("TYPESAFE_API_KEY"):
            raise SystemExit("set TYPESAFE_API_KEY in .env")
        play(game, (a.p1, a.p2), a.max_frames)

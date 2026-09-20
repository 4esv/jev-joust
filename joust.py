"""NES Joust with two controllers driven by code. Bring your own ROM at rom/joust.nes.

    uv run python joust.py --scan     # interactive RAM scan: find the bytes that track each player
    uv run python joust.py --frames   # step a few hundred frames with scripted inputs and save a GIF
    uv run python joust.py --play --p1 jev --p2 jev     # a match; bots are jev, jev-tactic, rules or idle

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
                for _ in range(24):  # the reserve counter decrements as each rider materializes; let it settle
                    self.step(0, 0)
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
OAM, EGG_TILE = 0x200, 204  # eggs are not in the object tables; they render as this sprite tile
FLOOR = 196

HORIZON = 24  # frames the candidate is held, 0.4 s
TAIL = 72  # then it keeps flying neutrally for this long, so the option text can say whether it dies soon
# Each candidate is a controller state held for the horizon. Flapping is one press of A per 4 frames.
CANDIDATES = {"fly left and flap": ("left", True), "fly left and glide": ("left", False),
              "fly right and flap": ("right", True), "fly right and glide": ("right", False),
              "hold course and flap": (None, True), "hold course and glide": (None, False)}

JEV_URL = "https://api.typesafe.ai/v1/systemone"
USD_PER_TOKEN = 0.042 / 1e6
DECIDE = 12  # frames per decision; half the simulated horizon
# One flap is one press of A. Measured from open air: no flaps falls 90 px in 48 frames, one per 12 frames
# still falls 49, one per 6 holds height, one per 4 climbs 38.
FLAP_PERIOD = {0: None, 1: 6, 2: 4}
MOVES = {"left": "fly left", "right": "fly right", "none": "keep the current drift"}
TACTICS = {
    "attack": "a rider is clearly below you and close enough to reach: fly into it from above",
    "climb": "the nearest rider is level with you or above you: gain height before engaging anyone",
    "evade": "a rider above you is close or closing in: get away from it sideways while gaining height",
}
INSTRUCTIONS = {
    "goal": "You ride a flying bird in Joust. When two riders touch, the higher one wins and the lower one "
            "is killed. The rival player is a target like the enemies.",
    "state": "`others` lists every other rider, nearest first, as a sentence plus numbers: dx is pixels to "
             "your right (negative is left; the screen wraps), dy is pixels above you (negative is below).",
}


def eggs(ram) -> list[dict]:
    """Uncollected eggs, read from the sprite table; each is 250+ points for whoever reaches it."""
    return [{"x": int(ram[OAM + 4 * k + 3]), "y": int(ram[OAM + 4 * k])}
            for k in range(64) if int(ram[OAM + 4 * k + 1]) == EGG_TILE and int(ram[OAM + 4 * k]) < FLOOR]


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


def ask_jev_direct(client: httpx.Client, state: dict) -> tuple[dict, int, float]:
    """Jev on the controller: a Choice for the stick and a Noul for the flap button."""
    body = {"state": state, "model": "jev-latest", "questions": {
        "move": {"type": "choice", "instructions": [INSTRUCTIONS, "Which way should you fly now?"], "criteria": MOVES},
        "flap": {"type": "noul", "instructions": [INSTRUCTIONS, "Should you be flapping now? Flapping climbs; "
                 "not flapping falls, faster the longer it lasts. `you.height_above_floor` 0 is the floor."],
                 "criteria": {"true": "gain height: someone near is level with you or above you, or you are low",
                              "false": "lose height: you are well above your target and should drop onto it"}}}}
    t0 = time.perf_counter()
    r = client.post(JEV_URL, headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}, json=body)
    lat = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    a = d["answers"]
    return {"move": a["move"]["choice"], "flap_p": round(a["flap"]["noul"], 3),
            "move_p": {k: round(v, 3) for k, v in a["move"]["probabilities"].items()}}, d["usage"]["input_tokens"], lat


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


def pad_for(direction: str | None, flap: bool, f: int) -> int:
    """One frame of a held candidate: a direction plus a flap every 4 frames (the measured climb rate)."""
    return (press(direction) if direction else 0) | (press("A") if flap and f % 4 < 2 else 0)


def relation(r, me: int) -> tuple[dict | None, list[dict]]:
    """The nearest other rider and the eggs, relative to player `me`."""
    rs = riders(r)
    m = rs[me]
    others = [o for o in rs if o is not m and o["y"] != OFF]
    for o in others:
        # dy is positive when the other rider is higher on screen, that is above you and winning a contact.
        o["dx"], o["dy"] = wrap(o["x"] - m["x"]), m["y"] - o["y"]
        o["dist"] = abs(o["dx"]) + abs(o["dy"])
    others.sort(key=lambda o: o["dist"])
    es = eggs(r)
    for e in es:
        e["dist"] = abs(wrap(e["x"] - m["x"])) + abs(e["y"] - m["y"])
    es.sort(key=lambda e: e["dist"])
    return (others[0] if others else None), es


def simulate(game: "Joust", me: int, cand: str, other: tuple) -> dict:
    """Hold one candidate for HORIZON frames, then fly neutrally for TAIL, and report what actually
    happened. The position reported is where the held part put you; the death covers the whole window."""
    d, fl = CANDIDATES[cand]
    r = game.ram
    s0 = score(r, me)
    alive0 = int(r[P_Y + me]) != OFF
    near0, eggs0 = relation(r, me)
    # NOTE: a death is the rider leaving play, not the lives counter moving: that counter holds reserves and
    # decrements when a rider materializes, so at spawn it drops with nobody touching anyone.
    # The candidate is held for HORIZON and then flown neutrally for TAIL, so a death that the held part
    # only sets up still shows up in the option text. Measuring only the held part hides every death here.
    died = False
    snap = None
    for f in range(HORIZON + TAIL):
        pads = [0, 0]
        pads[me] = pad_for(d, fl, f) if f < HORIZON else pad_for(None, True, f)
        pads[1 - me] = pad_for(other[0], other[1], f)
        game.step(*pads)
        died = died or (alive0 and int(r[P_Y + me]) == OFF)
        if f == HORIZON - 1:  # the actionable part: where holding this option puts you
            near_h, eggs_h = relation(r, me)
            snap = (int(r[P_Y + me]), score(r, me) - s0, near_h, eggs_h)
    y, pts_h, near1, eggs1 = snap
    return {"died": died,
            "points": max(pts_h, score(r, me) - s0),
            "height": None if y == OFF else FLOOR - y,
            "near": near1,
            "closed_on_rider": bool(near0 and near1 and near1["dist"] < near0["dist"]),
            "eggs_left": len(eggs1),
            "egg_gain": (eggs0[0]["dist"] - eggs1[0]["dist"]) if eggs0 and eggs1 else 0,
            "egg_dist": eggs1[0]["dist"] if eggs1 else None}


def describe(o: dict) -> str:
    if o["died"]:
        return "you are killed" + (f" after scoring {o['points']} points" if o["points"] else "")
    bits = []
    if o["points"]:
        bits.append(f"scores {o['points']} points")
    bits.append(f"ends {o['height']} px above the floor")
    n = o["near"]
    if n:
        where = "above you" if n["dy"] > 6 else "below you" if n["dy"] < -6 else "level with you"
        who = "the rival player" if n["who"].startswith("player") else n["who"]
        # Name the points, not just the geometry: being above a rider and near it is what a kill is made of.
        if n["dy"] < -6 and n["dist"] < 56:
            verdict = ", you are above it and in range to kill it for 500 points"
        elif n["dy"] < -6:
            verdict = ", you are above it, so closing the gap scores"
        elif n["dy"] > 6 and n["dist"] < 56:
            verdict = ", it is above you and in range to kill you"
        elif n["dy"] > 6:
            verdict = ", it is above you, so closing the gap is fatal"
        else:
            verdict = ", level with you, which settles nothing"
        bits.append(f"{who} {abs(n['dy'])} px {where} and {n['dist']} px away"
                    + (" and closing" if o["closed_on_rider"] else "") + verdict)
    if o["egg_dist"] is not None:
        bits.append(f"nearest egg {o['egg_dist']} px away, worth 250 points for free"
                    + (" and you are closing on it" if o["egg_gain"] > 0 else ""))
    return ", ".join(bits)


def outcomes_for(game: "Joust", me: int, other: tuple) -> dict[str, dict]:
    """Simulate every candidate from the same instant. One snapshot slot, so rewind between each."""
    game.snapshot()
    out = {}
    for cand in CANDIDATES:
        out[cand] = simulate(game, me, cand, other)
        game.rewind()
    return out


def label_state(outs: dict[str, dict]) -> tuple[str, dict, bool]:
    """The candidate that actually turns out best over the same window the option text describes."""
    rank = lambda o: (not o["died"], o["points"])
    best = max(outs.items(), key=lambda kv: rank(kv[1]))[0]
    decisive = len({rank(o) for o in outs.values()}) > 1  # else every option is worth the same
    return best, {c: {"died": o["died"], "points": o["points"]} for c, o in outs.items()}, decisive


def situation(r, me: int) -> dict:
    """What the player can see right now; the options carry what happens next."""
    near, es = relation(r, me)
    rs = riders(r)
    m = rs[me]
    others = sorted([o for o in rs if o is not m and o["y"] != OFF],
                    key=lambda o: abs(wrap(o["x"] - m["x"])) + abs(m["y"] - o["y"]))[:3]
    def line(o):
        dx, dy = wrap(o["x"] - m["x"]), m["y"] - o["y"]
        return {"who": "the rival player" if o["who"].startswith("player") else o["who"],
                "summary": f"{abs(dy)} px {'above' if dy > 6 else 'below' if dy < -6 else 'level with'} you, "
                           f"{abs(dx)} px to your {'right' if dx > 0 else 'left'}"}
    return {"height_above_floor": FLOOR - m["y"] if m["y"] != OFF else None,
            "respawns_left": m["lives"], "score": score(r, me),
            "riders_near_you": [line(o) for o in others],
            "uncollected_eggs_on_screen": len(es)}


CANDIDATE_INSTRUCTIONS = (
    "You ride a flying bird in Joust and you are trying to score as many points as possible. Points come from "
    "killing riders and from collecting the eggs they leave behind. When two riders touch, the one that is "
    "higher kills the lower one, so height decides every fight. An egg is free points and costs nothing: "
    "collect one whenever no rider threatens you. The rival player is a target like the enemies.\n"
    "Each option below says what actually happens if you hold it for the next 0.4 seconds, measured by "
    "running the game forward. Pick the option that gains the most points without being killed. Never pick "
    "an option that kills you while another option survives. You get another decision half way through, "
    "so an option that sets up a kill is worth as much as one that scores now."
)


def ask_jev_candidates(client: httpx.Client, state: dict, criteria: dict[str, str]) -> tuple[dict, int, float]:
    body = {"state": state, "model": "jev-latest", "questions": {
        "action": {"type": "choice", "instructions": CANDIDATE_INSTRUCTIONS, "criteria": criteria}}}
    t0 = time.perf_counter()
    r = client.post(JEV_URL, headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"}, json=body)
    lat = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    a = d["answers"]["action"]
    return {"action": a["choice"], "p": {k: round(v, 3) for k, v in a["probabilities"].items()}}, d["usage"]["input_tokens"], lat


def greedy_pick(outs: dict[str, dict]) -> str:
    """Deterministic control over the same candidates: survive, take points, get above the nearest rider."""
    def rank(kv):
        c, o = kv
        n = o["near"]
        # -dy: prefer ending with the nearest rider below you, which is the side that wins a contact.
        return (not o["died"], o["points"], o["egg_gain"] if o["egg_dist"] is not None else 0,
                (-n["dy"] if n else 0), o["height"] or 0)
    return max(outs.items(), key=rank)[0]


def play(game: Joust, bots: tuple[str, str], max_frames: int) -> dict:
    RUNS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    client, pool = httpx.Client(timeout=30), ThreadPoolExecutor(2)
    frames, log, tokens, lats = [], [], 0, []
    before = riders(game.ram)
    last = [{}, {}]
    held = [(None, False), (None, False)]  # each player's last held candidate, used when simulating the other
    plan = [{"move": "none", "flap": 0}] * 2
    start_lives = [r["lives"] for r in before[:2]]
    frame = 0

    def decide(p: int, state: dict) -> tuple[dict, int, float]:
        if bots[p] == "jev":
            return ask_jev_candidates(client, state[0], state[1])
        if bots[p] == "jev-noul":
            return ask_jev_direct(client, state)
        if bots[p] == "jev-tactic":
            return ask_jev(client, state)
        if bots[p] == "greedy":
            return {"action": greedy_pick(state[2])}, 0, 0.0
        return (rules(state) if bots[p] == "rules" else {"tactic": "idle"}), 0, 0.0

    while frame < max_frames:
        now = riders(game.ram)
        if any(out_of_game(r) for r in now[:2]):
            break
        states = []
        for p in (0, 1):
            if bots[p] in ("jev", "greedy"):
                outs = outcomes_for(game, p, held[1 - p])
                states.append((situation(game.ram, p), {c: describe(o) for c, o in outs.items()}, outs))
            else:
                states.append(view(now, before, p, last[p]))
        answers = list(pool.map(decide, (0, 1), states))
        for p, (act, tok, lat) in enumerate(answers):
            if "action" in act:  # candidate search: the answer names a held controller state
                d, fl = CANDIDATES[act["action"]]
                held[p] = (d, fl)
                plan[p] = {"cand": act["action"]}
                last[p] = {"action": act["action"]}
            elif "flap_p" in act:  # direct control: the answers are the buttons
                plan[p] = {"move": act["move"], "flap": 2 if act["flap_p"] >= 0.5 else 0}
                last[p] = dict(plan[p], flap="flapping" if plan[p]["flap"] else "not flapping")
            else:
                plan[p] = execute(act["tactic"], states[p]) if act["tactic"] != "idle" else {"move": "none", "flap": 0}
                last[p] = {"tactic": act["tactic"]}
            tokens += tok
            if lat:
                lats.append(lat)
        log.append({"frame": frame, "states": states, "answers": [a for a, _, _ in answers], "plans": list(plan),
                    "options": [s[1] if isinstance(s, tuple) else None for s in states]})
        before = now
        for f in range(DECIDE):
            pads = [pad_for(*CANDIDATES[a["cand"]], f) if "cand" in a else
                    (press(a["move"]) if a["move"] != "none" else 0)
                    | (press("A") if FLAP_PERIOD[a["flap"]] and frame % FLAP_PERIOD[a["flap"]] < 2 else 0) for a in plan]
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


DATA = Path(__file__).resolve().parent / "data"


def record(game: "Joust", n: int) -> None:
    """Play greedy and save decision states, each labelled by which option the game says turns out best."""
    DATA.mkdir(exist_ok=True)
    rows, held, frame, seen, restarts = [], [(None, False), (None, False)], 0, 0, 0
    while len(rows) < n:
        for p in (0, 1):
            if int(game.ram[P_Y + p]) == OFF:
                continue
            outs = outcomes_for(game, p, held[1 - p])
            gold, detail, decisive = label_state(outs)
            seen += 1
            if decisive:  # keep only states where the options are genuinely worth different amounts
                rows.append({"id": f"s{len(rows):04d}", "frame": frame, "player": p,
                             "situation": situation(game.ram, p),
                             "options": {c: describe(o) for c, o in outs.items()},
                             "label": gold, "rollout": detail})
            held[p] = CANDIDATES[greedy_pick(outs)]
            if len(rows) >= n:
                break
        for f in range(DECIDE):
            game.step(pad_for(*held[0], f), pad_for(*held[1], f))
            frame += 1
        if any(out_of_game(r) for r in riders(game.ram)[:2]) or frame > 20000:
            restarts += 1
            print(f"  game over after {frame} frames, {len(rows)} kept; restarting")
            game.__init__()
            held, frame = [(None, False), (None, False)], 0
            if restarts > 4:
                break
    (DATA / "states.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    counts = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"wrote data/states.jsonl, {len(rows)} of {seen} decisions kept as decisive; label counts {counts}")


def greedy_from_text(r: dict) -> str:
    """The deterministic control, scored on the same recorded states: it reads the same option sentences."""
    def rank(kv):
        c, t = kv
        dy = 0
        if " px above you" in t:
            dy = 1
        elif " px below you" in t:
            dy = -1
        return ("killed" not in t, "scores" in t, -dy, int(t.split("ends ")[1].split(" px")[0]) if "ends " in t else 0)
    return max(r["options"].items(), key=rank)[0]


def offline() -> None:
    """Ask Jev each recorded state and score it against the rollout label. No emulator, no match."""
    rows = [json.loads(l) for l in (DATA / "states.jsonl").read_text().splitlines()]
    client = httpx.Client(timeout=30)
    def best_set(r) -> set:
        """Every candidate that ties for the best rollout; picking any of them is optimal."""
        rank = lambda v: (not v["died"], v["points"])
        top = max(rank(v) for v in r["rollout"].values())
        return {c for c, v in r["rollout"].items() if rank(v) == top}

    ok = fatal = tokens = 0
    lats, picked, confs = [], {}, []
    for r in rows:
        ans, tok, lat = ask_jev_candidates(client, r["situation"], r["options"])
        tokens += tok
        lats.append(lat)
        picked[ans["action"]] = picked.get(ans["action"], 0) + 1
        ok += ans["action"] in best_set(r)
        confs.append(max(ans["p"].values()))
        dies = r["options"][ans["action"]].startswith("you are killed")
        safe = any(not t.startswith("you are killed") for t in r["options"].values())
        fatal += dies and safe
    n = len(rows)
    # Baselines over the same states: always play the single most often optimal option, and pick at random.
    fixed = max(CANDIDATES, key=lambda c: sum(c in best_set(r) for r in rows))
    chance = float(np.mean([len(best_set(r)) / len(CANDIDATES) for r in rows]))
    greedy_ok = sum(greedy_from_text(r) in best_set(r) for r in rows)
    print(json.dumps({"n": n, "jev_optimal": round(ok / n, 3),
                      "best_fixed_option": f"{fixed} {sum(fixed in best_set(r) for r in rows) / n:.3f}",
                      "random_pick": round(chance, 3),
                      "greedy_optimal": round(greedy_ok / n, 3),
                      "chose_an_option_its_text_calls_fatal_with_a_safe_one_offered": fatal,
                      "states_offering_a_fatal_option": sum(
                          1 for r in rows if any(t.startswith("you are killed") for t in r["options"].values())),
                      "mean_confidence": round(float(np.mean(confs)), 3),
                      "input_tokens": tokens, "cost_usd": round(tokens * USD_PER_TOKEN, 5),
                      "latency_p50_s": round(float(np.median(lats)), 3), "jev_picks": picked}, indent=1))


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
    ap.add_argument("--record", type=int, default=0, metavar="N", help="play greedy and write N labelled states to data/states.jsonl")
    ap.add_argument("--offline", action="store_true", help="replay data/states.jsonl against Jev and report accuracy")
    ap.add_argument("--p1", default="jev", choices=["jev", "jev-noul", "jev-tactic", "greedy", "rules", "idle"])
    ap.add_argument("--p2", default="jev", choices=["jev", "jev-noul", "jev-tactic", "greedy", "rules", "idle"])
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
    if a.record:
        load_env()
        record(game, a.record)
    if a.offline:
        load_env()
        offline()
    if a.play:
        load_env()
        if any(b.startswith("jev") for b in (a.p1, a.p2)) and not os.environ.get("TYPESAFE_API_KEY"):
            raise SystemExit("set TYPESAFE_API_KEY in .env")
        play(game, (a.p1, a.p2), a.max_frames)

"""Guards for the parts that are easy to get backwards or silently wrong.

A ROM is needed for the emulator tests; they skip without one.
"""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("joust", Path(__file__).resolve().parent.parent / "joust.py")
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)

needs_rom = pytest.mark.skipif(not J.ROM.exists(), reason="no rom/joust.nes")


def test_score_is_bcd_low_byte_first():
    ram = [0] * 0x800
    ram[J.P_SCORE[0]], ram[J.P_SCORE[0] + 1], ram[J.P_SCORE[0] + 2] = 0x00, 0x15, 0x00  # HUD showed 001500
    ram[J.P_SCORE[1]], ram[J.P_SCORE[1] + 1], ram[J.P_SCORE[1] + 2] = 0x50, 0x12, 0x00  # HUD showed 001250
    assert (J.score(ram, 0), J.score(ram, 1)) == (1500, 1250)


def test_describe_says_above_for_a_rider_higher_on_screen():
    """dy is positive when the other rider is higher on screen. Getting this backwards makes every bot
    chase the rider that would kill it, which is how it was found."""
    higher = {"died": False, "points": 0, "height": 40, "closed_on_rider": False, "eggs_left": 0,
              "egg_gain": 0, "egg_dist": None, "near": {"who": "enemy 1", "dy": 30, "dist": 10}}
    lower = dict(higher, near={"who": "enemy 1", "dy": -30, "dist": 10})
    assert "above you" in J.describe(higher) and "kill you" in J.describe(higher)
    assert "below you" in J.describe(lower) and "kill it" in J.describe(lower)
    assert "kill it" not in J.describe(higher) and "kill you" not in J.describe(lower)


def test_greedy_prefers_surviving_then_points_then_being_higher():
    base = {"died": False, "points": 0, "height": 40, "closed_on_rider": False, "eggs_left": 0,
            "egg_gain": 0, "egg_dist": None, "near": {"who": "e", "dy": 0, "dist": 40}}
    outs = {"dies": dict(base, died=True, points=500),
            "scores": dict(base, points=500),
            "under the rider": dict(base, near={"who": "e", "dy": 30, "dist": 40}),
            "over the rider": dict(base, near={"who": "e", "dy": -30, "dist": 40})}
    assert J.greedy_pick(outs) == "scores"
    assert J.greedy_pick({k: outs[k] for k in ("dies", "under the rider", "over the rider")}) == "over the rider"


@needs_rom
def test_rewind_restores_the_exact_state_and_simulation_is_deterministic():
    g = J.Joust()
    for i in range(200):
        g.step(J.press("A") if i % 4 < 2 else 0, 0)
    g.snapshot()
    keep = (int(g.ram[J.P_X]), int(g.ram[J.P_Y]), J.score(g.ram, 0))
    runs = []
    for _ in range(2):
        for f in range(24):
            g.step(J.pad_for("right", True, f), 0)
        runs.append((int(g.ram[J.P_X]), int(g.ram[J.P_Y]), J.score(g.ram, 0)))
        g.rewind()
        assert (int(g.ram[J.P_X]), int(g.ram[J.P_Y]), J.score(g.ram, 0)) == keep
    assert runs[0] == runs[1]


@needs_rom
def test_spawn_does_not_count_as_a_death():
    """The reserve counter decrements when a rider materializes; that is not a death."""
    g = J.Joust()
    outs = J.outcomes_for(g, 0, (None, False))
    assert not any(o["died"] for o in outs.values())
    assert len(outs) == len(J.CANDIDATES)


@needs_rom
def test_the_view_a_player_gets_matches_who_is_higher():
    g = J.Joust()
    for i in range(400):
        g.step(J.press("A") if i % 4 < 2 else 0, 0)
    assert int(g.ram[J.P_Y]) < int(g.ram[J.P_Y + 1]), "p1 should have climbed above p2"
    rival = [o for o in J.situation(g.ram, 0)["riders_near_you"] if o["who"] == "the rival player"][0]
    assert "below you" in rival["summary"]

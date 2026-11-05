"""Head follow and OSC, with synthetic data and loopback sockets only."""
import struct
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from xrlight.head import (CalPoint, Calibrator, FollowController, HeadMotion,  # noqa: E402
                          Interpolator, clamp_to_range, home_position, load_calibration,
                          merge_same_spot, parse_colour, parse_keys, save_calibration)
from xrlight.osc import (HELMET_ADDRESS, HelmetReceiver, OscSender, build_message,  # noqa: E402
                         parse_packet)
from xrlight.rig import MOVE_ATTRS, Rig                    # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "config" / "stage.toml"
FPS = 44.0
DT = 1.0 / FPS

# a 200 x 200 cm square, pan grows with x and tilt with y (a plane: pan = 100 + x/4)
SQUARE = [CalPoint(-100, -100, 75, 40), CalPoint(100, -100, 125, 40),
          CalPoint(100, 100, 125, 90), CalPoint(-100, 100, 75, 90)]


@pytest.fixture
def rig():
    return Rig.load(CONFIG)


# --- OSC -------------------------------------------------------------------------

def test_osc_round_trip():
    packet = build_message(HELMET_ADDRESS, 12.5, -40.25, 172.0)
    assert len(packet) % 4 == 0
    assert packet.startswith(b"/xr/helmet\x00\x00,fff\x00\x00\x00\x00")
    assert parse_packet(packet) == [(HELMET_ADDRESS, [12.5, -40.25, 172.0])]
    assert parse_packet(build_message("/n", 3, 1.5)) == [("/n", [3, 1.5])]


def test_osc_bundle_and_bad_data():
    inner = build_message(HELMET_ADDRESS, 1.0, 2.0, 3.0)
    bundle = b"#bundle\x00" + bytes(8) + struct.pack(">i", len(inner)) + inner
    assert parse_packet(bundle) == [(HELMET_ADDRESS, [1.0, 2.0, 3.0])]
    assert parse_packet(b"garbage") == []
    assert parse_packet(b"/x\x00\x00,s\x00\x00hi\x00\x00") == []     # strings not supported
    assert parse_packet(build_message(HELMET_ADDRESS, 1.0)[:-2]) == []   # truncated


def test_helmet_receiver_over_loopback():
    receiver = HelmetReceiver(0, "127.0.0.1")
    sender = OscSender(("127.0.0.1", receiver.port))
    try:
        sender.send("/other", 9.0, 9.0, 9.0)
        sender.send(HELMET_ADDRESS, 10.0, 20.0, 170.0)
        deadline = time.perf_counter() + 2.0
        while receiver.position is None and time.perf_counter() < deadline:
            receiver.poll(0.05)
        assert receiver.position == (10.0, 20.0, 170.0)
        assert receiver.take_count() == 1 and receiver.age() < 1.0
    finally:
        sender.close()
        receiver.close()


# --- interpolation -----------------------------------------------------------------

def test_interpolation_inside_hull():
    f = Interpolator(SQUARE)
    assert f(-100, -100) == pytest.approx((75, 40))            # taught points exactly
    assert f(100, 100) == pytest.approx((125, 90))
    assert f(0, 0) == pytest.approx((100, 65))                  # centre
    assert f(50, -20) == pytest.approx((112.5, 60))             # linear in between


def test_interpolation_outside_hull_uses_nearest_edge():
    f = Interpolator(SQUARE)
    assert f(400, 0) == pytest.approx(f(100, 0))                # beyond the right edge
    assert f(0, -900) == pytest.approx(f(0, -100))
    assert f(500, 500) == pytest.approx((125, 90))              # beyond a corner


def test_interpolation_needs_a_real_area():
    with pytest.raises(ValueError):
        Interpolator(SQUARE[:2])
    with pytest.raises(ValueError):
        Interpolator([CalPoint(0, 0, 1, 1), CalPoint(1, 1, 2, 2), CalPoint(2, 2, 3, 3)])


def test_clamped_to_taught_range():
    lo, hi = np.array((75, 40)), np.array((125, 90))
    assert tuple(clamp_to_range((300, -20), lo, hi)) == (125, 40)
    f = Interpolator(SQUARE + [CalPoint(0, 300, 100, 60)])     # a fifth point outside the square
    for x in range(-1000, 1001, 125):
        for y in range(-1000, 1001, 125):
            pan, tilt = f(x, y)
            assert 75 <= pan <= 125 and 40 <= tilt <= 90


# --- motion --------------------------------------------------------------------------

def test_rate_limit():
    m = HeadMotion((0, 0), max_step=30 * DT)                    # 30 steps per second
    path = [m.step((200, 100), DT) for _ in range(int(FPS))]    # one second
    assert path[-1] == pytest.approx((30, 30))
    steps = np.abs(np.diff(np.array([(0, 0)] + path), axis=0))
    assert steps.max() <= 30 * DT + 1e-9


def test_smoothing_eases_in_and_hold_stops():
    m = HeadMotion((0, 0), max_step=255, tau_s=0.12)
    first = m.step((100, 0), DT)[0]
    assert 0 < first < 30                                       # eased, not a jump
    for _ in range(100):
        m.step((100, 0), DT)
    assert m.pos[0] == pytest.approx(100, abs=0.01)
    m.step((0, 0), DT)
    held = m.step(None, DT)
    assert m.step(None, DT) == held                             # hold: no drift


# --- follow controller -----------------------------------------------------------------

def test_timeout_hold_then_home(rig):
    c = FollowController(rig, {"Head left": SQUARE})
    home = home_position(rig.fixture("Head left"))
    targets, moves = c.step(None, None, DT)                     # nothing received yet
    assert c.mode == "home" and moves["Head left"] == {"pan": home[0], "tilt": home[1]}
    assert targets["Head left"] == rig.home_states()["Head left"]

    for _ in range(int(10 * FPS)):                              # walk to (0, 0) and settle
        targets, moves = c.step((0.0, 0.0, 170.0), 0.01, DT)
    assert c.mode == "follow"
    assert (moves["Head left"]["pan"], moves["Head left"]["tilt"]) == pytest.approx((100, 65), abs=0.1)
    assert targets["Head left"]["dimmer"] == pytest.approx(rig.follow.follow_level)

    held = dict(moves["Head left"])
    targets, moves = c.step((90.0, 90.0, 170.0), 1.0, DT)       # data 1 s old: hold
    assert c.mode == "hold" and moves["Head left"] == held
    assert targets["Head left"]["dimmer"] > 0

    for _ in range(int(10 * FPS)):                              # data older than 2 s: home
        targets, moves = c.step((90.0, 90.0, 170.0), 3.0, DT)
    assert c.mode == "home"
    assert (moves["Head left"]["pan"], moves["Head left"]["tilt"]) == pytest.approx(home, abs=0.5)
    assert targets["Head left"] == rig.home_states()["Head left"]


def test_others_off_keeps_the_rest_dark(rig):
    c = FollowController(rig, {"Head left": SQUARE}, others_off=True)
    home, zero = rig.home_states(), rig.zero_states()
    assert home["Right back"]["dimmer"] > 0                     # normally on
    for age in (0.01, 1.0, 5.0):                                # follow, hold, home
        targets, _ = c.step((0.0, 0.0, 170.0), age, DT)
        for f in rig.fixtures:
            if f.name != "Head left":
                assert targets[f.name] == zero[f.name], (c.mode, f.name)
    assert c.mode == "home" and targets["Head left"] == home["Head left"]
    targets, _ = c.step((0.0, 0.0, 170.0), 0.01, DT)
    assert targets["Head left"]["dimmer"] == pytest.approx(rig.follow.follow_level)
    plain, _ = FollowController(rig, {"Head left": SQUARE}).step(None, None, DT)
    assert plain["Right back"] == home["Right back"]            # without the flag: home look


def test_follow_never_touches_other_locked_channels(rig):
    c = FollowController(rig, {"Head left": SQUARE, "Head right": SQUARE})
    other_locked = {}                                           # DMX index -> value it must keep
    for f in rig.fixtures:
        for ch in f.profile.channels:
            if ch.locked and ch.attr not in MOVE_ATTRS:
                other_locked[f.index(ch)] = f.lock_value(ch)
    assert other_locked[72] == 18                               # Head left shutter
    for i in range(int(20 * FPS)):
        x, y = 300 * np.cos(i / 40), 300 * np.sin(i / 40)
        targets, moves = c.step((x, y, 170.0), 0.01 if i < 600 else 5.0, DT)
        moves["Head left"].update(shutter=255, macro=255)        # even if asked, ignored
        moves["Head right"].update(reset=170, speed=255)
        frame = rig.render(targets, moves=moves)
        assert all(frame[k] == v for k, v in other_locked.items())


def test_render_moves_only_pan_and_tilt(rig):
    frame = rig.render(rig.zero_states(), moves={"Head right": {"pan": 200.4, "tilt": 9, "reset": 170}})
    i = rig.fixture("Head right").address - 1
    assert frame[i] == 200 and frame[i + 1] == 9
    assert frame[i + 8] == 0                                    # reset stays locked
    assert rig.render(rig.zero_states())[i] == 168              # without moves: fixed position


# --- calibration -------------------------------------------------------------------------

def test_calibration_file_round_trip(tmp_path):
    path = tmp_path / "calibration" / "Head left.toml"
    save_calibration(path, "Head left", SQUARE)
    assert load_calibration(path) == SQUARE


def test_calibrator_keys():
    assert parse_keys("\x1b[A\x1b[1;2C\rus") == ["up", "shift-right", "enter", "u", "s"]
    cal = Calibrator((168, 68))
    for key in ["right", "shift-right", "up", "down", "down"]:
        cal.handle(key, None, None, False)
    assert cal.aim == [174, 67]
    assert "no helmet" in cal.handle("enter", (10, 20, 170), 0.8, False)   # stale data
    assert "still moving" in cal.handle("enter", (10, 20, 170), 0.1, True)
    assert cal.points == []
    cal.handle("enter", (10.04, 20, 170), 0.1, False)
    assert cal.points == [CalPoint(10.0, 20, 174, 67)]
    cal.handle("u", None, None, False)
    assert cal.points == []
    cal.handle("q", None, None, False)
    assert cal.result == "quit"


def test_follow_config(rig):
    fo = rig.follow
    assert (fo.osc_port, fo.follow_level, fo.max_steps_per_second, fo.hold_s) == (9000, 0.5, 30, 0.5)
    assert [f.name for f in rig.heads()] == ["Head right", "Head left"]
    fo.hold_s, fo.follow_level = 3.0, 1.5
    problems = "\n".join(rig.problems())
    assert "hold_s must be at least 0 and below safety.input_timeout_s" in problems
    assert "follow_level must be between 0 and 1" in problems


def test_same_spot_keeps_the_last_aim():
    nudged = SQUARE + [CalPoint(100, 100, 255, 90), CalPoint(105, 98, 130, 88)]   # re-aimed corner
    merged = merge_same_spot(nudged)
    assert len(merged) == 4 and merged[-1] == CalPoint(105, 98, 130, 88)
    f = Interpolator(nudged)
    assert f(105, 98) == pytest.approx((130, 88))
    assert f.hi[0] == 130                                       # the wrong 255 no longer widens the range
    cal = Calibrator((130, 88), list(SQUARE))
    assert cal.handle("enter", (-95, -102, 170), 0.1, False).startswith("replaced")
    assert len(cal.points) == 4 and cal.points[-1] == CalPoint(-95, -102, 130, 88)


def test_follow_colour(rig):
    blue = parse_colour("#1a2cff")
    assert blue[2] == pytest.approx(1.0) and blue[0] < 0.02 and blue[1] < 0.03
    with pytest.raises(ValueError):
        parse_colour("blue")
    c = FollowController(rig, {"Head left": SQUARE}, others_off=True, colour=blue)
    targets, _ = c.step((0.0, 0.0, 170.0), 0.01, DT)
    head = targets["Head left"]
    assert head["blue"] == pytest.approx(1.0) and head["red"] < 0.02 and head["green"] < 0.03
    assert head["dimmer"] == pytest.approx(rig.follow.follow_level)
    assert targets["Head right"] == rig.zero_states()["Head right"]   # not followed: dark


def test_follow_head_option_rejects_unknown_or_uncalibrated(rig, tmp_path):
    from xrlight.head import run_follow
    rig.config_dir = tmp_path                                   # no calibration files here
    logs = []
    assert run_follow(rig, ("127.0.0.1", 1), heads=["Front Left"], log=logs.append) == 2
    assert "not a moving head: Front Left" in logs[-1]
    assert run_follow(rig, ("127.0.0.1", 1), heads=["Head left"], log=logs.append) == 2
    assert "not calibrated: Head left" in logs[-1]

import threading
import time
from pathlib import Path

import pytest

from xrlight.artnet import ArtNetSocket, build_artdmx, parse_artdmx, parse_target
from xrlight.color import (colour_to_state, cct_from_xy, mired_mix, rgb_to_xy,
                           split_white, srgb_to_linear)
from xrlight.patterns import PATTERNS, flash_test
from xrlight.rig import Rig
from xrlight.runner import run_proxy
from xrlight.safety import FlashGuard, Pipeline

CONFIG = Path(__file__).resolve().parent.parent / "config" / "stage.toml"


@pytest.fixture
def rig():
    return Rig.load(CONFIG)


def count_transitions(levels, threshold=0.10):
    """Independent zigzag count: times when brightness moved >= threshold away
    from the last peak or trough (two transitions = one flash)."""
    times = []
    first = levels[0][1]
    lo = hi = extreme = first
    direction = 0
    for t, v in levels[1:]:
        if direction == 0:
            lo, hi = min(lo, v), max(hi, v)
            if v - lo >= threshold:
                direction, extreme = 1, v
                times.append(t)
            elif hi - v >= threshold:
                direction, extreme = -1, v
                times.append(t)
        elif direction > 0:
            if v > extreme:
                extreme = v
            elif extreme - v >= threshold:
                direction, extreme = -1, v
                times.append(t)
        else:
            if v < extreme:
                extreme = v
            elif v - extreme >= threshold:
                direction, extreme = 1, v
                times.append(t)
    return times


def max_in_window(times, window=1.0):
    return max((sum(1 for u in times if t <= u < t + window) for t in times), default=0)


# --- Art-Net ---------------------------------------------------------------

def test_artdmx_roundtrip():
    data = bytes(range(256)) * 2
    packet = build_artdmx(1, data, sequence=7)
    assert packet[:8] == b"Art-Net\x00"
    assert packet[8:10] == b"\x00\x50"          # OpCode 0x5000, little endian
    assert packet[10:12] == b"\x00\x0e"         # protocol version 14
    assert parse_artdmx(packet) == (1, data)


def test_artdmx_high_universe_and_odd_length():
    universe, data = parse_artdmx(build_artdmx(0x1234, b"\x01\x02\x03"))
    assert universe == 0x1234
    assert data == b"\x01\x02\x03\x00"           # padded to even length


def test_parse_rejects_other_packets():
    assert parse_artdmx(b"hello") is None
    poll = b"Art-Net\x00" + b"\x00\x20" + bytes(10)   # ArtPoll
    assert parse_artdmx(poll) is None


def test_parse_target():
    assert parse_target("10.55.4.7") == ("10.55.4.7", 6454)
    assert parse_target("127.0.0.1:6455") == ("127.0.0.1", 6455)


# --- Rig ---------------------------------------------------------------------

def test_patch_matches_qlc_project(rig):
    spans = {f.name: (f.address, f.last_address) for f in rig.fixtures}
    assert spans == {"Right back": (1, 7), "Right front": (8, 14), "Front Right": (15, 22),
                     "Front Middle": (23, 30), "Front Left": (31, 38), "Left front": (39, 45),
                     "Left back": (46, 52), "Orbiter": (53, 65), "Head left": (66, 74),
                     "Head right": (77, 85)}
    assert rig.problems() == []


def test_right_head_cannot_move_or_strobe(rig):
    states = rig.zero_states()
    states["Head right"].update(dimmer=1.0, red=1.0, pan=1.0, tilt=1.0, speed=1.0, reset=1.0)
    frame = rig.render(states)
    i = rig.fixture("Head right").address - 1
    assert frame[i] == 0 and frame[i + 1] == 0          # pan, tilt locked
    assert frame[i + 2] == 134                           # full dimmer stays below strobe range
    assert frame[i + 7] == 0 and frame[i + 8] == 0      # speed, reset locked


def test_left_head_layout(rig):
    f = rig.fixture("Head left")
    states = rig.zero_states()
    states["Head left"].update(pan=1.0, tilt=1.0, unknown=1.0, macro=1.0, shutter=0.0)
    frame = rig.render(states)
    assert frame[65] == 0 and frame[66] == 0             # DMX 66/67 pan, tilt locked
    assert frame[71] == 0                                # DMX 72 macro always 0
    assert frame[72] == 18                               # DMX 73 shutter: steady, never strobe
    assert rig.render(rig.zero_states())[71:73] == bytes([0, 18])

    white = colour_to_state(f.profile, (1.0, 1.0, 1.0), 0.8)
    assert white["red"] == white["green"] == white["blue"] == pytest.approx(1.0)
    states = rig.zero_states()
    states["Head left"] = white
    frame = rig.render(states)
    assert frame[68:71] == bytes([255, 255, 255])        # DMX 69-71 red, green, blue
    assert frame[72] == 18                               # DMX 73 shutter still locked
    assert abs(frame[73] - 204) <= 1                     # DMX 74 dimmer at 0.8


def test_white_par_strobe_locked(rig):
    states = rig.zero_states()
    states["Right back"].update(white_1=1.0, dimmer=1.0, strobe=1.0)
    frame = rig.render(states)
    assert frame[4] == 0 and frame[0] == 255 and frame[5] == 255


def test_locked_channels_ignore_input(rig):
    states = rig.zero_states()
    # even if something tries to drive the strobe/mode channels, they stay locked
    for s in states.values():
        s.update({"strobe": 1.0, "mode": 1.0, "unknown": 1.0})
    frame = rig.render(states)
    for f in rig.fixtures:
        for c in f.profile.channels:
            if c.locked:
                assert frame[f.index(c)] == c.lock


def test_render_decode_roundtrip(rig):
    states = rig.zero_states()
    states["Front Left"].update(red=1.0, blue=0.5, dimmer=0.8)
    frame = rig.render(states)
    assert frame[30] == 255 and frame[32] == 128 and frame[36] == 204
    back = rig.decode(frame)["Front Left"]
    assert back["red"] == 1.0 and abs(back["blue"] - 0.5) < 0.01


def test_render_passthrough_keeps_unpatched_channels(rig):
    base = bytearray(512)
    base[99] = 77          # channel 100, not in the patch
    base[19] = 255         # Front Right strobe: patched and locked
    frame = rig.render(rig.zero_states(), base)
    assert frame[99] == 77
    assert frame[19] == 0


# --- Colour ------------------------------------------------------------------

def test_white_is_d65():
    x, y = rgb_to_xy((1.0, 1.0, 1.0))
    assert abs(x - 0.3127) < 0.001 and abs(y - 0.3290) < 0.001
    assert 6300 < cct_from_xy(x, y) < 6700


def test_srgb_mid_grey():
    assert abs(srgb_to_linear((0.5, 0.5, 0.5))[0] - 0.214) < 0.001


def test_split_white():
    assert split_white(1.0, 0.6, 0.6) == pytest.approx((0.4, 0.0, 0.0, 0.6))


def test_mired_mix_endpoints():
    assert mired_mix(6000, 6000, 3000) == pytest.approx((1.0, 0.0))
    assert mired_mix(3000, 6000, 3000) == pytest.approx((0.0, 1.0))
    cool, warm = mired_mix(4000, 6000, 3000)
    assert 0 < cool < 1 and abs(cool + warm - 1) < 1e-9


def test_colour_to_state_rgbw(rig):
    p = rig.fixture("Front Left").profile
    s = colour_to_state(p, srgb_to_linear((1.0, 0.45, 0.15)), 0.6)
    assert s["red"] == pytest.approx(1.0) and s["dimmer"] == pytest.approx(0.6)
    assert s["white"] > 0 and s["blue"] < s["green"] < s["red"]


def test_colour_to_state_white_par_follows_cct(rig):
    p = rig.fixture("Right back").profile
    daylight = colour_to_state(p, srgb_to_linear((0.95, 0.97, 1.0)), 0.5)
    sunset = colour_to_state(p, srgb_to_linear((1.0, 0.45, 0.15)), 0.5)
    assert daylight["white_1"] > daylight["white_2"]     # cool dominates
    assert sunset["white_2"] > sunset["white_1"]         # warm dominates
    assert sunset["amber"] > 0


def test_level_option(rig):
    from functools import partial
    from xrlight.cli import main
    from xrlight.patterns import identify
    targets, _ = partial(identify, level=0.8)(rig, 0.0)
    assert targets["Right back"]["dimmer"] == pytest.approx(0.8)
    with pytest.raises(SystemExit):                      # rejected before any socket opens
        main(["test", "--level", "1.5"])


def test_all_patterns_produce_valid_frames(rig):
    for name, pattern in PATTERNS.items():
        targets, label = pattern(rig, 3.3)
        frame = rig.render(targets)
        assert len(frame) == 512 and label


# --- Safety ------------------------------------------------------------------

def test_flash_guard_limits_10hz_strobe():
    guard = FlashGuard(max_flashes_per_second=3, threshold=0.10)
    fps, levels = 44.0, []
    for i in range(int(5 * fps)):
        t = i / fps
        requested = 1.0 if int(t * 20) % 2 == 0 else 0.0     # 10 Hz
        levels.append((t, guard.process(requested, t)))
    assert max_in_window(count_transitions(levels)) <= 6     # 3 flashes = 6 transitions


def test_flash_guard_lets_slow_changes_through():
    guard = FlashGuard()
    out = [guard.process(v, i / 44) for i, v in enumerate([0, 0.2, 0.4, 0.6, 0.8, 1.0])]
    assert out == [0, 0.2, 0.4, 0.6, 0.8, 1.0]


def test_pipeline_limits_flash_pattern(rig):
    pipeline = Pipeline(rig)
    fps = rig.network.fps
    per_fixture = {f.name: [] for f in rig.fixtures}
    for i in range(int(4 * fps)):
        t = i / fps
        targets, _ = flash_test(rig, t, hz=10)
        states = pipeline.step(targets, t)
        for f in rig.fixtures:
            per_fixture[f.name].append((t, f.profile.brightness(states[f.name])))
    for name, levels in per_fixture.items():
        assert max_in_window(count_transitions(levels)) <= 6, name


def test_pipeline_master_scales_output(rig):
    rig.safety.master = 0.5
    rig.safety.smoothing_ms = 0
    pipeline = Pipeline(rig)
    targets = rig.zero_states()
    targets["Front Left"].update(red=1.0, dimmer=1.0)
    out = pipeline.step(targets, 0.0)
    assert out["Front Left"]["dimmer"] == pytest.approx(0.5, abs=1 / 255)


# --- Proxy end to end (loopback) ---------------------------------------------

def test_proxy_forwards_and_limits_flashes(rig):
    listen_port, out_port = 16456, 16457
    receiver = ArtNetSocket("127.0.0.1", out_port)
    proxy = threading.Thread(target=run_proxy, daemon=True,
                             args=(rig, ("127.0.0.1", out_port), ("127.0.0.1", listen_port)),
                             kwargs={"seconds": 3.5, "log": lambda m: None})
    proxy.start()
    time.sleep(0.2)

    source = ArtNetSocket("127.0.0.1", 0)
    received = []
    start = time.perf_counter()
    while time.perf_counter() - start < 3.0:
        t = time.perf_counter() - start
        frame = bytearray(512)
        on = int(t * 20) % 2 == 0                            # raw 10 Hz strobe
        frame[30:38] = bytes([255, 255, 255, 255, 0, 255, 255 if on else 0, 0])  # Front Left
        frame[99] = 42                                       # unpatched channel
        source.send(("127.0.0.1", listen_port), 1, frame)
        for universe, data, _ in receiver.receive(1 / 44):
            received.append((time.perf_counter() - start, universe, data))
    source.close()
    proxy.join(timeout=5)
    receiver.close()

    assert len(received) > 50
    assert all(u == 1 for _, u, _ in received)
    late = [d for t, _, d in received if t > 0.5]
    assert all(d[35] == 0 for d in late)                     # strobe channel locked
    assert any(d[99] == 42 for d in late)                    # passthrough of unpatched
    p = rig.fixture("Front Left").profile
    levels = [(t, p.brightness(rig.decode(d)["Front Left"])) for t, _, d in received]
    # receive timestamps jitter by a few ms, so measure in a slightly shorter window
    assert max_in_window(count_transitions(levels), window=0.95) <= 6


def test_flash_guard_tiny_dips_do_not_reset_the_count():
    # rise in small steps with 1% dips in between: one long swing, not many
    guard = FlashGuard(max_flashes_per_second=3, threshold=0.10)
    signal, v = [], 0.0
    for i in range(40):
        v = min(1.0, v + 0.06) if i % 2 == 0 else v - 0.01
        signal.append(v)
    out = [(i / 44, guard.process(s, i / 44)) for i, s in enumerate(signal)]
    assert out[-1][1] == pytest.approx(signal[-1])        # nothing was held back
    assert len(count_transitions(out)) == 1


def test_pipeline_guard_with_jittered_timing(rig):
    import random
    for seed in range(20):
        random.seed(seed)
        pipeline = Pipeline(rig)
        p = rig.fixture("Front Left").profile
        offset, levels = random.random() * 0.05, []
        for i in range(int(4 * 44)):
            t = i / 44 + random.uniform(0, 0.004)
            on = int((t - offset) * 20) % 2 == 0
            targets = rig.zero_states()
            targets["Front Left"].update(red=1, green=1, blue=1, white=1, dimmer=1.0 if on else 0.0)
            frame = rig.render(pipeline.step(targets, t))
            levels.append((t, p.brightness(rig.decode(frame)["Front Left"])))
        assert max_in_window(count_transitions(levels)) <= 6, seed

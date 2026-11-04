"""Screen mode, with synthetic numpy frames only (no capture, no network)."""
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from xrlight.rig import Rig, Screen, Zone                          # noqa: E402
from xrlight.screen import (DelayBuffer, downsample, frame_colours, level_of,  # noqa: E402
                            targets_from_colours, zone_colours)

CONFIG = Path(__file__).resolve().parent.parent / "config" / "stage.toml"


@pytest.fixture
def rig():
    return Rig.load(CONFIG)


def bgra_frame(left_rgb, right_rgb, w=1920, h=1080):
    """A BGRA frame like mss returns: left half one sRGB colour, right half another."""
    frame = np.zeros((h, w, 4), np.uint8)
    frame[:, : w // 2, :3] = left_rgb[::-1]
    frame[:, w // 2:, :3] = right_rgb[::-1]
    frame[..., 3] = 255
    return frame


def test_downsample_to_about_96_wide_and_linear():
    small = downsample(bgra_frame((255, 0, 0), (0, 0, 255)))
    assert small.shape == (54, 96, 3)
    assert small[0, 0] == pytest.approx((1.0, 0.0, 0.0))    # BGRA -> RGB
    grey = downsample(np.full((10, 10, 3), 188, np.uint8), bgra=False)
    assert grey[0, 0, 0] == pytest.approx(0.5, abs=0.01)    # sRGB 188 is ~50% linear


def test_zone_average_is_linear():
    frame = np.zeros((20, 40, 3))
    frame[:, :20] = 1.0                                      # left half white, right black
    zones = [Zone("all", (0, 0, 1, 1)), Zone("left", (0, 0, 0.5, 1)),
             Zone("right_top", (0.5, 0, 1, 0.5))]
    c = zone_colours(frame, zones)
    assert c["all"] == pytest.approx((0.5, 0.5, 0.5))
    assert c["left"] == pytest.approx((1.0, 1.0, 1.0))
    assert c["right_top"] == pytest.approx((0.0, 0.0, 0.0))


def test_red_left_blue_right(rig):
    colours = frame_colours(bgra_frame((255, 0, 0), (0, 0, 255)), rig.zones)
    targets = targets_from_colours(rig, colours, gain=1.0)
    left, right = targets["Front Left"], targets["Front Right"]
    assert left["red"] == pytest.approx(1.0) and left["blue"] == pytest.approx(0.0)
    assert right["blue"] == pytest.approx(1.0) and right["red"] == pytest.approx(0.0)
    assert left["dimmer"] > 0 and right["dimmer"] > 0
    middle = targets["Front Middle"]                         # straddles the split: both
    assert middle["red"] > 0 and middle["blue"] > 0


def test_white_pars_follow_colour_temperature(rig):
    warm, cool = (255, 140, 60), (200, 215, 255)
    targets = targets_from_colours(rig, frame_colours(bgra_frame(warm, cool), rig.zones), 1.0)
    for name in ("Left front", "Left back"):                 # left of the picture = warm
        assert targets[name]["white_2"] > targets[name]["white_1"]
    for name in ("Right front", "Right back"):               # right of the picture = cool
        assert targets[name]["white_1"] > targets[name]["white_2"]


def test_unzoned_fixtures_show_home_and_gain_scales(rig):
    targets = targets_from_colours(rig, {"Front Left": (0.2, 0.2, 0.2)}, gain=1.0)
    home = rig.home_states()
    assert targets["Head left"] == home["Head left"]
    assert targets["Right back"] == home["Right back"]       # zoned, but no colour yet
    assert targets_from_colours(rig, None, 1.0) == home
    grey = (0.2, 0.2, 0.2)
    assert level_of(grey, 1.0) == pytest.approx(0.4845, abs=1e-3)   # sRGB of 20% linear
    assert level_of(grey, 2.0) == pytest.approx(2 * level_of(grey, 1.0))
    assert level_of((1.0, 1.0, 1.0), 2.0) == 1.0
    assert level_of(grey, 0.0) == 0.0


def test_zone_gain_halves_the_level(rig):
    grey = (0.2, 0.2, 0.2)
    rig.zones = [Zone("Front Left", (0, 0, 1, 1)), Zone("Front Right", (0, 0, 1, 1), gain=0.5)]
    targets = targets_from_colours(rig, {"Front Left": grey, "Front Right": grey}, gain=1.0)
    full = level_of(grey, 1.0, rig.screen)
    assert targets["Front Left"]["dimmer"] == pytest.approx(full)
    assert targets["Front Right"]["dimmer"] == pytest.approx(0.5 * full)
    assert {z.fixture: z.gain for z in Rig.load(CONFIG).zones}["Right back"] == 0.5


def test_brightness_curve():
    s = Screen()                                            # defaults: gamma 2, floor 0.08
    assert (s.gamma, s.black_floor) == (2.0, 0.08)
    assert s.curve(0.0) == 0.0
    assert s.curve(0.05) == 0.0 and s.curve(0.08) == 0.0    # at or below the floor: off
    assert s.curve(1.0) == pytest.approx(1.0)
    assert level_of((0.0, 0.0, 0.0), 1.0, s) == 0.0
    assert level_of((1.0, 1.0, 1.0), 1.0, s) == pytest.approx(1.0)
    dark = (0.005, 0.005, 0.005)                            # sRGB ~0.06, below the floor
    assert level_of(dark, 1.0) > 0.05 and level_of(dark, 2.0, s) == 0.0
    mid = 0.5
    assert Screen(1.0, 0.0).curve(mid) == pytest.approx(mid)
    assert Screen(1.0).curve(mid) > Screen(2.0).curve(mid) > Screen(3.0).curve(mid) > 0
    assert Screen(2.0, 0.0).curve(mid) == pytest.approx(0.25)


def test_dark_zone_switches_light_off(rig):
    targets = targets_from_colours(rig, {"Left front": (0.005, 0.005, 0.005)}, gain=1.0)
    assert rig.fixture("Left front").profile.brightness(targets["Left front"]) == 0.0


def test_screen_config(rig):
    assert (rig.screen.gamma, rig.screen.black_floor) == (2.0, 0.08)
    assert rig.problems() == []
    rig.screen = Screen(gamma=4.5, black_floor=0.6)
    problems = "\n".join(rig.problems())
    assert "screen: gamma must be between 0.5 and 4" in problems
    assert "screen: black_floor must be between 0 and 0.5" in problems
    rig.screen = Screen(gamma=0.4, black_floor=-0.1)
    assert len(rig.problems()) == 2


def test_delay_buffer_returns_value_from_d_ms_ago():
    buf = DelayBuffer(0.100)
    assert buf.get(0.0) is None
    for i in range(50):                                     # a value every 10 ms
        buf.push(i * 0.010, i)
    assert buf.get(0.050) is None                           # nothing 100 ms old yet
    assert buf.get(0.305) == 20                             # pushed at 0.200 s
    assert buf.get(0.355) == 25                             # newest at or before 0.255 s
    assert len(buf.items) < 30                              # old values are dropped
    assert DelayBuffer(0.0).get(1.0) is None
    zero = DelayBuffer(0.0)
    zero.push(1.0, "now")
    assert zero.get(1.0) == "now"


def test_default_zones(rig):
    rects = {z.fixture: z.rect for z in rig.zones}
    assert rects["Head left"] == (0.0, 0.0, 0.5, 0.5)
    assert rects["Head right"] == (0.5, 0.0, 1.0, 0.5)
    assert rects["Front Left"][0] == 0.0 and rects["Front Right"][2] == 1.0
    assert rects["Right back"] == rects["Right front"] == (0.5, 0.0, 1.0, 1.0)
    assert rects["Orbiter"] == (0.33, 0.2, 0.67, 0.8)


def test_zone_validation(rig):
    rig.zones = [Zone("Nobody", (0, 0, 1, 1)),
                 Zone("Front Left", (0, 0, 1.5, 1)),
                 Zone("Front Right", (0.5, 0, 0.5, 1)),
                 Zone("Orbiter", (0, 0, 1)),
                 Zone("Orbiter", (0, 0, 1, 1)),
                 Zone("Right back", (0, 0, 1, 1), gain=2.5)]
    problems = "\n".join(rig.problems())
    assert "unknown fixture 'Nobody'" in problems
    assert "Front Left: rect values must be between 0 and 1" in problems
    assert "Front Right: rect is empty" in problems
    assert "Orbiter: rect needs 4 numbers" in problems
    assert "Orbiter: fixture has more than one zone" in problems
    assert "Right back: gain must be between 0 and 2" in problems


def test_screen_options_rejected_before_capture():
    from xrlight.cli import main
    with pytest.raises(SystemExit):
        main(["screen", "--gain", "3"])
    with pytest.raises(SystemExit):
        main(["screen", "--delay-ms", "-5"])

"""Head follow: the moving heads aim at a tracked helmet.

There is no geometry model. A head is calibrated by teaching: the helmet stands at
a spot, the head is aimed at it by hand, and the pair (helmet x, y) -> (pan, tilt)
is recorded. At runtime pan/tilt are interpolated from the helmet's floor position,
piecewise-linear over a Delaunay triangulation of the taught points; height is
ignored. Outside the taught area the nearest point on its edge is used, and the
result is clamped to the range of taught pan/tilt values, so a head never points
anywhere it was not aimed by hand.

pan and tilt stay locked channels. They only move through Rig.render(moves=...),
which can override nothing but pan and tilt. Movement is smoothed and limited to
`follow.max_steps_per_second` DMX steps per second (`calibrate`: 5 steps per frame).

numpy is needed here and scipy for the triangulation; the CLI imports this module
only for `calibrate` and `follow`.
"""
from __future__ import annotations

import math
import os
import re
import select
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .artnet import PORT, ArtNetSocket
from .color import Rgb, colour_to_state, srgb_to_linear
from .osc import HelmetReceiver
from .rig import Fixture, Rig, State, tomllib
from .runner import Log, _ticks
from .safety import Pipeline

WHITE = (1.0, 1.0, 1.0)
CALIBRATE_LEVEL = 0.4
CALIBRATE_STEPS_PER_FRAME = 5
RECORD_MAX_AGE_S = 0.5        # calibrate: helmet data must be this fresh to record a point
SAME_SPOT_CM = 20.0           # a point this close to an earlier one replaces it
RETURN_TIMEOUT_S = 15.0       # on exit, give up waiting for the heads to reach home after this

Pos = tuple[float, float]     # (pan, tilt) in DMX values


# --- calibration files ------------------------------------------------------------

@dataclass(frozen=True)
class CalPoint:
    x: float                  # helmet floor position, cm, stage-local
    y: float
    pan: int                  # DMX 0..255
    tilt: int


def calibration_path(rig: Rig, head: str) -> Path:
    base = rig.config_dir if rig.config_dir is not None else Path("config")
    return base / "calibration" / f"{head}.toml"


def load_calibration(path: Path) -> list[CalPoint]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    points = []
    for raw in data.get("points", []):
        p = CalPoint(float(raw["x"]), float(raw["y"]), int(raw["pan"]), int(raw["tilt"]))
        if not (0 <= p.pan <= 255 and 0 <= p.tilt <= 255):
            raise ValueError(f"{path.name}: pan/tilt must be 0..255")
        points.append(p)
    return points


def save_calibration(path: Path, head: str, points: list[CalPoint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Calibration for {head}: helmet floor position (cm, stage-local) -> pan/tilt (DMX).",
             f"# Written by `xrlight calibrate --head \"{head}\"`, {time.strftime('%Y-%m-%d %H:%M')}.",
             "# Height (z) is not used.",
             "",
             f'head = "{head}"',
             "points = ["]
    lines += [f"  {{ x = {p.x:.1f}, y = {p.y:.1f}, pan = {p.pan}, tilt = {p.tilt} }},"
              for p in points]
    lines.append("]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_same_spot(points: list[CalPoint]) -> list[CalPoint]:
    """Keep one point per spot: a later point within SAME_SPOT_CM replaces an earlier one.

    Pressing Enter again without walking means the aim was corrected, so the last one wins.
    """
    out: list[CalPoint] = []
    for p in points:
        out = [q for q in out if math.hypot(q.x - p.x, q.y - p.y) >= SAME_SPOT_CM]
        out.append(p)
    return out


def parse_colour(text: str) -> Rgb:
    """'1a2cff' or '#1a2cff' (sRGB hex) -> linear RGB."""
    h = text.strip().removeprefix("#")
    if len(h) != 6:
        raise ValueError(f"colour must be 6 hex digits like 1a2cff, got {text!r}")
    rgb = tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    if max(rgb) == 0:
        raise ValueError("colour cannot be black")
    return srgb_to_linear(rgb)


# --- interpolation ----------------------------------------------------------------

class Interpolator:
    """Piecewise-linear pan/tilt from helmet (x, y) over the taught points."""

    def __init__(self, points: list[CalPoint]):
        from scipy.spatial import Delaunay, QhullError
        points = merge_same_spot(points)
        if len(points) < 3:
            raise ValueError("need at least 3 calibration points")
        self.xy = np.array([(p.x, p.y) for p in points], float)
        self.values = np.array([(p.pan, p.tilt) for p in points], float)
        try:
            self.tri = Delaunay(self.xy)
        except QhullError as exc:
            raise ValueError("calibration points lie on one line; spread them over the floor") from exc
        self.edges = self.tri.convex_hull            # hull edges as pairs of point indices
        self.lo = self.values.min(axis=0)
        self.hi = self.values.max(axis=0)

    def __call__(self, x: float, y: float) -> Pos:
        p = np.array((x, y), float)
        s = int(self.tri.find_simplex(p))
        if s >= 0:
            t = self.tri.transform[s]
            b = t[:2] @ (p - t[2])
            weights = np.append(b, 1.0 - b.sum())
            v = weights @ self.values[self.tri.simplices[s]]
        else:
            v = self._nearest_on_hull(p)
        v = clamp_to_range(v, self.lo, self.hi)
        return float(v[0]), float(v[1])

    def _nearest_on_hull(self, p: np.ndarray) -> np.ndarray:
        best, best_d = None, math.inf
        for i, j in self.edges:
            a, d = self.xy[i], self.xy[j] - self.xy[i]
            t = float(np.clip(np.dot(p - a, d) / np.dot(d, d), 0.0, 1.0))
            dist = float(np.sum((a + t * d - p) ** 2))
            if dist < best_d:
                best_d, best = dist, self.values[i] + t * (self.values[j] - self.values[i])
        return best


def clamp_to_range(v, lo, hi) -> np.ndarray:
    return np.minimum(np.maximum(np.asarray(v, float), lo), hi)


# --- motion -------------------------------------------------------------------------

class HeadMotion:
    """Pan/tilt position that follows a target smoothly and at a limited speed.

    The target goes through an exponential filter (time constant tau_s), then the
    position moves towards it by at most `max_step` DMX steps per call. target=None
    holds the current position.
    """

    def __init__(self, start: Pos, max_step: float, tau_s: float = 0.0):
        self.pos = np.array(start, float)
        self.smooth = self.pos.copy()
        self.max_step = max_step
        self.tau = tau_s

    def step(self, target: Pos | None, dt: float) -> Pos:
        if target is None:
            self.smooth = self.pos.copy()
        else:
            alpha = 1.0 if self.tau <= 0 else 1.0 - math.exp(-dt / self.tau)
            self.smooth += alpha * (np.asarray(target, float) - self.smooth)
        self.pos += np.clip(self.smooth - self.pos, -self.max_step, self.max_step)
        return float(self.pos[0]), float(self.pos[1])

    def at(self, target: Pos, tolerance: float = 0.5) -> bool:
        return bool(np.all(np.abs(self.pos - np.asarray(target, float)) <= tolerance))


def home_position(f: Fixture) -> Pos:
    """Where the head sits when nothing moves it: its locked (or `fixed`) pan/tilt."""
    by_attr = {c.attr: c for c in f.profile.channels if c.locked}
    return float(f.lock_value(by_attr["pan"])), float(f.lock_value(by_attr["tilt"]))


def moves_of(heads: dict[str, HeadMotion]) -> dict[str, dict[str, float]]:
    return {name: {"pan": m.pos[0], "tilt": m.pos[1]} for name, m in heads.items()}


def return_home(rig: Rig, sock: ArtNetSocket, target: tuple[str, int], pipeline: Pipeline,
                motions: dict[str, HeadMotion], log: Log) -> None:
    """Fade the light to the home look while the heads move back to their home position."""
    homes = {name: home_position(rig.fixture(name)) for name in motions}
    dt = 1.0 / rig.network.fps
    for m in motions.values():                   # always at follow speed, never faster
        m.max_step = min(m.max_step, rig.follow.max_steps_per_second * dt)
    end = time.perf_counter() + RETURN_TIMEOUT_S
    fade_end = time.perf_counter() + 1.5
    for now, next_tick in _ticks(rig.network.fps):
        if all(m.at(homes[n]) for n, m in motions.items()) and now >= fade_end:
            return
        if now >= end:
            log("heads did not reach their home position in time")
            return
        for name, m in motions.items():
            m.step(homes[name], dt)
        frame = rig.render(pipeline.step(rig.home_states(), now), moves=moves_of(motions))
        sock.send(target, rig.network.universe, frame)
        time.sleep(max(0.0, next_tick + dt - time.perf_counter()))


# --- follow -------------------------------------------------------------------------

@dataclass
class FollowHead:
    fixture: Fixture
    interp: Interpolator
    motion: HeadMotion
    home: Pos


class FollowController:
    """Turns the latest helmet position into fixture targets and pan/tilt moves.

    helmet data fresher than hold_s   -> follow: aim at the helmet, light at follow_level
    older than hold_s                 -> hold: keep position, light stays on
    older than safety.input_timeout_s -> home: back to home position, home look
    (no data yet counts as timed out)
    With others_off, every fixture except the followed heads gets a zero state
    instead of its home look. `colour` is the heads' light colour (linear RGB).
    """

    def __init__(self, rig: Rig, calibrations: dict[str, list[CalPoint]],
                 others_off: bool = False, colour: Rgb = WHITE):
        self.rig = rig
        self.others_off = others_off
        self.colour = colour
        fo = rig.follow
        dt = 1.0 / rig.network.fps
        self.heads: dict[str, FollowHead] = {}
        for name, points in calibrations.items():
            f = rig.fixture(name)
            home = home_position(f)
            self.heads[name] = FollowHead(
                f, Interpolator(points),
                HeadMotion(home, fo.max_steps_per_second * dt, rig.safety.smoothing_ms / 1000.0),
                home)
        self.mode = "home"

    def step(self, helmet: tuple[float, float, float] | None, age: float | None,
             dt: float) -> tuple[dict[str, State], dict[str, dict[str, float]]]:
        fo, timeout = self.rig.follow, self.rig.safety.input_timeout_s
        if helmet is None or age is None or age > timeout:
            self.mode = "home"
        elif age > fo.hold_s:
            self.mode = "hold"
        else:
            self.mode = "follow"
        targets = self.rig.home_states()
        if self.others_off:
            for f in self.rig.fixtures:
                if f.name not in self.heads:
                    targets[f.name] = f.profile.zero_state()
        for name, h in self.heads.items():
            if self.mode == "follow":
                h.motion.step(h.interp(helmet[0], helmet[1]), dt)
            elif self.mode == "hold":
                h.motion.step(None, dt)
            else:
                h.motion.step(h.home, dt)
            if self.mode != "home":
                targets[name] = colour_to_state(h.fixture.profile, self.colour, fo.follow_level)
        return targets, moves_of({n: h.motion for n, h in self.heads.items()})


def load_calibrations(rig: Rig, log: Log = print) -> dict[str, list[CalPoint]]:
    found = {}
    for f in rig.heads():
        path = calibration_path(rig, f.name)
        if not path.exists():
            log(f"{f.name}: no calibration ({path}), stays at its home look")
            continue
        points = load_calibration(path)
        try:
            Interpolator(points)
        except ValueError as exc:
            log(f"{f.name}: calibration not usable ({exc}), stays at its home look")
            continue
        found[f.name] = points
    return found


def run_follow(rig: Rig, target: tuple[str, int], seconds: float | None = None,
               others_off: bool = False, heads: list[str] | None = None,
               colour: Rgb = WHITE, log: Log = print) -> int:
    """heads: only these follow (default: every calibrated head)."""
    if heads:
        known = [f.name for f in rig.heads()]
        unknown = [h for h in heads if h not in known]
        if unknown:
            log(f"not a moving head: {', '.join(unknown)} (heads: {', '.join(known)})")
            return 2
    calibrations = load_calibrations(rig, log)
    if heads:
        missing = [h for h in heads if h not in calibrations]
        if missing:
            log(f"not calibrated: {', '.join(missing)}")
            return 2
        calibrations = {h: calibrations[h] for h in heads}
    if not calibrations:
        log("no calibrated heads — run `xrlight calibrate --head NAME` first")
        return 2
    net, fo = rig.network, rig.follow
    receiver = HelmetReceiver(fo.osc_port)
    controller = FollowController(rig, calibrations, others_off, colour)
    pipeline = Pipeline(rig)
    sock = ArtNetSocket(bind_port=PORT, fallback_to_any_port=True)
    dt = 1.0 / net.fps
    log(f"follow: OSC {receiver.port} {', '.join(calibrations)} -> {target[0]}:{target[1]} "
        f"universe {net.universe}, light {fo.follow_level:g}, "
        f"max {fo.max_steps_per_second:g} steps/s"
        + (", other fixtures off" if others_off else ""))
    start = time.perf_counter()
    report_at = start + 1.0
    try:
        for now, next_tick in _ticks(net.fps):
            if seconds is not None and now - start >= seconds:
                break
            receiver.poll(0.0)
            targets, moves = controller.step(receiver.position, receiver.age(now), dt)
            sock.send(target, net.universe, rig.render(pipeline.step(targets, now), moves=moves))
            if now >= report_at:
                pos = receiver.position
                helmet = "no helmet" if pos is None else f"helmet {pos[0]:7.1f} {pos[1]:7.1f} {pos[2]:6.1f} cm"
                heads = " | ".join(f"{n} pan {m['pan']:5.1f} tilt {m['tilt']:5.1f}"
                                   for n, m in moves.items())
                log(f"{helmet} | {heads} | OSC {receiver.take_count():3d}/s | {controller.mode}")
                report_at = now + 1.0
            receiver.poll(max(0.0, next_tick + dt - time.perf_counter()))
    except KeyboardInterrupt:
        pass
    finally:
        log("heads back to home…")
        return_home(rig, sock, target, pipeline,
                    {n: h.motion for n, h in controller.heads.items()}, log)
        sock.close()
        receiver.close()
    return 0


# --- calibrate ------------------------------------------------------------------------

KEY_PATTERN = re.compile(r"\x1b\[1;2([ABCD])|\x1b\[([ABCD])|(\r|\n)|(.)", re.S)
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


def parse_keys(text: str) -> list[str]:
    """Terminal input -> key names: up/down/left/right, shift-up..., enter, or the character."""
    keys = []
    for shift, arrow, enter, char in KEY_PATTERN.findall(text):
        if shift:
            keys.append("shift-" + ARROWS[shift])
        elif arrow:
            keys.append(ARROWS[arrow])
        elif enter:
            keys.append("enter")
        elif char != "\x1b":
            keys.append(char)
    return keys


class Calibrator:
    """Key handling for `calibrate`, separate from the terminal so it can be tested.

    left/right = pan -/+, up/down = tilt +/-, 1 DMX step (Shift: 5).
    """

    NUDGE = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}

    def __init__(self, start: Pos, points: list[CalPoint] | None = None):
        self.aim = [int(round(start[0])), int(round(start[1]))]
        self.points = list(points or [])
        self.result: str | None = None      # "save" or "quit" once finished

    def handle(self, key: str, helmet: tuple[float, float, float] | None, age: float | None,
               moving: bool) -> str:
        """Apply one key; returns a message for the status line ("" if none)."""
        base = key.removeprefix("shift-")
        if base in self.NUDGE:
            step = 5 if key.startswith("shift-") else 1
            dp, dtilt = self.NUDGE[base]
            self.aim[0] = min(255, max(0, self.aim[0] + dp * step))
            self.aim[1] = min(255, max(0, self.aim[1] + dtilt * step))
            return ""
        if key == "enter":
            if helmet is None or age is None or age > RECORD_MAX_AGE_S:
                return "not recorded: no helmet data in the last 0.5 s"
            if moving:
                return "not recorded: head still moving, press Enter again"
            before = len(self.points)
            self.points = merge_same_spot(
                self.points + [CalPoint(round(helmet[0], 1), round(helmet[1], 1), *self.aim)])
            what = "replaced point at this spot" if len(self.points) == before else f"point {len(self.points)}"
            return f"{what}: x {helmet[0]:.1f} y {helmet[1]:.1f} -> pan {self.aim[0]} tilt {self.aim[1]}"
        if key == "u":
            if not self.points:
                return "nothing to undo"
            p = self.points.pop()
            return f"removed point at x {p.x:.1f} y {p.y:.1f}"
        if key == "s":
            self.result = "save"
        elif key == "q" or key == "\x03":
            self.result = "quit"
        return ""


class _RawTerminal:
    """Single-key input without echo; restores the terminal on exit."""

    def __enter__(self):
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def read(self) -> str:
        if not select.select([self.fd], [], [], 0.0)[0]:
            return ""
        return os.read(self.fd, 64).decode("utf-8", "replace")

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


def run_calibrate(rig: Rig, head: str, target: tuple[str, int], log: Log = print) -> int:
    try:
        f = rig.fixture(head)
    except KeyError:
        log(f"no fixture named {head!r}")
        return 2
    if f not in rig.heads():
        log(f"{head} has no locked pan and tilt channels — not a moving head")
        return 2
    if not sys.stdin.isatty():
        log("calibrate needs an interactive terminal")
        return 2
    path = calibration_path(rig, head)
    existing = load_calibration(path) if path.exists() else []
    start = (float(existing[-1].pan), float(existing[-1].tilt)) if existing else home_position(f)
    cal = Calibrator(start, existing)
    net = rig.network
    receiver = HelmetReceiver(rig.follow.osc_port)
    motion = HeadMotion(home_position(f), CALIBRATE_STEPS_PER_FRAME)
    pipeline = Pipeline(rig)
    sock = ArtNetSocket(bind_port=PORT, fallback_to_any_port=True)
    log(f"calibrate {head}: {len(existing)} existing point(s) in {path}")
    log("arrows = pan/tilt (Shift: 5 steps), Enter = record, u = undo, s = save and quit, "
        "q = quit without saving")
    message = ""
    try:
        with _RawTerminal() as term:
            for now, next_tick in _ticks(net.fps):
                receiver.poll(0.0)
                moving = not motion.at(tuple(cal.aim))
                for key in parse_keys(term.read()):
                    message = cal.handle(key, receiver.position, receiver.age(now), moving) or message
                if cal.result:
                    break
                motion.step(tuple(cal.aim), 1.0 / net.fps)
                targets = rig.home_states()
                targets[head] = colour_to_state(f.profile, WHITE, CALIBRATE_LEVEL)
                frame = rig.render(pipeline.step(targets, now), moves=moves_of({head: motion}))
                sock.send(target, net.universe, frame)
                pos, age = receiver.position, receiver.age(now)
                helmet = ("helmet: none" if pos is None or age > RECORD_MAX_AGE_S
                          else f"helmet {pos[0]:7.1f} {pos[1]:7.1f}")
                sys.stdout.write(f"\r\x1b[Kpan {cal.aim[0]:3d} tilt {cal.aim[1]:3d} | {helmet} | "
                                 f"{len(cal.points)} points | {message}")
                sys.stdout.flush()
                time.sleep(max(0.0, next_tick + 1.0 / net.fps - time.perf_counter()))
    except KeyboardInterrupt:
        cal.result = "quit"
    finally:
        sys.stdout.write("\n")
        if cal.result == "save":
            save_calibration(path, head, cal.points)
            log(f"saved {len(cal.points)} point(s) to {path}")
            if len(cal.points) < 3:
                log("follow needs at least 3 points (6-9 spread over the floor is better)")
        else:
            log("not saved")
        log("head back to home…")
        return_home(rig, sock, target, pipeline, {head: motion}, log)
        sock.close()
        receiver.close()
    return 0

"""Screen mode: lights take colour from a display (the LED wall is just another monitor).
Capture runs in a thread, every zone gets averaged.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections import deque

import numpy as np

from .artnet import PORT, ArtNetSocket
from .color import Rgb, colour_to_state, linear_to_srgb, rgb_to_xyz
from .rig import Rig, Screen, State, Zone
from .runner import Log, _ticks, fade_home
from .safety import Pipeline

SAMPLE_WIDTH = 96    # make it small, we only need averages

PERMISSION_HELP = (
    "Screen capture failed. macOS only lets apps with Screen Recording permission\n"
    "capture the display. Open System Settings > Privacy & Security > Screen Recording\n"
    "(\"Screen & System Audio Recording\" on newer macOS), switch on the app you run\n"
    "xrlight from (Terminal, iTerm, VS Code, ...), then quit and reopen that app.")


class CaptureError(RuntimeError):
    pass


# maths

def srgb_to_linear_np(a: np.ndarray) -> np.ndarray:
    """same as srgb_to_linear but numpy"""
    a = np.clip(a, 0.0, 1.0)
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def downsample(frame: np.ndarray, width: int = SAMPLE_WIDTH, bgra: bool = True) -> np.ndarray:
    """make frame small + linear"""
    step = max(1, round(frame.shape[1] / width))
    small = frame[::step, ::step]
    rgb = small[..., 2::-1] if bgra else small[..., :3]
    return srgb_to_linear_np(rgb.astype(np.float32) / 255.0)


def zone_slice(rect: tuple[float, ...], h: int, w: int) -> tuple[slice, slice]:
    x0, y0, x1, y1 = rect
    ix0, iy0 = min(int(round(x0 * w)), w - 1), min(int(round(y0 * h)), h - 1)
    ix1, iy1 = max(int(round(x1 * w)), ix0 + 1), max(int(round(y1 * h)), iy0 + 1)
    return slice(iy0, iy1), slice(ix0, ix1)


def zone_colours(linear: np.ndarray, zones: list[Zone]) -> dict[str, Rgb]:
    """average colour per zone"""
    h, w = linear.shape[:2]
    out = {}
    for z in zones:
        ys, xs = zone_slice(z.rect, h, w)
        r, g, b = linear[ys, xs].reshape(-1, 3).mean(axis=0)
        out[z.fixture] = (float(r), float(g), float(b))
    return out


def frame_colours(frame: np.ndarray, zones: list[Zone], bgra: bool = True) -> dict[str, Rgb]:
    return zone_colours(downsample(frame, bgra=bgra), zones)


def level_of(rgb: Rgb, gain: float, screen: Screen | None = None) -> float:
    """brightness of a zone"""
    y = rgb_to_xyz(rgb)[1]
    lum = linear_to_srgb((y, y, y))[0]
    if screen is not None:
        lum = screen.curve(lum)
    return min(1.0, gain * lum)


def targets_from_colours(rig: Rig, colours: dict[str, Rgb] | None,
                         gain: float) -> dict[str, State]:
    """lights with a zone follow it, the rest stay home"""
    targets = rig.home_states()
    zone_gain = {z.fixture: z.gain for z in rig.zones}
    for f in rig.fixtures:
        if colours and f.name in colours:
            rgb = colours[f.name]
            level = level_of(rgb, gain * zone_gain.get(f.name, 1.0), rig.screen)
            targets[f.name] = colour_to_state(f.profile, rgb, level)
    return targets


class DelayBuffer:
    """gives back what was pushed delay_s ago"""

    def __init__(self, delay_s: float):
        self.delay = max(0.0, delay_s)
        self.items: deque[tuple[float, object]] = deque()

    def push(self, t: float, value) -> None:
        self.items.append((t, value))

    def get(self, now: float):
        cutoff = now - self.delay
        while len(self.items) > 1 and self.items[1][0] <= cutoff:
            self.items.popleft()
        if self.items and self.items[0][0] <= cutoff:
            return self.items[0][1]
        return None


# capture

def _mss():
    try:
        import mss
    except ImportError as exc:
        raise CaptureError("screen mode needs mss: .venv/bin/pip install -r requirements.txt") from exc
    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def monitors() -> list[dict]:
    """0 is all screens together"""
    with _mss() as sct:
        return list(sct.monitors)


def has_screen_permission() -> bool:
    """mac screen recording permission"""
    if sys.platform != "darwin":
        return True
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return True


class ScreenCapture(threading.Thread):
    def __init__(self, monitor: int, fps: float, zones: list[Zone]):
        super().__init__(daemon=True, name="screen-capture")
        self.monitor, self.period, self.zones = monitor, 1.0 / fps, zones
        self.lock = threading.Lock()
        self.latest: tuple[int, float, dict[str, Rgb]] | None = None   # (seq, time, colours)
        self.frames = 0
        self.error: BaseException | None = None
        self.started_ok = threading.Event()
        self.stop_event = threading.Event()

    def run(self) -> None:
        seq = 0
        try:
            with _mss() as sct:                # has to be made in this thread
                area = sct.monitors[self.monitor]
                next_tick = time.perf_counter()
                while not self.stop_event.is_set():
                    t = time.perf_counter()
                    colours = frame_colours(np.asarray(sct.grab(area)), self.zones)
                    seq += 1
                    with self.lock:
                        self.latest = (seq, t, colours)
                        self.frames += 1
                    self.started_ok.set()
                    next_tick = max(next_tick + self.period, time.perf_counter())
                    self.stop_event.wait(next_tick - time.perf_counter())
        except BaseException as exc:
            self.error = exc
        finally:
            self.started_ok.set()

    def take_count(self) -> int:
        with self.lock:
            n, self.frames = self.frames, 0
        return n

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=1.0)


def run_screen(rig: Rig, target: tuple[str, int], monitor: int = 1, capture_fps: float = 30.0,
               gain: float = 1.0, delay_ms: float = 0.0, seconds: float | None = None,
               log: Log = print) -> int:
    if not rig.zones:
        log("no [[zone]] entries in the config — nothing to follow")
        return 2
    if not has_screen_permission():
        log(PERMISSION_HELP)
        return 1
    capture = ScreenCapture(monitor, capture_fps, rig.zones)
    capture.start()
    capture.started_ok.wait(5.0)
    if capture.error is not None or capture.latest is None:
        capture.stop()
        log(f"{PERMISSION_HELP}\n(error: {capture.error or 'no frame within 5 s'})")
        return 1

    net = rig.network
    pipeline = Pipeline(rig)
    sock = ArtNetSocket(bind_port=PORT, fallback_to_any_port=True)
    delay = DelayBuffer(delay_ms / 1000.0)
    log(f"screen: display {monitor} at {capture_fps:g} fps -> {target[0]}:{target[1]} "
        f"universe {net.universe} at {net.fps:g} fps, gain {gain:g}, delay {delay_ms:g} ms")
    seen = 0
    frames_out = 0
    start = time.perf_counter()
    report_at = start + 1.0
    try:
        for now, next_tick in _ticks(net.fps):
            if seconds is not None and now - start >= seconds:
                break
            if capture.error is not None:
                log(f"capture stopped: {capture.error}")
                break
            with capture.lock:
                latest = capture.latest
            if latest is not None and latest[0] != seen:
                seen = latest[0]
                delay.push(latest[1], latest[2])
            targets = targets_from_colours(rig, delay.get(now), gain)
            sock.send(target, net.universe, rig.render(pipeline.step(targets, now)))
            frames_out += 1
            if now >= report_at:
                log(f"capture {capture.take_count():3d} fps | out {frames_out:3d} fps | "
                    f"guard held {pipeline.blocked_frames} fixture-frames so far")
                frames_out = 0
                report_at = now + 1.0
            time.sleep(max(0.0, next_tick + 1.0 / net.fps - time.perf_counter()))
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
        log("fading to home look…")
        fade_home(rig, sock, target, pipeline)
        sock.close()
    return 0

"""The three ways to run: send a pattern, proxy Unreal, or monitor Art-Net."""
from __future__ import annotations

import sys
import time
from typing import Callable

from .artnet import PORT, ArtNetSocket
from .color import display_colour
from .patterns import Pattern
from .rig import Rig
from .safety import Pipeline

Log = Callable[[str], None]


def _ticks(fps: float):
    """Yield (now, next_tick) at a steady frame rate without drifting."""
    period = 1.0 / fps
    next_tick = time.perf_counter()
    while True:
        now = time.perf_counter()
        yield now, next_tick
        next_tick += period
        if next_tick < now - period:          # fell behind (sleep, debugger): resync
            next_tick = now + period


def fade_home(rig: Rig, sock: ArtNetSocket, target: tuple[str, int], pipeline: Pipeline,
              seconds: float = 1.5, base: bytes | None = None) -> None:
    """Fail-safe: ease the rig back to its home look before stopping."""
    end = time.perf_counter() + seconds
    for now, next_tick in _ticks(rig.network.fps):
        if now >= end:
            break
        time.sleep(max(0.0, next_tick - time.perf_counter()))
        frame = rig.render(pipeline.step(rig.home_states(), time.perf_counter()), base)
        sock.send(target, rig.network.universe, frame)


def run_pattern(rig: Rig, pattern: Pattern, target: tuple[str, int], raw: bool = False,
                seconds: float | None = None, log: Log = print) -> None:
    """Send a test pattern. raw=True skips smoothing and the flash guard (fake Unreal)."""
    pipeline = Pipeline(rig)
    sock = ArtNetSocket(bind_port=PORT, fallback_to_any_port=True)
    log(f"sending to {target[0]}:{target[1]} universe {rig.network.universe} "
        f"at {rig.network.fps:g} fps{' (RAW, no safety layer)' if raw else ''}")
    start = time.perf_counter()
    label_shown = None
    try:
        for now, next_tick in _ticks(rig.network.fps):
            t = now - start
            if seconds is not None and t >= seconds:
                break
            targets, label = pattern(rig, t)
            if label != label_shown:
                log(f"[{t:6.1f}s] {label}")
                label_shown = label
            states = targets if raw else pipeline.step(targets, now)
            sock.send(target, rig.network.universe, rig.render(states))
            time.sleep(max(0.0, next_tick + 1.0 / rig.network.fps - time.perf_counter()))
    except KeyboardInterrupt:
        pass
    finally:
        if not raw:
            log("fading to home look…")
            fade_home(rig, sock, target, pipeline)
            if pipeline.blocked_frames:
                log(f"flash guard held back {pipeline.blocked_frames} fixture-frames")
        sock.close()


def run_proxy(rig: Rig, target: tuple[str, int], listen: tuple[str, int],
              seconds: float | None = None, log: Log = print) -> None:
    """Receive Art-Net (from Unreal), make it safe, forward it to the ODE."""
    net, safety = rig.network, rig.safety
    if listen[1] == target[1] and target[0] in ("127.0.0.1", "localhost"):
        raise ValueError("proxy would send to its own port — use different ports")
    pipeline = Pipeline(rig)
    sock = ArtNetSocket(*listen)
    log(f"proxy: listening on {listen[0]}:{sock.port} for universe {net.input_universe}, "
        f"forwarding to {target[0]}:{target[1]} universe {net.universe}")
    base = bytearray(512)
    targets = rig.home_states()
    last_input: float | None = None
    stale_logged = False
    frames_in = frames_out = 0
    sources: set[str] = set()
    start = time.perf_counter()
    report_at = start + 1.0
    period = 1.0 / net.fps
    next_tick = start
    try:
        while seconds is None or time.perf_counter() - start < seconds:
            for universe, data, addr in sock.receive(next_tick - time.perf_counter()):
                if universe != net.input_universe:
                    continue
                frame = bytearray(512)
                frame[:len(data)] = data[:512]
                base, targets = frame, rig.decode(frame)
                last_input = time.perf_counter()
                frames_in += 1
                sources.add(addr[0])
            now = time.perf_counter()
            if now < next_tick:
                continue
            next_tick = max(next_tick + period, now)
            stale = last_input is None or now - last_input > safety.input_timeout_s
            if stale and not stale_logged:
                log("no input -> home look")
                stale_logged = True
            elif not stale and stale_logged:
                log(f"input from {', '.join(sorted(sources))}")
                stale_logged = False
            states = pipeline.step(rig.home_states() if stale else targets, now)
            frame = rig.render(states, base if safety.passthrough_unpatched else None)
            sock.send(target, net.universe, frame)
            frames_out += 1
            if now >= report_at:
                log(f"in {frames_in:3d} fps | out {frames_out:3d} fps | "
                    f"guard held {pipeline.blocked_frames} fixture-frames so far")
                frames_in = frames_out = 0
                report_at = now + 1.0
    except KeyboardInterrupt:
        pass
    finally:
        log("fading to home look…")
        fade_home(rig, sock, target, pipeline,
                  base=base if safety.passthrough_unpatched else None)
        sock.close()


def run_monitor(rig: Rig, listen: tuple[str, int], universe: int,
                fps: float = 15.0, seconds: float | None = None,
                out=sys.stdout) -> None:
    """Draw every patched fixture in the terminal from incoming Art-Net."""
    sock = ArtNetSocket(*listen)
    frame = bytes(512)
    source, count, rate = "-", 0, 0
    start = last_rate = time.perf_counter()
    out.write("\x1b[2J")
    try:
        while seconds is None or time.perf_counter() - start < seconds:
            for u, data, addr in sock.receive(1.0 / fps):
                if u == universe:
                    frame = data.ljust(512, b"\x00")
                    source = f"{addr[0]}:{addr[1]}"
                    count += 1
            now = time.perf_counter()
            if now - last_rate >= 1.0:
                rate, count, last_rate = count, 0, now
            out.write(_draw(rig, frame, universe, source, rate, listen[1]))
            out.flush()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[0m\n")
        sock.close()


SHORT = {"red": "R", "green": "G", "blue": "B", "white": "W", "white_1": "W1",
         "white_2": "W2", "amber": "A", "cyan": "C", "lime": "L", "dimmer": "Dim",
         "strobe": "Strb", "mode": "Mode", "unknown": "?", "ctemp": "CT", "curve": "Crv",
         "cct": "CCT", "gm": "GM", "zoom": "Zm", "focus": "Fo", "reserved": "Rsv",
         "macro": "Mac", "colour_change": "CChg",
         "pan": "Pan", "tilt": "Tilt", "speed": "Spd", "reset": "Rst"}


def _draw(rig: Rig, frame: bytes, universe: int, source: str, rate: int, port: int) -> str:
    lines = ["\x1b[H",
             f"Art-Net monitor  port {port}  universe {universe}  from {source}  {rate} pkt/s\x1b[K",
             "\x1b[K"]
    states = rig.decode(frame)
    patched = set()
    for f in rig.fixtures:
        r, g, b = (int(round(c * 255)) for c in display_colour(f.profile, states[f.name]))
        level = f.profile.brightness(states[f.name])
        values = []
        for c in f.profile.channels:
            i = f.index(c)
            patched.add(i)
            mark = "!" if c.locked and frame[i] != c.lock else " "
            values.append(f"{SHORT.get(c.attr, c.attr[:4])}:{frame[i]:3d}{mark}")
        lines.append(f"{f.name:13.13s} \x1b[48;2;{r};{g};{b}m        \x1b[0m "
                     f"{level * 100:4.0f}%  " + " ".join(values) + "\x1b[K")
    stray = [f"{i + 1}={v}" for i, v in enumerate(frame) if v and i not in patched]
    lines += ["\x1b[K", "unpatched non-zero: " + (", ".join(stray[:16]) or "none")
              + (" …" if len(stray) > 16 else "") + "\x1b[K",
              "'!' = a locked channel received a non-zero value\x1b[K", "\x1b[J"]
    return "\n".join(lines)

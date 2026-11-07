"""Command line: python -m xrlight {check,test,proxy,monitor,screen,helmet,calibrate,follow,fake-helmet} ..."""
from __future__ import annotations

import argparse
import sys
from functools import partial
from pathlib import Path

from .artnet import PortBusyError, parse_target
from .osc import run_fake_helmet, run_helmet
from .patterns import PATTERNS
from .rig import Rig
from .runner import run_monitor, run_pattern, run_proxy

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "stage.toml"
LEVEL_PATTERNS = {"identify", "palette"}   # these take --level


def cmd_check(rig: Rig, args) -> None:
    n = rig.network
    print(f"target {n.target_ip}:{n.target_port}  universe {n.universe}  {n.fps:g} fps")
    print(f"proxy listens on {n.listen_ip}:{n.listen_port} for universe {n.input_universe}\n")
    width = max(len(c.attr) for f in rig.fixtures for c in f.profile.channels)
    for f in sorted(rig.fixtures, key=lambda f: f.address):   # patch in address order
        print(f"{f.address:3d}-{f.last_address:<3d} {f.name:13s} {f.profile.key}  ({f.zone})")
        for c in f.profile.channels:
            if not c.locked:
                status = f"range {c.min}-{c.max}"
            elif c.attr in f.fixed:
                status = f"LOCKED at {f.lock_value(c)} (fixed in stage.toml; profile {c.lock})"
            else:
                status = f"LOCKED at {c.lock}"
            note = f"  — {c.note}" if c.note else ""
            print(f"        ch{c.offset:<2d} (DMX {f.index(c) + 1:3d}) {c.attr:{width}s} {status}{note}")
    used = sum(f.profile.footprint for f in rig.fixtures)
    print(f"\n{len(rig.fixtures)} fixtures, {used} of 512 channels, no overlaps")
    if rig.zones:
        print("\nscreen zones [x0, y0, x1, y1]:")
        for z in rig.zones:
            gain = f"  gain {z.gain:g}" if z.gain != 1.0 else ""
            print(f"  {z.fixture:13s} [{', '.join(f'{v:.3g}' for v in z.rect)}]{gain}")
        s = rig.screen
        print(f"screen curve: gamma {s.gamma:g}, black_floor {s.black_floor:g}")
    fo = rig.follow
    print(f"\nfollow: OSC port {fo.osc_port}, light {fo.follow_level:g}, "
          f"max {fo.max_steps_per_second:g} steps/s, hold {fo.hold_s:g} s")
    for f in rig.heads():
        path = rig.config_dir / "calibration" / f"{f.name}.toml" if rig.config_dir else None
        found = "calibrated" if path is not None and path.exists() else "not calibrated"
        print(f"  {f.name:13s} {found}")


def cmd_screen(rig: Rig, args, target: tuple[str, int]) -> int:
    try:
        from . import screen     # numpy + mss
    except ImportError as exc:
        print(f"screen mode needs numpy and mss ({exc}): pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    try:
        displays = screen.monitors()
    except screen.CaptureError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.list_monitors:
        for i, m in enumerate(displays):
            label = "  (all displays combined)" if i == 0 else ""
            print(f"{i}: {m['width']}x{m['height']} at ({m['left']}, {m['top']}){label}")
        return 0
    if not 0 <= args.monitor < len(displays):
        print(f"no display {args.monitor}; see xrlight screen --list-monitors", file=sys.stderr)
        return 2
    return screen.run_screen(rig, target, args.monitor, args.fps, args.gain, args.delay_ms,
                             args.seconds)


def cmd_head(rig: Rig, args, target: tuple[str, int]) -> int:
    try:
        from . import head       # numpy + scipy
    except ImportError as exc:
        print(f"{args.command} needs numpy and scipy ({exc}): pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    if args.command == "calibrate":
        return head.run_calibrate(rig, args.head, target)
    try:
        colour = head.parse_colour(args.colour)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return head.run_follow(rig, target, args.seconds, args.others_off, args.head, colour)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xrlight", description="Reactive lighting for the XR Stage")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to stage.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate the config and print the patch")

    p_test = sub.add_parser("test", help="send a test pattern")
    p_test.add_argument("--pattern", choices=sorted(PATTERNS), default="identify")
    p_test.add_argument("--target", help="IP[:PORT], default from config")
    p_test.add_argument("--seconds", type=float, help="stop after N seconds")
    p_test.add_argument("--raw", action="store_true",
                        help="skip smoothing and flash guard (to fake an unsafe source)")
    p_test.add_argument("--level", type=float,
                        help="brightness 0..1 for identify and palette (default 0.3 / 0.6)")

    p_proxy = sub.add_parser("proxy", help="receive Art-Net, make it safe, forward it")
    p_proxy.add_argument("--target", help="IP[:PORT], default from config")
    p_proxy.add_argument("--listen-port", type=int, help="default from config")
    p_proxy.add_argument("--seconds", type=float)

    p_mon = sub.add_parser("monitor", help="show incoming Art-Net as fixtures in the terminal")
    p_mon.add_argument("--port", type=int, default=6455)
    p_mon.add_argument("--universe", type=int, help="default: universe from config")
    p_mon.add_argument("--seconds", type=float)

    p_scr = sub.add_parser("screen", help="lights follow the picture on a display (LED wall)")
    p_scr.add_argument("--list-monitors", action="store_true",
                       help="print the displays that can be captured and exit")
    p_scr.add_argument("--monitor", type=int, default=1, help="display to capture (default 1)")
    p_scr.add_argument("--target", help="IP[:PORT], default from config")
    p_scr.add_argument("--fps", type=float, default=30.0,
                       help="capture rate (default 30); output stays at the config fps")
    p_scr.add_argument("--gain", type=float, default=1.0, help="brightness gain 0..2 (default 1)")
    p_scr.add_argument("--delay-ms", type=float, default=0.0,
                       help="delay the lights to line up with the wall (default 0)")
    p_scr.add_argument("--seconds", type=float, help="stop after N seconds")

    p_hel = sub.add_parser("helmet", help="print the helmet position received over OSC (no DMX)")
    p_hel.add_argument("--port", type=int, help="OSC port, default from config")
    p_hel.add_argument("--seconds", type=float)

    p_cal = sub.add_parser("calibrate", help="teach a moving head where the helmet is (interactive)")
    p_cal.add_argument("--head", required=True, help='fixture name, e.g. "Head left"')
    p_cal.add_argument("--target", help="IP[:PORT], default from config")

    p_fol = sub.add_parser("follow", help="calibrated moving heads follow the helmet")
    p_fol.add_argument("--target", help="IP[:PORT], default from config")
    p_fol.add_argument("--seconds", type=float)
    p_fol.add_argument("--others-off", action="store_true",
                       help="keep every fixture except the followed heads dark (exit still fades home)")
    p_fol.add_argument("--head", action="append", metavar="NAME",
                       help='only this head follows (repeat for more; default: every calibrated head)')
    p_fol.add_argument("--colour", default="ffffff",
                       help="light colour of the following heads, sRGB hex (default ffffff)")

    p_fake = sub.add_parser("fake-helmet", help="send a helmet walking a circle over OSC")
    p_fake.add_argument("--target", default="127.0.0.1:9000", help="IP[:PORT] (default 127.0.0.1:9000)")
    p_fake.add_argument("--radius", type=float, default=150.0, help="cm (default 150)")
    p_fake.add_argument("--period", type=float, default=30.0, help="seconds per lap (default 30)")
    p_fake.add_argument("--rate", type=float, default=50.0, help="messages per second (default 50)")
    p_fake.add_argument("--seconds", type=float)

    args = parser.parse_args(argv)
    if getattr(args, "level", None) is not None and not 0.0 <= args.level <= 1.0:
        parser.error("--level must be between 0 and 1")
    if args.command == "screen":
        if not 0.0 <= args.gain <= 2.0:
            parser.error("--gain must be between 0 and 2")
        if args.fps <= 0:
            parser.error("--fps must be above 0")
        if args.delay_ms < 0:
            parser.error("--delay-ms cannot be negative")
    try:
        rig = Rig.load(args.config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    n = rig.network
    default_target = (n.target_ip, n.target_port)
    try:
        if args.command == "check":
            cmd_check(rig, args)
        elif args.command == "test":
            target = parse_target(args.target) if args.target else default_target
            pattern = PATTERNS[args.pattern]
            if args.level is not None and args.pattern in LEVEL_PATTERNS:
                pattern = partial(pattern, level=args.level)
            run_pattern(rig, pattern, target, raw=args.raw, seconds=args.seconds)
        elif args.command == "proxy":
            target = parse_target(args.target) if args.target else default_target
            listen = (n.listen_ip, args.listen_port or n.listen_port)
            run_proxy(rig, target, listen, seconds=args.seconds)
        elif args.command == "monitor":
            run_monitor(rig, ("0.0.0.0", args.port), args.universe or n.universe,
                        seconds=args.seconds)
        elif args.command == "screen":
            target = parse_target(args.target) if args.target else default_target
            return cmd_screen(rig, args, target)
        elif args.command == "helmet":
            run_helmet(args.port or rig.follow.osc_port, seconds=args.seconds)
        elif args.command in ("calibrate", "follow"):
            target = parse_target(args.target) if args.target else default_target
            return cmd_head(rig, args, target)
        elif args.command == "fake-helmet":
            run_fake_helmet(parse_target(args.target, 9000), args.radius, args.period,
                            args.rate, seconds=args.seconds)
    except (PortBusyError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0

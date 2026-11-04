"""Command line: python -m xrlight {check,test,proxy,monitor} ..."""
from __future__ import annotations

import argparse
import sys
from functools import partial
from pathlib import Path

from .artnet import PortBusyError, parse_target
from .patterns import PATTERNS
from .rig import Rig
from .runner import run_monitor, run_pattern, run_proxy

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "stage.toml"
LEVEL_PATTERNS = {"identify", "palette"}   # patterns that take a level argument


def cmd_check(rig: Rig, args) -> None:
    n = rig.network
    print(f"target {n.target_ip}:{n.target_port}  universe {n.universe}  {n.fps:g} fps")
    print(f"proxy listens on {n.listen_ip}:{n.listen_port} for universe {n.input_universe}\n")
    for f in rig.fixtures:
        print(f"{f.address:3d}-{f.last_address:<3d} {f.name:13s} {f.profile.key}  ({f.zone})")
        for c in f.profile.channels:
            status = f"LOCKED at {c.lock}" if c.locked else f"range {c.min}-{c.max}"
            note = f"  — {c.note}" if c.note else ""
            print(f"        ch{c.offset:<2d} (DMX {f.index(c) + 1:3d}) {c.attr:8s} {status}{note}")
    used = sum(f.profile.footprint for f in rig.fixtures)
    print(f"\n{len(rig.fixtures)} fixtures, {used} of 512 channels, no overlaps")


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

    args = parser.parse_args(argv)
    if getattr(args, "level", None) is not None and not 0.0 <= args.level <= 1.0:
        parser.error("--level must be between 0 and 1")
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
    except PortBusyError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0

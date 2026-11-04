"""Test patterns. Each pattern maps time t (seconds) to (targets, label)."""
from __future__ import annotations

from typing import Callable

from .color import colour_to_state, srgb_to_linear
from .rig import Rig, State

Targets = dict[str, State]
Pattern = Callable[[Rig, float], tuple[Targets, str]]

# sRGB colours as if they were sampled from the LED wall
LOOKS = {
    "daylight": (1.00, 0.97, 0.92),
    "sunset":   (1.00, 0.45, 0.15),
    "night":    (0.20, 0.30, 0.85),
    "forest":   (0.40, 0.75, 0.30),
}


def home(rig: Rig, t: float) -> tuple[Targets, str]:
    return rig.home_states(), "home look"


def identify(rig: Rig, t: float, step_s: float = 2.0, level: float = 0.3) -> tuple[Targets, str]:
    """Light one fixture at a time at a low level — use this to check the patch."""
    targets = rig.zero_states()
    f = rig.fixtures[int(t // step_s) % len(rig.fixtures)]
    targets[f.name] = colour_to_state(f.profile, (1.0, 1.0, 1.0), level)
    return targets, f"{f.name} (address {f.address}-{f.last_address})"


def palette(rig: Rig, t: float, hold_s: float = 6.0, fade_s: float = 2.0,
            level: float = 0.6) -> tuple[Targets, str]:
    """Cycle through scene colours; every fixture renders them its own way."""
    names = list(LOOKS)
    period = hold_s + fade_s
    i = int(t // period) % len(names)
    j = (i + 1) % len(names)
    phase = t % period
    mix = 0.0 if phase < hold_s else (phase - hold_s) / fade_s
    a, b = srgb_to_linear(LOOKS[names[i]]), srgb_to_linear(LOOKS[names[j]])
    colour = tuple(x + (y - x) * mix for x, y in zip(a, b))
    targets = {f.name: colour_to_state(f.profile, colour, level) for f in rig.fixtures}
    label = names[i] if mix == 0.0 else f"{names[i]} -> {names[j]}"
    return targets, label


def flash_test(rig: Rig, t: float, hz: float = 10.0) -> tuple[Targets, str]:
    """Request a full white strobe — the safety layer must slow it down."""
    on = int(t * hz * 2) % 2 == 0
    targets = {f.name: colour_to_state(f.profile, (1.0, 1.0, 1.0), 1.0 if on else 0.0)
               for f in rig.fixtures}
    return targets, f"requesting {hz:g} Hz full flashes"


PATTERNS: dict[str, Pattern] = {
    "home": home,
    "identify": identify,
    "palette": palette,
    "flash-test": flash_test,
}

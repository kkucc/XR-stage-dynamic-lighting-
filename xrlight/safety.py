"""Safety layer between "what we want" and "what we send".

Every frame goes through, per fixture:
  1. master level      — global brightness ceiling
  2. smoothing         — exponential filter, removes jitter and hard cuts
  3. flash guard       — at most N flashes in any 1-second window
Locked channels (strobe, mode, unknown) are handled by the rig and can never
be driven at all. Range limits (min/max) are applied when rendering DMX.

A flash is a pair of opposite brightness changes, each at least `threshold`
(10% by default), which follows the usual photosensitivity guidance of at most
three flashes per second. The guard works on the values after 8-bit rounding,
i.e. what the fixtures actually receive. Brightness is the DMX-level estimate
from Profile.brightness, not measured luminance, so treat it as an approximation.
"""
from __future__ import annotations

import math
from collections import deque

from .rig import DIMMER, Profile, Rig, State, clamp01


class FlashGuard:
    """Limits how often one fixture's brightness may swing up and down.

    Turning points are tracked with hysteresis ("zigzag"): a change only counts
    as a transition once brightness has moved at least `threshold` away from the
    last peak or trough. Tiny dips therefore never reset the count. Two
    transitions make one flash; at most 2 x max_flashes_per_second transitions
    are allowed in any `window_s`. When the budget is used up, brightness is
    kept inside a band around the last extreme until the window frees up.

    Call limit() with the brightness you would like to show, then commit() with
    the brightness that was actually sent (after 8-bit rounding).
    """

    CLAMP_MARGIN = 0.85   # blocked output stays within 85% of the threshold (room for rounding)

    def __init__(self, max_flashes_per_second: float = 3.0, threshold: float = 0.10,
                 window_s: float = 1.0):
        self.max_transitions = max(0, int(round(2 * max_flashes_per_second)))
        self.threshold = threshold
        self.window = window_s
        self.events: deque[float] = deque()
        self.direction = 0        # +1 rising, -1 falling, 0 no transition yet
        self.extreme = 0.0        # running peak (rising) or trough (falling)
        self.lo = self.hi = None  # range seen before the first transition
        self.blocked_frames = 0

    def _purge(self, now: float) -> None:
        while self.events and now - self.events[0] >= self.window:
            self.events.popleft()

    def _would_count(self, v: float) -> bool:
        if self.lo is None:
            return False
        if self.direction == 0:
            return v - self.lo >= self.threshold or self.hi - v >= self.threshold
        if self.direction > 0:
            return self.extreme - v >= self.threshold
        return v - self.extreme >= self.threshold

    def limit(self, proposed: float, now: float) -> float:
        self._purge(now)
        if not self._would_count(proposed) or len(self.events) < self.max_transitions:
            return proposed
        self.blocked_frames += 1
        c = self.threshold * self.CLAMP_MARGIN
        if self.direction == 0:
            return min(max(proposed, self.hi - c), self.lo + c)
        if self.direction > 0:
            return max(proposed, self.extreme - c)
        return min(proposed, self.extreme + c)

    def commit(self, actual: float, now: float) -> None:
        if self.lo is None:
            self.lo = self.hi = self.extreme = actual
            return
        t = self.threshold
        if self.direction == 0:
            self.lo, self.hi = min(self.lo, actual), max(self.hi, actual)
            if actual - self.lo >= t:
                self.direction, self.extreme = 1, actual
                self.events.append(now)
            elif self.hi - actual >= t:
                self.direction, self.extreme = -1, actual
                self.events.append(now)
        elif self.direction > 0:
            if actual > self.extreme:
                self.extreme = actual
            elif self.extreme - actual >= t:
                self.direction, self.extreme = -1, actual
                self.events.append(now)
        else:
            if actual < self.extreme:
                self.extreme = actual
            elif actual - self.extreme >= t:
                self.direction, self.extreme = 1, actual
                self.events.append(now)

    def process(self, proposed: float, now: float) -> float:
        """limit() and commit() in one go, for signals without rounding."""
        allowed = self.limit(proposed, now)
        self.commit(allowed, now)
        return allowed


def quantize(profile: Profile, state: State) -> State:
    """Round every value to what the fixture will really receive over 8-bit DMX."""
    return {a: c.from_dmx(c.to_dmx(state.get(a, 0.0))) for a, c in profile.by_attr.items()}


def _lerp(a: State, b: State, t: float) -> State:
    return {k: a.get(k, 0.0) + (b.get(k, 0.0) - a.get(k, 0.0)) * t for k in b}


def blend_to_brightness(profile: Profile, prev: State, proposed: State, target: float) -> State:
    """Move from `prev` towards `proposed` only as far as brightness `target` allows."""
    b_prev, b_prop = profile.brightness(prev), profile.brightness(proposed)
    if (target - b_prev) * (target - b_prop) > 0:           # target outside the segment
        return dict(prev) if abs(target - b_prev) < abs(target - b_prop) else dict(proposed)
    lo, hi = 0.0, 1.0
    rising = b_prop > b_prev
    for _ in range(20):
        mid = (lo + hi) / 2
        b_mid = profile.brightness(_lerp(prev, proposed, mid))
        if (b_mid < target) == rising:
            lo = mid
        else:
            hi = mid
    return _lerp(prev, proposed, (lo + hi) / 2)


class Pipeline:
    def __init__(self, rig: Rig, fps: float | None = None):
        self.rig = rig
        s = rig.safety
        self.dt = 1.0 / (fps or rig.network.fps)
        tau = s.smoothing_ms / 1000.0
        self.alpha = 1.0 if tau <= 0 else 1.0 - math.exp(-self.dt / tau)
        self.smoothed = rig.zero_states()   # start dark and fade in
        self.out = rig.zero_states()
        self.guards = {f.name: FlashGuard(s.max_flashes_per_second, s.flash_threshold)
                       for f in rig.fixtures}

    @property
    def blocked_frames(self) -> int:
        return sum(g.blocked_frames for g in self.guards.values())

    def step(self, targets: dict[str, State], now: float) -> dict[str, State]:
        master = clamp01(self.rig.safety.master)
        for f in self.rig.fixtures:
            p = f.profile
            target = {a: clamp01(targets.get(f.name, {}).get(a, 0.0)) for a in p.by_attr}
            if DIMMER in target:
                target[DIMMER] *= master
            else:
                target = {a: v * master for a, v in target.items()}
            smooth = self.smoothed[f.name]
            for a, v in target.items():
                smooth[a] += self.alpha * (v - smooth[a])
            proposed = quantize(p, smooth)
            wanted = p.brightness(proposed)
            guard = self.guards[f.name]
            allowed = guard.limit(wanted, now)
            if abs(allowed - wanted) > 1e-9:
                proposed = quantize(p, blend_to_brightness(p, self.out[f.name], proposed, allowed))
            guard.commit(p.brightness(proposed), now)
            self.out[f.name] = proposed
        return {name: dict(state) for name, state in self.out.items()}

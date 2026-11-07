"""Safety: master level, smoothing, flash guard (max 3 flashes a second).

flash = up and down by at least 10%. brightness is a guess, not measured.
"""
from __future__ import annotations

import math
from collections import deque

from .rig import DIMMER, Profile, Rig, State, clamp01


class FlashGuard:
    """counts flashes per light (zigzag between peaks), too many -> hold it.

    limit() what you want, then commit() what was really sent.
    """

    CLAMP_MARGIN = 0.85   # bit under threshold because of rounding

    def __init__(self, max_flashes_per_second: float = 3.0, threshold: float = 0.10,
                 window_s: float = 1.0):
        self.max_transitions = max(0, int(round(2 * max_flashes_per_second)))
        self.threshold = threshold
        self.window = window_s
        self.events: deque[float] = deque()
        self.direction = 0        # 1 up, -1 down, 0 not yet
        self.extreme = 0.0        # last peak or bottom
        self.lo = self.hi = None
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
        """limit + commit"""
        allowed = self.limit(proposed, now)
        self.commit(allowed, now)
        return allowed


def quantize(profile: Profile, state: State) -> State:
    """round to 8 bit like the light gets it"""
    return {a: c.from_dmx(c.to_dmx(state.get(a, 0.0))) for a, c in profile.by_attr.items()}


def _lerp(a: State, b: State, t: float) -> State:
    return {k: a.get(k, 0.0) + (b.get(k, 0.0) - a.get(k, 0.0)) * t for k in b}


def blend_to_brightness(profile: Profile, prev: State, proposed: State, target: float) -> State:
    """go towards proposed only until target brightness"""
    b_prev, b_prop = profile.brightness(prev), profile.brightness(proposed)
    if (target - b_prev) * (target - b_prop) > 0:
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
        self.smoothed = rig.zero_states()   # start dark
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

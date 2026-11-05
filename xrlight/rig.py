"""Fixture profiles and the stage patch.

A *state* is what the software wants a fixture to do, as a dict of attribute
values between 0 and 1, for example {"red": 1.0, "dimmer": 0.5}. The rig turns
states into a 512-byte DMX frame (render) and a received frame back into states
(decode). Locked channels are always rendered with their fixed value: the
profile's `lock`, or the fixture's `fixed` value for that attribute if it has one.
Only `follow` and `calibrate` may move a head, by passing `moves` to render(); that
override is limited to the locked pan and tilt channels (MOVE_ATTRS).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

DIMMER = "dimmer"
MOVE_ATTRS = ("pan", "tilt")   # the only locked channels a runtime move may override
UNIVERSE_SIZE = 512

State = dict[str, float]


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


@dataclass(frozen=True)
class Channel:
    offset: int                      # 1-based position inside the fixture
    attr: str                        # "red", "dimmer", "strobe", ...
    lock: int | None = None          # fixed DMX value; locked channels ignore input
    min: int = 0                     # attribute value 0 maps to this DMX value
    max: int = 255                   # attribute value 1 maps to this DMX value
    display: tuple[float, float, float] | None = None   # rough colour for the monitor
    cct: float | None = None         # colour temperature of a white emitter (K)
    note: str = ""

    @property
    def locked(self) -> bool:
        return self.lock is not None

    def to_dmx(self, value: float) -> int:
        if self.lock is not None:
            return self.lock
        return int(round(self.min + clamp01(value) * (self.max - self.min)))

    def from_dmx(self, dmx: int) -> float:
        if self.max == self.min:
            return 0.0
        return clamp01((dmx - self.min) / (self.max - self.min))


@dataclass
class Profile:
    key: str
    name: str
    source: str
    footprint: int
    channels: list[Channel]

    def __post_init__(self) -> None:
        self.by_attr = {c.attr: c for c in self.channels if not c.locked}

    @property
    def free(self) -> list[Channel]:
        return [c for c in self.channels if not c.locked]

    @property
    def colour_attrs(self) -> list[str]:
        return [c.attr for c in self.free if c.attr != DIMMER]

    def has(self, *attrs: str) -> bool:
        return all(a in self.by_attr for a in attrs)

    def zero_state(self) -> State:
        return {a: 0.0 for a in self.by_attr}

    def brightness(self, state: State) -> float:
        """Rough output level 0..1: dimmer times the strongest colour channel."""
        colours = [state.get(a, 0.0) for a in self.colour_attrs]
        level = max(colours) if colours else 1.0
        if DIMMER in self.by_attr:
            level *= state.get(DIMMER, 0.0)
        return level


@dataclass
class Fixture:
    name: str
    profile: Profile
    address: int                     # 1-based DMX start address
    zone: str = ""
    home: State = field(default_factory=dict)
    fixed: dict[str, int] = field(default_factory=dict)   # attr -> DMX value for locked channels

    @property
    def last_address(self) -> int:
        return self.address + self.profile.footprint - 1

    def index(self, channel: Channel) -> int:
        """0-based index of a channel inside the 512-byte universe."""
        return self.address - 1 + channel.offset - 1

    def lock_value(self, channel: Channel) -> int | None:
        """DMX value of a locked channel on this fixture (None for free channels)."""
        if not channel.locked:
            return None
        return self.fixed.get(channel.attr, channel.lock)


@dataclass(frozen=True)
class Zone:
    """Part of the captured picture that drives one fixture in `screen` mode.

    rect = (x0, y0, x1, y1) as fractions 0..1 of the display, y = 0 at the top.
    gain (0..2) multiplies the zone's brightness level, on top of the global --gain.
    """
    fixture: str
    rect: tuple[float, ...]
    gain: float = 1.0


@dataclass
class Screen:
    """Brightness curve for `screen` mode, applied to each zone's luminance L (0..1).

    curve(L) = (max(0, L - black_floor) / (1 - black_floor)) ** gamma
    """
    gamma: float = 2.0
    black_floor: float = 0.08

    def curve(self, lum: float) -> float:
        lifted = max(0.0, lum - self.black_floor) / (1.0 - self.black_floor)
        return min(1.0, lifted) ** self.gamma


@dataclass
class Follow:
    """`xrlight follow`: the moving heads aim at a tracked helmet (OSC from Unreal)."""
    osc_port: int = 9000
    follow_level: float = 0.5          # head brightness while following, 0..1
    max_steps_per_second: float = 30.0  # pan/tilt speed limit, DMX steps per second
    hold_s: float = 0.5                 # no helmet data for this long -> hold position


@dataclass
class Network:
    target_ip: str = "10.55.4.7"
    target_port: int = 6454
    universe: int = 1
    input_universe: int = 1
    listen_ip: str = "0.0.0.0"
    listen_port: int = 6454
    fps: float = 44.0


@dataclass
class Safety:
    max_flashes_per_second: float = 3.0
    flash_threshold: float = 0.10
    smoothing_ms: float = 120.0
    master: float = 1.0
    input_timeout_s: float = 2.0
    passthrough_unpatched: bool = True


def load_profile(path: Path) -> Profile:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    footprint = int(data["footprint"])
    channels = []
    for raw in data["channels"]:
        display = raw.get("display")
        channels.append(Channel(
            offset=int(raw["offset"]),
            attr=str(raw["attr"]),
            lock=None if raw.get("lock") is None else int(raw["lock"]),
            min=int(raw.get("min", 0)),
            max=int(raw.get("max", 255)),
            display=tuple(float(v) for v in display) if display else None,
            cct=None if raw.get("cct") is None else float(raw["cct"]),
            note=str(raw.get("note", "")),
        ))
    profile = Profile(path.stem, data.get("name", path.stem), data.get("source", ""),
                      footprint, sorted(channels, key=lambda c: c.offset))
    _validate_profile(profile)
    return profile


def _validate_profile(p: Profile) -> None:
    offsets = [c.offset for c in p.channels]
    if sorted(offsets) != list(range(1, p.footprint + 1)):
        raise ValueError(f"{p.key}: channels must cover offsets 1..{p.footprint} exactly once")
    free_attrs = [c.attr for c in p.free]
    if len(free_attrs) != len(set(free_attrs)):
        raise ValueError(f"{p.key}: duplicate attribute among unlocked channels")
    for c in p.channels:
        if c.lock is not None and not 0 <= c.lock <= 255:
            raise ValueError(f"{p.key} ch{c.offset}: lock must be 0..255")
        if not 0 <= c.min <= c.max <= 255:
            raise ValueError(f"{p.key} ch{c.offset}: need 0 <= min <= max <= 255")


class Rig:
    def __init__(self, network: Network, safety: Safety, fixtures: list[Fixture],
                 zones: list[Zone] | None = None, screen: Screen | None = None,
                 follow: Follow | None = None, config_dir: Path | None = None):
        self.network = network
        self.safety = safety
        self.fixtures = fixtures
        self.zones = zones or []
        self.screen = screen or Screen()
        self.follow = follow or Follow()
        self.config_dir = config_dir         # folder of stage.toml (calibration lives next to it)

    @classmethod
    def load(cls, path: str | Path) -> "Rig":
        path = Path(path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        profile_dir = path.parent / "profiles"
        profiles: dict[str, Profile] = {}
        fixtures = []
        for raw in data.get("fixture", []):
            key = raw["profile"]
            if key not in profiles:
                profiles[key] = load_profile(profile_dir / f"{key}.toml")
            home = {k: clamp01(float(v)) for k, v in raw.get("home", {}).items()}
            fixed = dict(raw.get("fixed", {}))          # checked in problems()
            fixtures.append(Fixture(raw["name"], profiles[key], int(raw["address"]),
                                    raw.get("zone", ""), home, fixed))
        zones = [Zone(str(raw["fixture"]), tuple(float(v) for v in raw["rect"]),
                      float(raw.get("gain", 1.0)))
                 for raw in data.get("zone", [])]
        screen = {k: float(v) for k, v in data.get("screen", {}).items()}
        rig = cls(Network(**data.get("network", {})), Safety(**data.get("safety", {})),
                  fixtures, zones, Screen(**screen), Follow(**data.get("follow", {})),
                  path.parent)
        errors = rig.problems()
        if errors:
            raise ValueError("stage config problems:\n  " + "\n  ".join(errors))
        return rig

    def problems(self) -> list[str]:
        out = []
        names = [f.name for f in self.fixtures]
        if len(names) != len(set(names)):
            out.append("fixture names must be unique")
        used: dict[int, str] = {}
        for f in self.fixtures:
            if f.address < 1 or f.last_address > UNIVERSE_SIZE:
                out.append(f"{f.name}: addresses {f.address}-{f.last_address} outside 1-512")
            for a in range(f.address, f.last_address + 1):
                if a in used:
                    out.append(f"{f.name} overlaps {used[a]} at address {a}")
                    break
                used[a] = f.name
            for attr in f.home:
                if attr not in f.profile.by_attr:
                    out.append(f"{f.name}: home look uses unknown attribute '{attr}'")
            locked = {c.attr for c in f.profile.channels if c.locked}
            for attr, value in f.fixed.items():
                if attr not in locked:
                    out.append(f"{f.name}: fixed '{attr}' is not a locked channel of "
                               f"{f.profile.key} (locked: {', '.join(sorted(locked)) or 'none'})")
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                    out.append(f"{f.name}: fixed {attr} = {value!r} must be a whole number 0..255")
        zoned: set[str] = set()
        for z in self.zones:
            if z.fixture not in names:
                out.append(f"zone: unknown fixture '{z.fixture}'")
            if z.fixture in zoned:
                out.append(f"zone {z.fixture}: fixture has more than one zone")
            zoned.add(z.fixture)
            if not 0.0 <= z.gain <= 2.0:
                out.append(f"zone {z.fixture}: gain must be between 0 and 2")
            if len(z.rect) != 4:
                out.append(f"zone {z.fixture}: rect needs 4 numbers [x0, y0, x1, y1]")
                continue
            x0, y0, x1, y1 = z.rect
            if not all(0.0 <= v <= 1.0 for v in z.rect):
                out.append(f"zone {z.fixture}: rect values must be between 0 and 1")
            elif x1 <= x0 or y1 <= y0:
                out.append(f"zone {z.fixture}: rect is empty (need x0 < x1 and y0 < y1)")
        if not 0.5 <= self.screen.gamma <= 4.0:
            out.append("screen: gamma must be between 0.5 and 4")
        if not 0.0 <= self.screen.black_floor <= 0.5:
            out.append("screen: black_floor must be between 0 and 0.5")
        fo = self.follow
        if not 1 <= fo.osc_port <= 65535:
            out.append("follow: osc_port must be 1..65535")
        if not 0.0 <= fo.follow_level <= 1.0:
            out.append("follow: follow_level must be between 0 and 1")
        if not 0.0 < fo.max_steps_per_second <= 255.0:
            out.append("follow: max_steps_per_second must be above 0 and at most 255")
        if not 0.0 <= fo.hold_s < self.safety.input_timeout_s:
            out.append("follow: hold_s must be at least 0 and below safety.input_timeout_s")
        return out

    def fixture(self, name: str) -> Fixture:
        for f in self.fixtures:
            if f.name == name:
                return f
        raise KeyError(name)

    def heads(self) -> list[Fixture]:
        """Fixtures whose profile has locked pan and tilt channels."""
        return [f for f in self.fixtures
                if all(any(c.locked and c.attr == a for c in f.profile.channels)
                       for a in MOVE_ATTRS)]

    def zero_states(self) -> dict[str, State]:
        return {f.name: f.profile.zero_state() for f in self.fixtures}

    def home_states(self) -> dict[str, State]:
        states = self.zero_states()
        for f in self.fixtures:
            states[f.name].update(f.home)
        return states

    def render(self, states: dict[str, State], base: bytes | None = None,
               moves: dict[str, dict[str, float]] | None = None) -> bytearray:
        """Build a DMX frame. Channels outside the patch keep their value from `base`.

        `moves` ({fixture: {"pan": dmx, "tilt": dmx}}) replaces the locked value of pan
        and tilt for this frame only. Any other attribute in it is ignored.
        """
        frame = bytearray(UNIVERSE_SIZE)
        if base is not None:
            frame[:min(len(base), UNIVERSE_SIZE)] = base[:UNIVERSE_SIZE]
        moves = moves or {}
        for f in self.fixtures:
            state = states.get(f.name, {})
            move = moves.get(f.name, {})
            for c in f.profile.channels:
                lock = f.lock_value(c)
                if lock is not None and c.attr in MOVE_ATTRS and c.attr in move:
                    lock = min(255, max(0, int(round(move[c.attr]))))
                frame[f.index(c)] = lock if lock is not None else c.to_dmx(state.get(c.attr, 0.0))
        return frame

    def decode(self, frame: bytes) -> dict[str, State]:
        """Read attribute values of every patched fixture from a received frame."""
        padded = bytes(frame[:UNIVERSE_SIZE]).ljust(UNIVERSE_SIZE, b"\x00")
        return {f.name: {c.attr: c.from_dmx(padded[f.index(c)]) for c in f.profile.free}
                for f in self.fixtures}

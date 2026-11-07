"""Colour stuff. Pixels are sRGB, light adds up in linear, so convert first."""
from __future__ import annotations

from .rig import DIMMER, Profile, State, clamp01

Rgb = tuple[float, float, float]

# Linear sRGB / Rec.709 (D65) -> CIE 1931 XYZ
_SRGB_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
D65_XY = (0.3127, 0.3290)


def srgb_to_linear(rgb: Rgb) -> Rgb:
    def f(c: float) -> float:
        c = clamp01(c)
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return tuple(f(c) for c in rgb)  # type: ignore[return-value]


def linear_to_srgb(rgb: Rgb) -> Rgb:
    def f(c: float) -> float:
        c = clamp01(c)
        return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(f(c) for c in rgb)  # type: ignore[return-value]


def rgb_to_xyz(linear_rgb: Rgb) -> tuple[float, float, float]:
    r, g, b = linear_rgb
    return tuple(row[0] * r + row[1] * g + row[2] * b for row in _SRGB_TO_XYZ)  # type: ignore


def xyz_to_xy(xyz: tuple[float, float, float]) -> tuple[float, float]:
    total = sum(xyz)
    if total <= 1e-12:
        return D65_XY
    return xyz[0] / total, xyz[1] / total


def rgb_to_xy(linear_rgb: Rgb) -> tuple[float, float]:
    return xyz_to_xy(rgb_to_xyz(linear_rgb))


def cct_from_xy(x: float, y: float) -> float:
    """colour temperature (McCamy)"""
    n = (x - 0.3320) / (0.1858 - y)
    cct = 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33
    return min(max(cct, 1000.0), 25000.0)


def split_white(r: float, g: float, b: float) -> tuple[float, float, float, float]:
    """put the shared part of rgb into white"""
    w = min(r, g, b)
    return r - w, g - w, b - w, w


def mired_mix(target_k: float, cool_k: float, warm_k: float) -> tuple[float, float]:
    """cool/warm mix in mired. guessing both are equally bright, need to measure"""
    m_target, m_cool, m_warm = 1e6 / target_k, 1e6 / cool_k, 1e6 / warm_k
    t = clamp01((m_target - m_cool) / (m_warm - m_cool))
    return 1.0 - t, t


def colour_to_state(profile: Profile, linear_rgb: Rgb, level: float) -> State:
    """colour + level -> fixture state. white-only lights just get the temperature"""
    state = profile.zero_state()
    peak = max(linear_rgb)
    level = clamp01(level)
    if peak <= 0.0 or level <= 0.0:
        return state
    r, g, b = (c / peak for c in linear_rgb)

    if profile.has("red", "green", "blue"):
        w = 0.0
        if "white" in profile.by_attr:
            r, g, b, w = split_white(r, g, b)
            state["white"] = w
        state.update(red=r, green=g, blue=b)
    else:
        whites = [c for c in profile.free if c.cct]
        if len(whites) >= 2:
            cool = max(whites, key=lambda c: c.cct)
            warm = min(whites, key=lambda c: c.cct)
            cct = cct_from_xy(*rgb_to_xy((r, g, b)))
            state[cool.attr], state[warm.attr] = mired_mix(cct, cool.cct, warm.cct)
            if "amber" in profile.by_attr and cct < warm.cct:
                state["amber"] = clamp01((warm.cct - cct) / (warm.cct - 1800.0))
        elif whites:
            state[whites[0].attr] = 1.0

    colours = [state[a] for a in profile.colour_attrs]
    top = max(colours) if colours else 0.0
    if top > 0:
        for a in profile.colour_attrs:
            state[a] /= top
    if DIMMER in profile.by_attr:
        state[DIMMER] = level
    else:
        for a in profile.colour_attrs:
            state[a] *= level
    return state


def display_colour(profile: Profile, state: State) -> Rgb:
    """roughly what colour it is, for the monitor"""
    r = g = b = 0.0
    for c in profile.free:
        if c.display and c.attr != DIMMER:
            v = state.get(c.attr, 0.0)
            r += v * c.display[0]
            g += v * c.display[1]
            b += v * c.display[2]
    dim = state.get(DIMMER, 1.0) if DIMMER in profile.by_attr else 1.0
    return linear_to_srgb((min(r * dim, 1.0), min(g * dim, 1.0), min(b * dim, 1.0)))

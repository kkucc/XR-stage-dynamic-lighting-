# Screen mode

`xrlight screen` makes the fixtures follow the picture the Mac sends to the LED wall.
That output is just another display on the Mac, so it can be captured like a
screenshot. Each fixture with a `[[zone]]` takes the average colour of its rectangle of
the picture.

## From pixels to a colour

1. **Capture.** A background thread grabs the chosen display with mss at `--fps`
   (default 30). The capture rate is independent of the DMX output rate (44 fps).
2. **Downsample.** The BGRA frame is strided to about 96 px wide (54 rows for a
   1920x1080 picture). Zones are large and averaged anyway, so this costs almost
   nothing in accuracy and keeps the per-frame work small.
3. **Linearise.** Pixels are sRGB-encoded. Light adds up linearly, so the average has to
   be taken on linear values: a zone that is half white and half black should give
   50% linear light, not sRGB 0.5 (which is about 21% linear).
4. **Average per zone.** Mean linear RGB over the zone's rectangle. Rects are fractions
   of the display, `[x0, y0, x1, y1]`, with y = 0 at the top.

## From a colour to a fixture state

The colour and the brightness are handled separately.

**Colour** (`colour_to_state`): the mean colour is divided by its largest component,
so only the hue and saturation remain. Then, per fixture type:

- RGB and RGBW fixtures mix it directly. On RGBW the common part of R, G and B moves to
  the white channel.
- The white-only side pars cannot show a hue. They reproduce the colour temperature
  instead: CCT is estimated from the chromaticity (McCamy's approximation), and the cool
  and warm emitters are mixed in mired between 6000 K and 3000 K. Below 3000 K the amber
  channel is added. This assumes both whites are equally bright, which is not measured.

**Level**: the brightness of a zone is

```
level = gain × zone_gain × curve(L)
curve(L) = (max(0, L − black_floor) / (1 − black_floor)) ** gamma
```

where `L` is the luminance (Y, Rec.709 weights) of the zone's mean linear colour,
encoded back to sRGB so it is roughly perceptual. `gain` is `--gain` (0–2),
`zone_gain` is the zone's own `gain` (0–2), and `gamma` and `black_floor` come from the
`[screen]` section. The result is clipped to 1. On fixtures with a dimmer the level goes
to the dimmer; on fixtures without one (the Astera pixels) the colour channels are scaled
by it.

## Why there is a curve

Without it, the level was simply `L`. Dark scenes on the wall are rarely pure black, and
because `L` is sRGB-encoded, a dark zone still has a sizeable value: 1% linear light is
already L = 0.10. With a linear curve that drove the side pars at a visible level in
scenes that read as dark on the wall. The floor cuts the darkest part off completely and
the gamma keeps mid tones low, while full white still gives full output.

With the defaults (gamma 2.0, black_floor 0.08):

| Zone luminance (linear) | L (sRGB) | gamma 1 | gamma 2 (default) | gamma 3 |
|---|---|---|---|---|
| 0.5% | 0.06 | 0 | 0 | 0 |
| 1% | 0.10 | 0.02 | 0.00 | 0.00 |
| 5% | 0.25 | 0.18 | 0.03 | 0.01 |
| 18% (mid grey) | 0.46 | 0.42 | 0.17 | 0.07 |
| 50% | 0.74 | 0.71 | 0.51 | 0.36 |
| 100% | 1.00 | 1.00 | 1.00 | 1.00 |

Raise `black_floor` if dark scenes still glow; lower `gamma` if mid-tone scenes look too
dark. Both are read at start-up, so tuning on the stage means: edit `stage.toml`, stop
`screen` with Ctrl+C, start it again. `check` prints the values in use.

## Zone gain

The four white side pars sit closer to the talent and read brighter than the front pars
at the same level, so their zones have `gain = 0.5`. This is the side-fill ratio, judged
by eye, not measured.

## Delay

The wall shows the picture later than the Mac does: capture card, OBS and the LED
processor each add latency. `--delay-ms` holds the zone colours back by that amount.
Each capture result is stored with the time it was grabbed; every output frame uses the
newest result at least `delay` old. Until the buffer has such a result, fixtures show
their home look. The delay has to be found by eye: raise it until a hard cut on the wall
and the change in the lights happen together.

## After the colour

The states go through the same pipeline as every other mode: master level, 120 ms
smoothing, then the flash guard. A scene that cuts between bright and dark faster than
three times a second is held back by the guard, not passed through. See
[safety.md](safety.md).

## Limits of this approach

- Front lights in a real location would carry what is *behind* the camera, which the
  wall does not show. Mapping wall zones onto fixtures is a first approximation; an
  Unreal SceneCapture of the virtual surroundings would be the better source.
- Colour is not calibrated. A fixture's RGB channels are treated as the sRGB primaries,
  which they are not. Matching the wall properly needs per-fixture measurements.
- The average of a zone loses detail: a small bright object in a dark zone barely changes
  the level, by design.
- macOS needs Screen Recording permission for the terminal app; without it `screen` stops
  with an explanation instead of capturing a black frame.

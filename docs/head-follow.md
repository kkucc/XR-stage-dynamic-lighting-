# Head follow

The two moving heads can follow a tracked helmet. Unreal, on the render PC, sends the
helmet position to the Mac over OSC; `xrlight follow` turns it into pan and tilt.

## Input

OSC over UDP, port 9000 (`[follow] osc_port`), address `/xr/helmet`, three floats
`x y z` in centimetres, stage-local. Verified on the stage, Nov 2026: Unreal sends it and
it arrives on the Mac at about 50 messages per second. Only int32 and
float32 arguments are parsed; bundles are unpacked. Only the floor position (x, y) is
used. Height is ignored, which is fine as long as the helmet stays at standing height.

Check the link before anything moves:

```bash
python -m xrlight helmet
```

It prints the position, message rate and sender once per second and sends no DMX.

## Why teaching instead of geometry

Aiming from geometry would need each head's mounting position, orientation, pan/tilt
ranges in degrees, and the tracking system's origin and axes, all measured in the same
frame. None of that is documented for the stage, and errors in any of it show up as
the beam missing the person. Teaching skips all of it: put the helmet on a spot, aim the
head at it by hand, record the pair. Whatever the mounting and the coordinate system
are, the recorded points already include them.

The cost is that the head is only accurate near the taught area, and in between it is
only as good as linear interpolation. With 6–9 points spread over the walking area,
the error between points is small compared to the beam width.

## Calibrating

```bash
python -m xrlight calibrate --head "Head left"
```

The head gets steady white light at 40% and starts at its `fixed` position (or the last
point of an existing calibration). All other fixtures show their home look. Keys:

| Key | Action |
|---|---|
| ← → | pan −1 / +1 DMX step (Shift: 5) |
| ↑ ↓ | tilt +1 / −1 (Shift: 5) |
| Enter | record a point: the latest helmet (x, y) with the current pan/tilt |
| u | undo the last point |
| s | save and quit |
| q | quit without saving |

Which way the head turns for "+" depends on how it is hung; it does not matter for the
calibration. The head moves at most 5 DMX steps per frame towards the keyed position.
Enter is refused when there was no helmet message in the last 0.5 s, or while the head
is still on its way to the keyed position. Pressing Enter again within 20 cm of an
earlier point replaces that point, so correcting the aim at one spot is just: nudge,
Enter, nudge, Enter. The last aim wins. The same rule is applied when a file is loaded,
because the first calibrations on the stage had up to five points recorded at the exact
same position with different pan values; Delaunay can use only one of them, and the
wrong ones also widened the clamp range (Head left: pan 107–255 instead of 147–226).

Points are saved to `config/calibration/<head name>.toml`. An existing file is loaded
first, so a second session adds to it; delete the file to start over. The format is
plain TOML and can be edited by hand:

```toml
head = "Head left"
points = [
  { x = -180.0, y = 95.5, pan = 151, tilt = 72 },
  ...
]
```

How to place the points:

- 6–9 points, spread over the whole area where people walk, including its edges and
  corners. Outside the taught area the head stops at the edge (see below), so an area
  that is not taught is an area the head will not follow into.
- Aim at the chest of the person wearing the helmet, never at the face. The calibration
  is the only place the aim is decided; `follow` reproduces it.
- Points must not all lie on one line, and each head needs at least 3.
- Repeat for the other head. The heads hang on opposite sides, so their points differ.

## Following

```bash
python -m xrlight follow
```

For each head with a calibration file:

1. **Interpolate.** The taught (x, y) points are triangulated (Delaunay). Inside a
   triangle, pan and tilt are blended linearly from its three corners. Outside the
   taught area, the nearest point on its boundary is used, so the head waits at the edge
   instead of extrapolating.
2. **Clamp** to the lowest and highest pan and tilt among that head's points.
3. **Smooth** with the same time constant as the light (`smoothing_ms`, 120 ms).
4. **Rate-limit** to `max_steps_per_second` (30 DMX steps/s) on each axis.

The head's light is white at `follow_level` (0.5), through the normal pipeline
(master, smoothing, flash guard). Heads without a calibration, and every other fixture,
show their home look. `--head NAME` limits following to that head (repeat for more);
`--colour` sets the light colour as sRGB hex (default `ffffff`). With `--others-off`
everything that is not following is kept dark instead, so only the
followed heads give light; the switch to dark goes through the same smoothing, so it is a
short fade, not a cut. On exit everything still fades to the home look.

Losing the helmet:

| Last helmet message | Heads do |
|---|---|
| less than `hold_s` (0.5 s) ago | follow |
| between `hold_s` and `safety.input_timeout_s` (2 s) | hold position, light stays on |
| longer, or never | move back to their `fixed` position, light fades to the home look |

When the helmet comes back, following resumes from wherever the heads are, at the same
speed limit. On exit (Ctrl+C or `--seconds`) the light fades home and the heads return
to their `fixed` position at the speed limit; xrlight waits for them (up to 15 s) before
it stops sending.

Once per second `follow` prints the helmet position, pan/tilt per head, the OSC rate
and the mode (follow, hold, home).

For example, only the left head, in deep blue, with the rest of the stage dark:

```bash
python -m xrlight follow --head "Head left" --others-off --colour 1a2cff
```

On an RGB head a blue like this is almost only the blue emitter, so it looks darker than
white at the same `follow_level`; raise `follow_level` in `[follow]` if needed.

## Testing at home

Three terminals, nothing leaves the Mac:

```bash
python -m xrlight monitor --port 6455
python -m xrlight fake-helmet --target 127.0.0.1:9000
python -m xrlight follow --target 127.0.0.1:6455
```

`fake-helmet` walks a circle of radius 150 cm around (0, 0), 30 s per lap, 50 messages
per second (`--radius`, `--period`, `--rate`). `follow` needs a calibration file; for a
home test, make one by hand with points around (0, 0), or run `calibrate` with
`--target 127.0.0.1:6455` while `fake-helmet` runs and record a few points as the circle
passes. Stop `fake-helmet` to see hold, then home after 2 s. If Unreal is also sending
to the Mac, both streams arrive on port 9000 and the position jumps between them; `helmet`
shows the rate (about 50/s from one source). In the monitor, Pan and
Tilt of a following head are marked `!`, because they differ from the locked value;
that is expected in this mode only.

## Safety

- pan and tilt remain locked channels. `follow` and `calibrate` move them with a
  per-frame override in `Rig.render`, which is hard-limited to the two attributes in
  `MOVE_ATTRS` (`pan`, `tilt`). Shutter, strobe, reset, macro and speed cannot be
  changed this way; the tests check that every other locked channel keeps its value
  during a 20 s follow run, even when the override asks for them.
- Every other mode (`test`, `proxy`, `screen`) leaves the heads at their `fixed`
  position. The proxy does not forward pan/tilt from Unreal.
- The head can only point within the range of pan/tilt values that were aimed by hand.
  Aim every point at chest height; a point aimed too high raises the whole area around
  it.
- 30 DMX steps per second is a speed limit on the DMX value, not on the head: how many
  degrees that is depends on the head's pan/tilt range. Lower `max_steps_per_second` if
  the movement looks too fast with people on stage.
- The heads move. Before `calibrate` or `follow`, check that nobody is on a ladder or
  working near the truss.
- Speed (ch8 on Head right, probably ch3 on Head left) is locked at 0, which on most
  heads means fastest internal movement. The software rate limit is what keeps it slow.

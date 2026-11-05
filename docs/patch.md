# Stage patch

Art-Net universe 1, ENTTEC ODE Mk2 at 10.55.4.7. Left and right are as seen from the
brain bar desk, which faces the LED wall. `python -m xrlight check` prints the same
patch with every channel.

| DMX | Fixture | Profile | QLC+ name | Screen zone |
|---|---|---|---|---|
| 1–7 | Right back | `white_par_7ch` | white right #1 | right half, gain 0.5 |
| 8–14 | Right front | `white_par_7ch` | white right #2 | right half, gain 0.5 |
| 15–22 | Front Right | `elation_opti_quad_par_8ch` | rgb front #1 | right third |
| 23–30 | Front Middle | `rgbw_par_8ch` | rgb front #2 | middle third |
| 31–38 | Front Left | `rgbw_par_8ch` | rgb front #3 | left third |
| 39–45 | Left front | `white_par_7ch` | white left #1 | left half, gain 0.5 |
| 46–52 | Left back | `white_par_7ch` | white left #2 | left half, gain 0.5 |
| 53–65 | Orbiter | `arri_orbiter_13ch` | Orbiter | centre |
| 66–74 | Head left | `moving_head_left_9ch` | 7x10W Mini Moving Head | top-left quarter |
| 77–85 | Head right | `miniwash_710_9ch` | 7x10W Mini Moving Head | top-right quarter |
| 112–135 | Tube A 1–8 | `astera_helios_rgb_pixel` | — | left edge, 8 slices |
| 136–159 | Tube B 1–8 | `astera_helios_rgb_pixel` | — | right edge, 8 slices |

75–76 and 86–111 are unpatched. The Orbiter is in the patch but not on the
stage at the moment. The fixtures are listed in `stage.toml` in physical order around
the stage (right to left), which is the order `identify` lights them in.

## Per fixture type

**White side pars** — Elation Opti Tri Par White, 7 ch. Cool white, warm white, amber,
colour temperature presets, strobe, dimmer, dimming curve. Layout from the QLC+ fixture
panel. Strobe (ch5), presets (ch4) and curve (ch7) are locked at 0. The 6000 K / 3000 K
values for the two white emitters are placeholders until they are measured.

**Front Right** — Elation Opti Quad Par DMX8: R, G, B, W, macro, strobe, colour change,
dimmer on ch8. A different model from the other two front pars, which is why its dimmer
is on ch8.

**Front Middle, Front Left** — RGBW par, 8 ch, dimmer on ch7. Layout from the
VP-Template Unreal library; the dimmer position was confirmed live (QLC+ sends it on
DMX 29 and 37). Ch6 is labelled "Burst" in the library and treated as a strobe (locked).
Ch5 and ch8 are unnamed and locked at 0. The model name is still unknown.

**Orbiter** — ARRI Orbiter in "RGBAC Mode 13": dimmer, R, G, B, amber, cyan, lime,
white, then CCT, green/magenta, zoom, focus and a reserved channel, all locked at 0 so
the software never touches the optic. The Unreal library still has it at address 200
with 10 channels; QLC+ has 53 with 13, and QLC+ is used.

**Head right** — Miniwash 710 layout (9 ch), verified live: pan, tilt, dimmer, R, G, B,
W, speed, reset. The dimmer is limited to DMX 0–134 because 135–239 is the strobe range
on the same channel. Reset is locked at 0 (150–200 resets the head).

**Head left** — QLC+ patches it as the same fixture as Head right, but the head itself
does not follow that layout; it is probably set to a different mode in its own menu.
Tested in QLC+ Simple Desk: ch4–6 R, G, B, ch8 shutter/strobe, ch9 dimmer (smooth from
about 30). There is no white emitter; white is mixed from RGB. Ch8 gives steady light at
15–20 and strobes from 60 up, so it is locked at 18.

Both heads have pan and tilt locked. Their position comes from `fixed = { pan = 168,
tilt = 68 }` in `stage.toml`, found in QLC+ Simple Desk. The two heads use different
profiles and hang on opposite sides, so the same numbers are not guaranteed to point them
symmetrically.

**Astera Helios tubes** — both tubes run Astera profile 42 ("RGB RGB", 8 pixels, strobe
off), 24 channels each. Tube A started on profile 18 (2 pixels, at 118) and was switched
to 42 at 112 to match Tube B. Each pixel is three channels R, G, B and is patched as its
own fixture, so each pixel gets its own zone (an eighth of the screen height) and its
own flash guard. There is no dimmer: brightness is carried by the RGB values. Verified
with `identify` on the stage: pixel 1 is the top end of both tubes; Tube A stands at the
left pole under Head left, Tube B at the right pole under Head right.

## Confidence

| What | Status | Source |
|---|---|---|
| ODE Mk2 at 10.55.4.7, 44 fps max | documented | XR Stage wiki, DMX pages |
| Art-Net universe 1 | confirmed | QLC+ output settings |
| Addresses of pars, Orbiter, heads | from QLC+ | `XRStage_default.qxw`, Nov 2026 |
| White par and Front Right layouts | from QLC+ | fixture panel |
| RGBW par layout | library + live dimmer check | VP-Template, QLC+ |
| RGBW par ch6 = strobe | assumed | "Burst" label |
| White par CCTs (6000/3000 K) | placeholder | needs measurement |
| Head right layout | verified live | Miniwash 710 manual, test |
| Head right speed/reset channels | not tested | manual only |
| Head left layout | verified live | QLC+ Simple Desk |
| Head pan/tilt aim 168/68 | set on stage | QLC+ Simple Desk |
| Tube profiles and addresses | config | Astera DMX profiles V2 |
| Tube orientation and position | verified live | `identify`, Nov 2026 |
| Home look (warm white, dimmer 60%) | approximation | QLC+ live look |

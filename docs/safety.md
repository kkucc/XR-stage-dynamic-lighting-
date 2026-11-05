# Safety

The stage has people on it and a camera pointed at them, and some of the fixtures can
strobe. The software is built so that nothing upstream (a pattern, Unreal through the
proxy, the screen picture) can make a fixture strobe or flash fast. The layers below run
every frame, per fixture, in this order.

## 1. Locked channels

Every channel in a profile is either free (follows the state) or locked at a fixed DMX
value. Strobe, shutter, macro, mode, reset, optics and anything unidentified are locked.
`Rig.render` writes locked values unconditionally: they are not part of a state, so no
source can set them. The proxy overwrites them even with passthrough on. `monitor` marks
a locked channel with `!` when an incoming frame has it at a different value, which is a
quick way to see what Unreal or QLC+ is trying to send.

A fixture can override the value of its own locked channels with `fixed` in
`stage.toml`. This is how the heads are aimed (pan 168, tilt 68). The channel stays
locked: the value is still constant and nothing upstream can change it.

The one exception is head follow. `follow` and `calibrate` pass a per-frame `moves`
override to `Rig.render`, and render applies it only to channels named in `MOVE_ATTRS`
(`pan`, `tilt`). Anything else in the override is ignored, so shutter, strobe, reset,
macro and speed stay locked even in that mode. Movement is rate-limited and clamped to
the hand-taught range; see [head-follow.md](head-follow.md).

## 2. Range limits

A free channel maps 0..1 onto its own `min..max`. This matters for Head right, whose
dimmer channel is a strobe from DMX 135 up: its range is 0–134, so even full brightness
never reaches the strobe range.

## 3. Master level

`safety.master` (0–1) scales the dimmer, or the colour channels on fixtures without a
dimmer.

## 4. Smoothing

An exponential filter with time constant `smoothing_ms` (120 ms). At 44 fps that is a
step of about 17% of the remaining distance per frame, so a hard cut reaches 63% after
120 ms and 95% after about 360 ms. It removes jitter from capture noise and softens cuts,
but on its own it does not stop a slow strobe.

## 5. Flash guard

The limit is at most `max_flashes_per_second` (3) flashes in any 1-second window, which
follows the common photosensitivity guidance. A flash is a pair of opposite brightness
changes of at least `flash_threshold` (10%).

Brightness per fixture is estimated as dimmer × the strongest colour channel. The guard
tracks turning points with hysteresis: a change only counts once brightness has moved at
least the threshold away from the last peak or trough. Small dips in between therefore do
not reset anything, and slow drifts do not count at all. The guard keeps the times of the
last transitions; once six (2 × 3) fall within one second, further transitions are
refused and brightness is held inside a band of 85% of the threshold around the last
peak or trough until the oldest transition leaves the window.

When the guard holds a fixture back, the output is moved from the previous state towards
the requested one only as far as the allowed brightness, so the colour moves part of the
way instead of freezing.

The guard works on the values after 8-bit rounding, i.e. on what the fixture actually
receives. Checking the unrounded values would let rounding push a change just over the
threshold without the guard counting it.

`test --pattern flash-test` requests 10 Hz full white flashes; with the guard the output
stays at 3 flashes per second or fewer. The tests check this with an independent counter,
including with jittered frame timing.

## 6. Fail-safe

- Every command fades to the home look over 1.5 s when it stops.
- The proxy fades home when no input arrives for 2 s.
- A config problem (overlapping addresses, an unknown attribute, a value out of range)
  stops the program before anything is sent.

## Known gaps

These are not handled, and are worth knowing before running anything new on the stage.

- **Brightness is estimated, not measured.** Dimmer × strongest channel is not luminance,
  so the 10% threshold is approximate, and it differs between fixture types.
- **Per fixture, not per field of view.** Each fixture (each tube pixel) has its own
  guard. Many fixtures flashing out of phase, each within its own limit, are not
  detected as a combined pattern.
- **No red flash rule.** The guidance has a separate, stricter rule for saturated red
  flashes. Not implemented.
- **Strobe at a constant value.** Some fixtures strobe internally at a steady DMX value
  (Head left ch8 from 60, Head right dimmer 135–239). The flash guard cannot see this;
  only locking and range limits protect against it.
- **`fixed` accepts any locked channel.** It is meant for pan and tilt, but
  `fixed = { shutter = 200 }` on Head left would pass validation and strobe the head.
  `check` shows every fixed value next to the profile's lock, so look at it after editing.
- **Head follow trusts the calibration.** The heads can point anywhere within the
  taught pan/tilt range. A point aimed at someone's face makes the area around it aim at
  faces too. Nothing checks where the beam lands.
- **`test --raw` has no safety layer at all.** It exists to fake an unsafe source for
  testing the proxy on localhost. Nothing stops it from being sent to the stage's IP.

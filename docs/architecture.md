# Architecture

xrlight is a single Python package. On Python 3.11+ it only needs the standard library,
except `screen` (numpy, mss) and `calibrate`/`follow` (numpy, scipy). It sends Art-Net (ArtDmx,
one universe) to the ENTTEC ODE Mk2 at 10.55.4.7.

## Modules

| Module | Responsibility |
|---|---|
| `rig.py` | Loads `stage.toml` and the profiles, validates them, renders states to a 512-byte DMX frame and decodes frames back to states |
| `color.py` | sRGB/linear conversion, XYZ, CCT, and `colour_to_state`: how one colour is approximated by a given fixture |
| `safety.py` | `Pipeline`: master level, smoothing and the flash guard, applied every frame |
| `patterns.py` | Test patterns (`identify`, `palette`, `home`, `flash-test`) as functions of time |
| `runner.py` | The loops for `test`, `proxy` and `monitor`, plus the fade home on exit |
| `screen.py` | Display capture thread, zone averaging, brightness curve, delay buffer, the `screen` loop |
| `osc.py` | Minimal OSC parsing and sending; the helmet receiver; `helmet` and `fake-helmet` |
| `head.py` | Head calibration files, interpolation, rate-limited pan/tilt, the `calibrate` and `follow` loops |
| `artnet.py` | Building and parsing ArtDmx packets, one UDP socket |
| `cli.py` | Argument parsing and `check` |

## States

Everything above the DMX layer works with *states*: a dict per fixture of attribute
values between 0 and 1, for example `{"red": 1.0, "green": 0.2, "dimmer": 0.6}`. Only
unlocked channels appear in a state. The rig is the only place that knows DMX
addresses and byte values, so patterns, colour maths and safety code never deal with
channel numbers.

## One frame

All modes run at the config fps (44, the ODE Mk2 maximum) and share the same path:

```
source            ->  targets      ->  Pipeline.step  ->  Rig.render  ->  ArtDmx
(pattern, proxy       (states,         (master,          (512 bytes,
 input, screen)        0..1)            smoothing,        locked channels
                                        flash guard)      forced)
```

1. The source produces target states. `test` calls a pattern function with the time;
   `proxy` decodes the last received Art-Net frame; `screen` turns zone colours into
   states with `colour_to_state`.
2. `Pipeline.step` limits and smooths them (see [safety.md](safety.md)).
3. `Rig.render` writes each channel: locked channels get their locked value no matter
   what the state says; free channels map 0..1 onto the channel's `min..max`. In
   `follow` and `calibrate` only, a `moves` argument replaces the locked value of pan
   and tilt (and nothing else) for that frame.
4. The frame goes out as one ArtDmx packet with an incrementing sequence number.

The loop timing uses `perf_counter` with a fixed tick, and resyncs instead of
bursting if it falls behind (after a sleep or a debugger stop).

On exit (Ctrl+C, `--seconds`, or an error in the loop) every mode that sends, except
`test --raw`, fades to the home look over 1.5 s through the same pipeline.

## Proxy

`proxy` sits between Unreal (or QLC+) and the ODE. It listens on `listen_port` for
`input_universe`, decodes the patched fixtures' free channels, runs them through the
pipeline and forwards the result. With `passthrough_unpatched = true`, channels that are
not in the patch are copied from the input unchanged; patched locked channels are always
overwritten. If no input arrives for `input_timeout_s` (2 s) it fades to the home look
and goes back to following input when packets return.

Only one program can bind UDP 6454 on the Mac, so QLC+ and `proxy`/`monitor` cannot run
on that port at the same time.

## Screen mode threads

Capture runs in its own thread because a grab can take longer than one output frame.
The thread keeps only the latest result `(sequence, capture time, zone colours)` behind
a lock. The main loop takes new results, pushes them into the delay buffer with their
capture time, and reads the value from `--delay-ms` ago. Details in
[screen-mode.md](screen-mode.md).

## Head follow

`follow` is a fourth source next to patterns, proxy and screen. The OSC socket is
polled in the main loop (no thread: a helmet message is a few bytes and the loop runs
at 44 fps anyway). Each frame the controller produces light targets for the pipeline
and pan/tilt moves for `render`. See [head-follow.md](head-follow.md).

## Tests

`pytest` runs without network hardware or a display: Art-Net tests use loopback sockets,
screen tests use synthetic numpy frames. The tests load the real `config/stage.toml`, so
an overlapping address, or a change to a channel the tests cover (the strobe channels,
the heads' dimmer limits, the tube pixels), fails the suite.

# XR Reactive Light

Project for reactive lighting on the XR Stage. It speaks Art-Net to the
stage's ENTTEC ODE Mk2 and the fixtures.

Patch and channel layouts come from the `Stage_DMX_Library` in
[VP-Template](https://github.com/BredaUniversityResearch/VP-Template) 

## Setup (macOS/linux)

Python 3.11+ 

```bash
cd xr-reactive-light
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          
python -m xrlight check      # prints the patch, locked channels and notes
```

I am connection so far via Ethernet cable, trying to think how to make with out it.

## Commands
To test connection 
```bash
python -m xrlight monitor --port 6454  
```
First test command thats ignight all lisght in order in loop
```bash
python -m xrlight test --pattern identify --level 0.8
```
```bash
.venv/bin/python -m xrlight screen --list-monitors
.venv/bin/python -m xrlight screen --monitor 2
```

## Testing at home

**Patterns in the terminal** — two terminal windows:

```bash
python -m xrlight monitor --port 6455
python -m xrlight test --pattern palette --target 127.0.0.1:6455
```

**The proxy and its safety layer** — three windows. The raw source plays the role of
Unreal sending a 10 Hz strobe; the monitor shows what would reach the fixtures:

```bash
python -m xrlight monitor --port 6455
python -m xrlight proxy --listen-port 6456 --target 127.0.0.1:6455
python -m xrlight test --pattern flash-test --raw --target 127.0.0.1:6456
```

The proxy prints how many fixture-frames the flash guard held back. Stop the raw
source and after 2 s the proxy fades to the home look on its own.

**QLC+ (optional)** — in Inputs/Outputs, give Universe 1 an Art-Net input, enable
Passthrough, and open the DMX Monitor. Send with `--target 127.0.0.1`. Close QLC+
before running `proxy` or `monitor` on port 6454: only one program can listen there.

**ESP32 + WLED** — set WLED's sync interface to Art-Net, universe 1, and send with
`--target <esp-ip>`. Good for testing smoothing and latency with real light.

## On the stage

1. Connect the Mac to the 10.55.4.x network (USB-C Ethernet) and check you got an
   address in 10.55.4.1–10.55.7.254.
2. `python -m xrlight check`, then `test --pattern identify` at low level and watch that
   each named fixture lights up. Make sure Unreal/QLC+ is **not** sending at the same
   time — two sources on one universe make the lights flicker.
3. For the proxy: set Unreal's DMX output port *Destination IP* from 10.55.4.7 to the
   Mac's IP, then run `python -m xrlight proxy`. To undo, set it back to 10.55.4.7.
4. Run long sessions with `caffeinate -i python -m xrlight proxy` so the Mac does not
   throttle the process.
5. If the lights stop responding entirely, the ODE Mk2 has probably frozen: power-cycle
   it (barrel plug, rack behind the screen), as described in the DMX Troubleshooting page.

## Video mode on the stage

The lights follow what the Mac sends to the LED wall. Each fixture with a `[[zone]]` in
`config/stage.toml` takes the average colour of its part of the picture; the rest keep
their home look.

1. Signal path, as in the wiki page "Hooking up HDMI": Mac → HDMI → capture card IN on
   the Recording PC → OBS (source: Blackmagic device, HDMI) → Fullscreen Projector on
   the HELIOS display. Send 1920x1080 from the Mac.
2. In QLC+, untick **Output** for the universe so it does not fight xrlight for the lights.
3. Find the wall output and start:
   ```bash
   python -m xrlight screen --list-monitors
   python -m xrlight screen --monitor N
   ```
   Options: `--gain` (0..2), `--fps` (capture rate, default 30; DMX output stays at the
   config fps), `--target`, `--seconds`. Every second it prints the capture fps, output
   fps and how many frames the flash guard held back. Ctrl+C fades to the home look.
4. The wall shows the picture later than the Mac does (capture card, OBS, processor).
   Raise `--delay-ms` until the lights and the wall change together.
5. macOS asks for **Screen Recording** permission (System Settings → Privacy & Security →
   Screen Recording) for the terminal app you run from. Switch it on and reopen the app;
   without it `screen` stops with a message.

Known limitation: in reality the front lights would carry what is *behind* the camera,
which the wall does not show. Mapping wall zones straight onto the fixtures is a first
approximation; an Unreal SceneCapture of the virtual surroundings is the better source later.


## Safety model

Every frame, for every fixture:

1. **Locked channels** (strobe, mode, unnamed) always send their fixed value. Nothing in
   the software can drive them.
2. **Range limits**: each channel maps 0..1 into its own `min`..`max` DMX range.
3. **Master level** caps overall brightness (`safety.master`).
4. **Smoothing**: exponential filter with `smoothing_ms` time constant.
5. **Flash guard**: at most `max_flashes_per_second` (3) flashes in any 1 s window. A flash
   is two opposite brightness swings of at least `flash_threshold` (10%), tracked between
   peaks and troughs on the 8-bit values the fixtures actually receive. When the budget is
   used up, brightness is kept within a band around the last peak/trough.
6. **Fail-safe**: the proxy fades to the home look when input stops for
   `input_timeout_s`, and every command fades home on exit.

The brightness estimate is `dimmer × strongest colour channel`, not measured luminance,
so the flash limit is an approximation of the photosensitivity guidance, not a
certification. The guidance also has a separate rule for saturated red flashes, which is
not implemented yet.


## Next steps

- Audio input (`sounddevice` + NumPy FFT, beat detection with `aubio`) as a new source.
- Scene sampling from an Unreal SceneCapture instead of the wall picture (see Video mode).
- Orbiter in its x/y 16-bit mode (with permission) and an `arri_orbiter_xy` profile.
- Calibration: measure each fixture's real colour per channel and replace the guesses.
- Measurements for the report: ΔE between wall and light, latency, setup time vs manual.


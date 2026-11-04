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

## Verified vs assumed

| Item | Status | Source |
|---|---|---|
| ODE Mk2 at 10.55.4.7, Art-Net unicast, 44 fps max | wiki | DMX / Unreal DMX Setup pages |
| Patch: 7 fixed pars on 1–52 | wiki + library | DMX page, Stage_DMX_Library |
| Orbiter on 200–209 | library only | Stage_DMX_Library — **verify** |
| Art-Net universe 1 | template config | `ExternUniverseStart=1` in VP-Template — **verify** |
| White par ch1/ch2 = cool/warm white, CCT 6000/3000 K | guess | **measure** |
| RGBW par ch6 "Burst" = strobe (locked) | guess | **verify** |
| Front Right has Mode/Dimmer swapped | library | **verify** (`rgbw_par_8ch_alt`) |
| Orbiter channel meaning | library | compare with the fixture's DMX mode |
| Home look | placeholder | ask the stage team |

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
- Wall/scene sampling: zones from an Unreal render target or from frames on the Mac.
- Orbiter in its x/y 16-bit mode (with permission) and an `arri_orbiter_xy` profile.
- Calibration: measure each fixture's real colour per channel and replace the guesses.
- Measurements for the report: ΔE between wall and light, latency, setup time vs manual.

## Notes

The wiki export contains the stage PC password — do not commit those PDFs to Git or quote
them in documentation.

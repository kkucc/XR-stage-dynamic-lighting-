# XR Reactive Light

Starter project for reactive lighting on the BUas XR Stage. It speaks Art-Net to the
stage's ENTTEC ODE Mk2, knows the stage patch, and puts a safety layer between any
source (test patterns, Unreal, later audio and wall sampling) and the fixtures.

Status: **starter**. The patch comes from the stage's QLC+ project
(`C:\QLC+\projects\XRStage_default.qxw`), checked live from a Mac on the stage
network. Channel layouts come from QLC+, the fixture manuals and the `Stage_DMX_Library`
in [VP-Template](https://github.com/BredaUniversityResearch/VP-Template), which turned
out to be outdated (see *Verified vs assumed*).

## Setup (macOS)

Python 3.11 or newer needs no extra packages to run. Tests need pytest.

```bash
cd xr-reactive-light
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # 24 tests, ~6 s
python -m xrlight check      # prints the patch, locked channels and notes
```

The first time a command opens the network, macOS may ask for *Local Network* access
for Terminal / VS Code, and the firewall may ask to allow incoming connections. Allow
both, otherwise packets silently go nowhere.

## Commands

| Command | What it does |
|---|---|
| `python -m xrlight check` | Validate `config/stage.toml` and print the patch |
| `python -m xrlight test --pattern identify` | Light one fixture at a time at 30% (checks the patch) |
| `python -m xrlight test --pattern palette` | Cycle scene colours (daylight, sunset, night, forest) |
| `python -m xrlight test --pattern flash-test` | Request a 10 Hz strobe — the safety layer slows it down |
| `python -m xrlight test --pattern home` | Send the home (fallback) look |
| `python -m xrlight proxy` | Receive Art-Net from Unreal, make it safe, forward to the ODE |
| `python -m xrlight monitor` | Show incoming Art-Net as coloured fixtures in the terminal |

Useful flags: `--target IP[:PORT]` (default: the ODE from the config), `--seconds N`,
`--raw` for `test` (skip the safety layer, to fake an unsafe source),
`--listen-port` for `proxy`, `--port` for `monitor` (default 6455).
Ctrl+C always fades back to the home look before stopping.

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

1. Connect the Mac to the 10.55.4.x network (USB-C Ethernet, DHCP) and check you got an
   address in 10.55.4.1–10.55.7.254. `ping 10.55.4.7` should answer in under 1 ms.
2. **Listen first**: QLC+ on the Recording PC broadcasts to 10.55.7.255, so
   `python -m xrlight monitor --port 6454` shows what it sends without sending anything.
3. **Send**: untick *Output* for the ArtNet line in QLC+ (Inputs/Outputs), make sure
   Unreal is not sending either — two sources on one universe make the lights flicker —
   then `python -m xrlight test --pattern identify`. Tick *Output* again afterwards.
4. **Proxy with QLC+ as the source** (no Unreal needed): in QLC+ Inputs/Outputs, open the
   ArtNet configuration and change the *Outputs* IP address from `10.55.7.255` to the
   Mac's IP. Run `python -m xrlight proxy` and play QLC+ scenes or the RGBMatrix effects:
   everything now passes through the safety layer. Put the IP back to `10.55.7.255`
   afterwards and do not save the QLC+ project with your change.
5. **Proxy with Unreal**: set Unreal's DMX output port *Destination IP* from 10.55.4.7 to
   the Mac's IP, then run the proxy. To undo, set it back to 10.55.4.7.
6. Run long sessions with `caffeinate -i python -m xrlight proxy` so the Mac does not
   throttle the process.
7. If the lights stop responding entirely, the ODE Mk2 has probably frozen: power-cycle
   it (barrel plug, rack behind the screen), as described in the DMX Troubleshooting page.

## Verified vs assumed

| Item | Status | Source |
|---|---|---|
| ODE Mk2 at 10.55.4.7, 44 fps max | verified | wiki, QLC+ Nodes Tree, ping |
| Art-Net universe 1 | verified | QLC+ ArtNet output configuration |
| Patch 1–85 (7 pars, Orbiter, 2 heads) | verified | QLC+ fixture list, `identify` test |
| White pars = Elation Opti Tri Par White 7ch, ch1 cool / ch2 warm, ch5 strobe | verified | QLC+ fixture panel |
| Stage's normal look: warm white full, dimmer ~60% | verified | live from QLC+ |
| Front Right dimmer on ch8, other RGB pars on ch7 | verified | live from QLC+ |
| Front Right = Elation Opti Quad Par DMX8 8ch (ch5 macro, ch6 strobe, ch7 colour change — all locked) | verified | QLC+ fixture panel |
| Front Middle / Front Left RGB par model, meaning of ch5/ch8 | unknown | read QLC+ fixture panel |
| RGB par ch6 "Burst" = strobe (locked) | assumed | Unreal library |
| Orbiter on 53–65, 13ch "RGBAC Mode 13" | from QLC+ | not yet seen lighting up |
| Heads = Miniwash 710 in 9ch mode | assumed | QLC+ name "7x10W Mini Moving Head", 9 ch |
| White emitter CCTs 6000/3000 K | placeholder | measure |
| Unreal `Stage_DMX_Library` | outdated | still has Orbiter at 200, no heads |

## Safety model

Every frame, for every fixture:

1. **Locked channels** (strobe, mode, reset, unnamed, and for now the heads' pan/tilt) always
   send their fixed value. Nothing in the software can drive them — not even the proxy,
   so an EFX in QLC+ or Unreal cannot move the heads through it.
2. **Range limits**: each channel maps 0..1 into its own `min`..`max` DMX range; the heads'
   dimmer stops at 134 because 135–239 is their strobe range.
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

## Project layout

```
config/stage.toml          network, safety limits, patch, home look
config/profiles/*.toml     channel layout per fixture type (locks, ranges, notes)
xrlight/artnet.py          ArtDmx packets and UDP socket
xrlight/rig.py             profiles, patch, render/decode DMX frames
xrlight/safety.py          smoothing, flash guard, master level
xrlight/color.py           sRGB/linear, CIE xy, CCT, RGBW and cool/warm white mixing
xrlight/patterns.py        test patterns
xrlight/runner.py          pattern sender, proxy, terminal monitor
tests/                     unit tests + loopback proxy test
```

## Next steps

- Audio input (`sounddevice` + NumPy FFT, beat detection with `aubio`) as a new source.
- Wall/scene sampling: zones from an Unreal render target or from frames on the Mac.
- Orbiter in its x/y 16-bit mode (with permission) and an `arri_orbiter_xy` profile.
- Controlled head movement: pan/tilt limits and a maximum speed, never towards the camera.
- Astera Helios tubes (8-pixel x/y mode, profile 115) via a CRMX transmitter.
- Calibration: measure each fixture's real colour per channel and replace the guesses.
- Measurements for the report: ΔE between wall and light, latency, setup time vs manual.

## Notes

The wiki export contains the stage PC password — do not commit those PDFs to Git or quote
them in documentation.

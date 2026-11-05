# Configuration reference

`config/stage.toml` describes the stage; `config/profiles/*.toml` describe fixture
types. Both are read at start-up. `python -m xrlight check` loads them, reports every
problem it finds, and prints the result.

## stage.toml

### `[network]`

| Key | Default | Meaning |
|---|---|---|
| `target_ip` | `10.55.4.7` | where Art-Net is sent (the ODE Mk2) |
| `target_port` | 6454 | |
| `universe` | 1 | universe sent |
| `input_universe` | 1 | universe the proxy listens for |
| `listen_ip`, `listen_port` | `0.0.0.0`, 6454 | proxy input |
| `fps` | 44 | output rate; the ODE Mk2 accepts at most 44 |

### `[safety]`

| Key | Default | Meaning |
|---|---|---|
| `max_flashes_per_second` | 3 | flash guard limit per fixture |
| `flash_threshold` | 0.10 | brightness swing that counts as one transition |
| `smoothing_ms` | 120 | smoothing time constant, 0 = off |
| `master` | 1.0 | global brightness ceiling, 0..1 |
| `input_timeout_s` | 2.0 | proxy: fade home after this long without input |
| `passthrough_unpatched` | true | proxy: copy channels outside the patch from the input |

### `[screen]`

| Key | Default | Range | Meaning |
|---|---|---|---|
| `gamma` | 2.0 | 0.5–4 | exponent of the brightness curve; 1 = linear |
| `black_floor` | 0.08 | 0–0.5 | luminance at or below this gives no light |

See [screen-mode.md](screen-mode.md) for the formula and example values.

### `[[fixture]]`

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | unique; used by zones, `identify` and the monitor |
| `profile` | yes | file name in `config/profiles/` without `.toml` |
| `address` | yes | DMX start address, 1-based |
| `zone` | no | free-text group label, shown by `check` |
| `home` | no | fallback look, attribute → 0..1, e.g. `{ white_2 = 1.0, dimmer = 0.6 }` |
| `fixed` | no | DMX values for this fixture's locked channels, e.g. `{ pan = 168, tilt = 68 }` |

The order of fixtures in the file is the order `identify` and `monitor` use. `check`
prints them sorted by address.

Validation: names are unique; addresses fit in 1–512 and do not overlap; `home` only uses
the profile's free attributes; `fixed` only uses the profile's locked attributes, with
whole numbers 0–255.

### `[[zone]]`

| Key | Required | Meaning |
|---|---|---|
| `fixture` | yes | fixture name |
| `rect` | yes | `[x0, y0, x1, y1]` as fractions 0..1 of the display, y = 0 at the top |
| `gain` | no | 0–2, default 1; multiplies this zone's level |

A fixture has at most one zone. Fixtures without a zone show their home look in screen
mode. Rects may overlap (the side pars share a half each).

## Profiles

```toml
name = "Elation Opti Tri Par White (7ch)"
source = "QLC+ XRStage_default.qxw"
footprint = 7

channels = [
  { offset = 1, attr = "white_1", cct = 6000, display = [0.95, 0.97, 1.00] },
  { offset = 5, attr = "strobe",  lock = 0, note = "strobe — keep at 0" },
  { offset = 6, attr = "dimmer" },
  ...
]
```

| Channel key | Meaning |
|---|---|
| `offset` | 1-based position in the fixture; offsets must cover 1..footprint exactly once |
| `attr` | attribute name; `dimmer` is special, `red`/`green`/`blue`/`white`/`amber` are used by the colour code |
| `lock` | fixed DMX value; the channel is never driven |
| `min`, `max` | DMX range that 0..1 maps onto (default 0–255) |
| `display` | rough sRGB colour of the emitter, for the terminal monitor only |
| `cct` | colour temperature of a white emitter, used by white-only fixtures |
| `note` | shown by `check` |

Free attributes must be unique within a profile; locked ones may repeat (several
`unknown` channels, for example).

Adding a fixture type: write the profile with every channel you are not sure about
locked, add the fixture to `stage.toml`, run `check`, then `test --pattern identify` at a
low level and watch that the right fixture lights up. Unlock a channel only after testing
what it does across its whole range.

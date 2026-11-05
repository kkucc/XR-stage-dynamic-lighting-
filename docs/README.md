# Docs

Technical notes for xr-reactive-light. The top-level README covers setup and how to
run things on the stage; these pages explain how the code works and why it is built
the way it is.

- [architecture.md](architecture.md) — modules, and what happens to one frame
- [patch.md](patch.md) — every fixture on the stage, its channels, and how sure we are
- [screen-mode.md](screen-mode.md) — from captured pixels to fixture colour and level
- [safety.md](safety.md) — locked channels, smoothing, the flash guard, and known gaps
- [config.md](config.md) — reference for `stage.toml` and the profile files

Facts about the stage (addresses, channel layouts) are marked with where they came
from: QLC+ project, VP-Template Unreal library, a manual, or a live test. When those
sources disagree, the QLC+ project wins, because it is what the stage runs today.

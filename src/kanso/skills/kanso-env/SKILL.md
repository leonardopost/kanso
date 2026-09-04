---
name: kanso-env
description: Detect the host machine envelope (chip, cores, memory, OS, power, Python/Nautilus compatibility), derive how many research lanes kanso may run, and point the operator at the service-unit recipe for 24/7 operation. Use when setting up a workspace, when the operator asks how many experiments can run in parallel, after a hardware or OS change, or when `kanso doctor` reports an envelope problem.
license: Apache-2.0
metadata:
  version: "0.1"
---

# kanso-env

## Steps
1. `kanso env detect --json` → writes `envelope.yaml` (`detected` + `plan`). Report in two lines: hardware summary, and `plan.lanes` with the binding constraint (cores or memory).
2. If `detected.nautilus_wheel_ok` is false, the installed `nautilus_trader` wheel is not compatible with this host: its platform tag needs a newer OS than the host runs, or a different architecture. kanso supports macOS 26+ on arm64 and Linux x86_64; an older macOS has no published wheel for the pinned engine and is not supported. Report the tag and the host version rather than guessing a fix, and never edit the kanso package's own `pyproject.toml`.
3. If `detected.on_ac_power` is false and the operator wants 24/7 research: say so; the daemon uses `caffeinate -i` on macOS but cannot override battery sleep policies.
4. For unattended operation, point the operator at the service-unit recipe in `docs/` (launchd on macOS, systemd on Linux) that runs `kanso research start`; never install a unit without their say-so.
5. Re-run `kanso env detect` after the first baseline card of any hypothesis: `mem_per_lane_gb` is calibrated from measured peaks and `lanes` may change.

## Rules
- Never hand-edit `plan.lanes` upward. Override reserved resources in `kanso.toml [env]` (`reserved_cores`, `reserved_mem_gb`, `cores_per_lane`) and re-detect.
- `live_colocated` is derived from `portfolio.yaml`; a live stage with strategies reserves 2 cores + 8 GB by default.

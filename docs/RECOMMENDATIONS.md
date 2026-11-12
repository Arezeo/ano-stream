# Validation and current limits

This page replaces the original planning estimates; hardware performance, platform pricing and eligibility must not be inferred from stale notes.

| Capability | Evidence |
| --- | --- |
| Multi-destination command | Explicit video/audio mapping, one encoder, FIFO tee outputs; command/security unit tests |
| Credentials | Missing-key dry-run reporting, both outputs redacted, backup query handled; unit tests |
| Process ownership | Birth identity required before stop; reused-PID regression test |
| macOS geometry | Main-display bounds and pixel mode from CoreGraphics; secondary/cross-display windows rejected |
| 1080p/60 | Default output; mini hardware-only synthetic test encoded 240 H.264 frames over 4 seconds at 1920×1080/60. This is not proof of game capture FPS. |
| Windows | Native window enumeration + gdigrab + NVENC implemented; native capture verification required |
| Audio | Silent AAC stereo only; game audio and microphones are not captured |
| Platform publishing | Requires stream keys and destination receipt; local process liveness is not proof |
| Model impact | Must be measured before/during the broadcast; hardware encoding can still share memory and bandwidth |
| Automatic recovery | Separate optional skill; not enabled by the broadcast command |

Generated captures, secrets, model data and per-machine measurements are intentionally excluded from Git. Record the helper revision, selected source, actual dimensions/rate, encoder and destination receipts with each local evaluation. Do not publish credentials or private screenshots as test fixtures.

For the current operator setup, the selected split is Mac mini browser/broadcast with Kuka-PC inference. No cloud relay, paid service, or extra model owner is needed for that split.

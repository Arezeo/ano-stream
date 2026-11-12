# ANOStreaming

Broadcast a game on [Agent Number One](https://agentnumberone.io) with one window capture and one hardware encode, then send the encoded feed to YouTube, Twitch, or both. Includes a reusable desktop plugin and the `ano-stream` skill.

```sh
python3 bin/ano-stream init
python3 bin/ano-stream devices
python3 bin/ano-stream up --window Arc --to youtube --dry-run
```

The CLI uses Python 3.10+ and FFmpeg. macOS captures the selected window with ScreenCaptureKit (`--capture window`, default; `--capture screen-crop` keeps the older AVFoundation display grab) and encodes with VideoToolbox; native Windows uses gdigrab and NVIDIA NVENC. Windows capture requires native Windows Python/FFmpeg, not Linux screen capture inside WSL. See [the workflow](docs/WORKFLOW.md) for installation and checks.

## Preview and simulcast

Use the exact game window shown by `devices`. First record a bounded local preview:

```sh
python3 bin/ano-stream preview --window "Game window" --resolution 1920x1080 --fps 60 --seconds 3
python3 bin/ano-stream status --json
```

Enter keys interactively on the broadcast computer, then inspect the redacted plan:

```sh
python3 bin/ano-stream key set youtube
python3 bin/ano-stream key set twitch
python3 bin/ano-stream up --window "Game window" --to youtube,twitch --resolution 1920x1080 --fps 60 --dry-run
```

Publishing is a separate explicit operation:

```sh
python3 bin/ano-stream up --window "Game window" --to youtube,twitch --resolution 1920x1080 --fps 60 --live
python3 bin/ano-stream down
```

Default output is 1920×1080/60 FPS; explicit options and existing destination settings override it. A dry-run discovers and selects a window but does not capture, encode or publish. Missing keys are reported without pretending the destinations are ready. `preview` needs no keys and has no network output. The current audio track is **silent stereo**, not game audio or microphone capture.

Multiple destinations use FFmpeg's `tee` muxer with explicit mappings and one encoder. Each output consumes upload bandwidth. Destinations must agree on resolution and bitrate unless explicit common overrides are supplied. The default shared bitrate is 6 Mbps; platform recommendations and available upload bandwidth should guide an explicit override.

## Plugin and skill

The repository root is a skills-only desktop plugin: `.codex-plugin/plugin.json` references `skills/`. The [ano-stream skill](skills/ano-stream/SKILL.md) uses the same CLI, dry-run, local preview, credentials and verification workflow. No model weights, API service or paid streaming relay are required by this package.

The existing [game-restart skill](skills/game-restart/SKILL.md) is a separate, optional recovery tool. Broadcasting never enables it automatically or counts scripted recovery as model gameplay success. The macOS CGEvent backend and CDP restart selectors still require their own game-specific verification.

## Storage and credentials

Set `ANO_STREAM_HOME` to an unsynced runtime directory before running. Defaults are macOS Application Support, Windows LocalAppData, or Linux local state. Set `ANO_STREAM_FFMPEG` to a native FFmpeg executable when it is not on PATH. Windows users with a full C: drive should place runtime files on another local volume.

Keys stay under `ANO_STREAM_HOME/keys`; dry-runs, worker logs and stored plans redact all destination URLs/keys. FFmpeg still receives the credentials in process arguments, which privileged local process inspection can reveal. Do not commit private configuration, generated captures or credentials.

## Validation

```sh
python3 -B -m unittest discover -s tests -v
```

Tests cover shared encode settings, multi-output mapping, key redaction, backup URL composition, dry-run credential reporting, bounded local output and PID ownership checks. These do not establish capture permissions, platform receipt, sustained 60 FPS or broadcast latency. See [the validation record](docs/RECOMMENDATIONS.md) for measured versus pending checks.

Local model adapters under `packages/` are intentionally excluded from this streaming repository; maintained model integrations live in the [Agent Number One SDK](https://github.com/Arezeo/agentnumberone-agent-sdk).

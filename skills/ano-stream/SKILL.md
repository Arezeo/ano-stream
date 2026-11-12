---
name: ano-stream
description: Prepare, start, inspect or stop game broadcasts through ano-stream, including direct PC capture and simultaneous YouTube and Twitch outputs. Use for broadcasting games, not the model's video input transport.
---

Use this repository's `bin/ano-stream`. Run capture, encoding and publishing on the user's selected broadcast computer. Preserve their host choice; an SSH coordinator is not the broadcast host.

Inspect `--help`, `devices` and `up --help` on that host. This version supports macOS single-window capture through ScreenCaptureKit (`tools/window-capture`; `--capture screen-crop` is the older display-crop fallback) and native Windows capture through gdigrab with NVENC. WSL's Linux screen is not the Windows desktop: run the helper with native Windows Python and FFmpeg when capturing Windows windows.

Examples, after `init`:

```sh
ano-stream up --window Arc --to youtube --dry-run
ano-stream up --window "Game window title" --to youtube,twitch --dry-run
```

Use the actual game window, not an account dashboard or a whole desktop. Inspect the returned plan, credential readiness and selected source before live output. A dry-run neither captures nor broadcasts. It can show missing keys; that does not establish destination readiness.

Default broadcast output is 1920×1080 at 60 FPS. Read [the workflow](../../docs/WORKFLOW.md) for host setup, credentials and source verification. Capture dimensions and observed FPS must be checked independently of output settings. Keep broadcast overlays separate from what the model sees; this output setting does not change model input dimensions or inference frequency.

The helper shares one encoded video stream across comma-separated destinations. All selected destinations must agree on resolution and bitrate, or use explicit common `--resolution` and `--bitrate` overrides. Each destination still consumes upload bandwidth. YouTube's backup ingest is redundancy, not a Twitch output.

Keep keys in the helper's private key files. Use `ano-stream key set youtube` or `twitch` for concealed local entry. Do not place keys or pairing tokens in Git, command examples, overlays or responses. Stream URLs are not keys. FFmpeg needs credentials in its process arguments; local users able to inspect that process may see them. The helper redacts terminal output and persisted logs, not privileged process inspection.

Before publication, establish the requested content, destinations and visibility. Preparation or dry-run does not authorize going live; carry forward existing explicit launch authorization. An unlisted YouTube event is still external publication. Verify the local preview first, then destination receipt before claiming a stream is live. A running worker means only that the local process is alive.

Use `preview --window ... --seconds 3` to record a bounded local calibration clip without keys or network publishing. Inspect it for the intended source, dimensions, FPS and timing. Run `status --json` and compare model latency/tokens/s/completion frequency before and during broadcasting; record capture/encoder FPS, drops, CPU/GPU load and upload separately. Browser-applied action latency remains unknown without application acknowledgement.

Use `down` to stop the helper's owned broadcast or preview. Do not stop inference or restart game worlds as part of stopping a broadcast. Keep recordings and logs outside iCloud; use `ANO_STREAM_HOME` on an unsynced volume, especially when C: is full.

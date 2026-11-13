# Broadcast workflow

Choose the broadcast computer first. A useful split is **Mac mini: game browser + capture/encode/upload; PC: model inference**. The game browser connects to the PC's authenticated WebRTC endpoint. Broadcasting and inference are independently started and stopped. A MacBook used to coordinate over SSH is not in the media path.

The current requested broadcast target is **1920×1080 at 60 FPS**. Keep the model's capture/input settings independent; a 60 FPS broadcast does not imply 60 model decisions per second.

## Install

Clone this repository on the capture computer and install Python 3.10+ and a native FFmpeg with the required device/encoder. No Python packages are needed by the streaming CLI. Keep compiled helpers and runtime files outside synced source trees.

### macOS

```sh
export ANO_STREAM_HOME="$HOME/Library/Application Support/ANOStreaming"
export ANO_STREAM_FFMPEG=/opt/homebrew/bin/ffmpeg
mkdir -p "$ANO_STREAM_HOME/tools" "$ANO_STREAM_HOME/cache/swift"
swiftc -O -module-cache-path "$ANO_STREAM_HOME/cache/swift" \
  skills/game-restart/scripts/os-helper.swift -o "$ANO_STREAM_HOME/tools/os-helper"
swiftc -O -swift-version 5 -module-cache-path "$ANO_STREAM_HOME/cache/swift" \
  skills/ano-stream/scripts/window-capture.swift -o "$ANO_STREAM_HOME/tools/window-capture"
python3 bin/ano-stream init
python3 bin/ano-stream devices
```

Screen Recording permission is required for the capture process; a user must grant macOS permission when prompted.

**Default capture (`--capture window`)** uses ScreenCaptureKit through `tools/window-capture`, which captures only the selected window and pipes raw BGRA frames at a constant rate into the single FFmpeg encode. Overlapping windows, notifications and the menu bar never appear, and the window may be moved or covered. It still captures the whole window, including the browser's own tab strip and address bar, so for a page-only picture run the game in a chromeless app window (for example Chrome `--app=<url>`). The window is found by the `--window` text (app name and title) and found again if the app recreates it; the title is re-checked every second, so if the window stops matching (for example its tab is switched to another page) the capture drops it at once and sends black frames, never the old picture; after 60 seconds with no matching window the capture exits and the broadcast ends. For "stream whichever window or screen", use `--pick` instead of `--window`: the capture tool shows Apple's system sharing picker (the one Meet and Zoom use), nothing is sent until you choose, you can change the shared window from the menu bar sharing control, and stopping sharing ends the capture. Preview first (`preview --pick`). When several windows of one app share a title (for example a normal and an incognito browser window), pin the exact window with `--window-id` from `devices`; the window must then have that id and a matching title. Matching is by text, so make it specific (for example the game page's title) or a different window of the same app, including a personal one, could be captured. A minimized window, or one on another Space, stops delivering new frames; the tool warns and repeats the last frame. Window resizes are scaled into the original output size and are not re-cropped.

**Legacy capture (`--capture screen-crop`)** records the main display with AVFoundation and crops it to the window's rectangle at launch. Keep that window fully on the main display, unobscured and stationary; it does not isolate an occluded window and does not follow a moved or resized one. The helper's logical/pixel display geometry determines the crop; it does not infer Retina scale from a screenshot.

### Windows

Use native Windows Python and FFmpeg, even if coordinating through WSL. A Windows graphical session and a visible, non-minimized game window are required. The current Windows encoder is NVIDIA NVENC. Do not fall back silently to CPU encoding if unavailable.

```powershell
$env:ANO_STREAM_HOME = 'E:\AgentNumberOne\streaming\runtime'
$env:ANO_STREAM_FFMPEG = 'E:\tools\ffmpeg\bin\ffmpeg.exe' # actual installed path
python -B bin/ano-stream init
python -B bin/ano-stream devices
```

WSL may invoke an installed native `python.exe` with Windows paths. Linux FFmpeg `x11grab` is not a substitute for Windows window capture. Capture uses the selected window handle rather than a whole desktop or first title match.

## Duration and bitrate

- **Auto-stop:** a live broadcast ends by itself after `--max-hours` (default 3; `0` = no limit, maximum 48). Preview is unaffected. YouTube archives and rewinds streams only up to 12 hours; Twitch caps one broadcast at 48 hours.
- **Bitrate:** YouTube's encoder guidance for H.264 at 1080p60 is 6 Mbps minimum, 17 Mbps recommended, constant bitrate, keyframe every 2 s. The macOS VideoToolbox encoder spends almost nothing on a still picture (measured about 0.35-0.47 Mbps for a static page at a 6-17 Mbps target, with or without `-constant_bit_rate`), so use `--cbr` with `--bitrate 17000k` to send the full rate: it encodes with x264 and pads with filler (measured 17.0 Mbps on a still image, roughly 1.6 CPU cores). That is about 7.6 GB of upload per hour. Twitch's ingest accepts far less (about 6-8 Mbps), so when one encode feeds both, pick a rate Twitch accepts.

## Local preview

Open the production game page on the capture computer, select the game, and pair it with the inference runner. When moving a single-peer connection to a new browser, disconnect the old browser first; do not restart inference solely to change browsers.

```sh
python3 bin/ano-stream preview --window "Game window" --resolution 1920x1080 --fps 60 --seconds 3
python3 bin/ano-stream status --json
```

Open the returned `preview_file`. Verify the intended game is visible and read dimensions/FPS from FFprobe. FFmpeg progress includes encoded frame count, reported FPS, output time and dropped/duplicated frames where available. Configured FPS and metadata are not proof of genuinely fresh source frames at that rate. On macOS window capture the preview carries the shared app's audio (see "Audio and tab guard" below); Windows and `--capture screen-crop` are silent.

If frame/input timing is used for model supervision, perform a separate real calibration with a visible input effect and source timestamps. A 60 FPS encode does not establish calibrated demonstration timing.

## Destinations and keys

`init` creates `destinations.json` and preserves existing configuration/state. Keys are entered through concealed input:

```sh
python3 bin/ano-stream key set youtube
python3 bin/ano-stream key set twitch
```

Use current ingest addresses from each platform. YouTube supplies primary and backup ingest addresses plus a separate stream key; the backup is optional redundancy, not the Twitch output. The helper appends a key before a server URL's query string. Default destinations use RTMPS. Set visibility in the platform's event/dashboard before starting a public output.

The requested channels are configured locally, not embedded as credentials or assumed to be signed in by this repository. Choose shared resolution/bitrate explicitly when existing destination configurations differ.

```sh
python3 bin/ano-stream up --window "Game window" --to youtube,twitch \
  --resolution 1920x1080 --fps 60 --bitrate 6000k --dry-run
```

A successful dry-run is a prepared command, not an ingest test. Missing keys are allowed for planning and clearly reported. Publishing requires `--live` and valid keys:

```sh
python3 bin/ano-stream up --window "Game window" --to youtube,twitch \
  --resolution 1920x1080 --fps 60 --bitrate 6000k --live
```

Inspect YouTube/Twitch's own ingest health and actual watch pages before reporting success. Check [Twitch's current simulcasting terms](https://www.twitch.tv/p/terms-of-service#11-simulcasting), [YouTube encoder guidance](https://support.google.com/youtube/answer/2853702?hl=en), and [Twitch ingest documentation](https://dev.twitch.tv/docs/video-broadcast/). Two direct 6 Mbps video outputs consume about 12 Mbps plus audio/protocol overhead. No paid relay is configured.

## Metrics and stop

```sh
python3 bin/ano-stream status --json
python3 bin/ano-stream down
```

`status` reports owned process liveness, redacted plan, encoder progress and exit status. It does not query platform receipt or measure viewer delay. Progress timestamps establish freshness; stale metrics from an exited process are not current performance.

Compare the same model/settings before and during broadcasting: inference median/P95, generated tokens/s, actual model completions/s, received video FPS, input events sent, GPU/VRAM/CPU/RAM and network throughput. Keep policy/playback callbacks separate from model completions and successfully applied browser inputs. Successful action application remains unknown when the protocol supplies no acknowledgement.

`down` verifies process identity before stopping its own process tree. It leaves the model runner and game state alone. Multiple simultaneous `up` invocations are not supported; start one command at a time.

## Audio and tab guard (macOS)

`window-capture` also records the shared app's audio through ScreenCaptureKit and hands it to FFmpeg over an inherited pipe (48 kHz stereo float, written at a constant rate, silence when nothing plays). Use `--no-audio` to send silence instead.

ScreenCaptureKit exposes audio **per application**, not per tab or window. Every tab of the shared browser that is playing sound is included, even one in another window, so keep the management tabs (YouTube Studio, Twitch) muted. Capturing only one tab's sound would need a browser-tab capture inside the browser rather than ScreenCaptureKit.

A window shows only its active tab, so opening a new tab in the shared window changes what is streamed. `--pick --guard TEXT` prevents that from leaking: the title of the shared window must contain TEXT (for example `--guard "AGENT NUMBER ONE"`). While it does not, the picture is black and the audio silent; both return when the title matches again. The title is checked four times a second, so a tab switch can show for a fraction of a second first. The cleanest setup is a dedicated window that holds only the game tab, with every other tab opened in a different window. `--guard` needs `--pick` and a shared window (not a screen).

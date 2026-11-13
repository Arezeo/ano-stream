"""Browser sharing for ano-stream, like Google Meet.

A page served here opens the browser's own share picker (Chrome, Edge, Arc): a Chrome tab, a
window or the entire screen. It records only what was chosen and uploads the chunks to this
relay, which feeds them to FFmpeg. A shared tab sends only that tab's picture and sound, so other
tabs and windows never enter the stream, whatever you open or switch to.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_CHUNK = 32*1024*1024

PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ANO stream</title>
<style>
 :root{color-scheme:dark light} body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}
 button{font:inherit;padding:.6rem 1rem;margin:.25rem .5rem .25rem 0;cursor:pointer}
 #state{padding:.75rem 1rem;border-radius:8px;background:#8883}
 .live{background:#1a7f3733!important} .bad{background:#c0392b33!important}
</style></head><body>
<h1>Choose what to stream</h1>
<p>Press the button and pick in the browser, as in Meet: <b>Chrome Tab</b> (best: only that tab's picture and sound, tick <b>Also share tab audio</b>), <b>Window</b> or <b>Entire Screen</b> (picture only, no sound in this browser). To list a private-window tab, open this page in that private window.</p>
<p id="state">Nothing is being streamed yet.</p>
<button id="go">Choose what to stream</button><button id="stop" disabled>Stop streaming</button>
<p><small>Only what you choose is sent. Closing this page, or choosing Stop sharing in the browser, ends the broadcast.</small></p>
<script>
const WANT_AUDIO = __WANT_AUDIO__;
const base = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
const $ = id => document.getElementById(id);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let recorder = null, stream = null, surface = 'unknown', seq = 0, sent = 0, startedAt = 0, failed = false, finished = false;
function say(text, kind) { const s = $('state'); s.textContent = text; s.className = kind || ''; }

async function post(n, blob) {
  for (let attempt = 0; attempt < 40 && !finished; attempt++) {
    try {
      const r = await fetch(base + 'chunk', { method: 'POST', body: blob,
        headers: { 'X-Seq': String(n), 'X-Has-Audio': stream.getAudioTracks().length ? '1' : '0', 'X-Surface': surface } });
      if (r.ok) { sent += blob.size; return true; }
      if (r.status === 410) return false;           // the encoder has ended
      if (r.status === 400) { failed = true; return false; }
    } catch (e) { /* relay briefly unreachable: retry */ }
    await sleep(250);
  }
  return false;
}

function finish(message, kind) {
  if (finished) return;
  finished = true;
  try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (e) {}
  if (stream) stream.getTracks().forEach(t => t.stop());
  fetch(base + 'end', { method: 'POST', keepalive: true }).catch(() => {});
  say(message, kind || 'bad');
  $('go').disabled = true; $('stop').disabled = true;
}

$('go').onclick = async () => {
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 60, max: 60 } },
      audio: WANT_AUDIO ? { echoCancellation: false, noiseSuppression: false, autoGainControl: false, suppressLocalAudioPlayback: false } : false,
      preferCurrentTab: false, selfBrowserSurface: 'exclude', surfaceSwitching: 'exclude', systemAudio: 'include' });
  } catch (e) { say('Nothing was shared (' + e.name + '). Press the button to try again.', 'bad'); return; }
  const video = stream.getVideoTracks()[0];
  surface = video.getSettings().displaySurface || 'unknown';
  if (WANT_AUDIO && surface === 'browser' && !stream.getAudioTracks().length) {
    stream.getTracks().forEach(t => t.stop());
    say('No tab audio was shared. Choose the tab again and tick "Also share tab audio".', 'bad'); return;
  }
  const types = ['video/webm;codecs=h264,opus', 'video/webm;codecs=avc1,opus', 'video/webm;codecs=vp8,opus', 'video/webm'];
  const mimeType = types.find(t => MediaRecorder.isTypeSupported(t));
  recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 12000000, audioBitsPerSecond: 128000 });
  let queue = Promise.resolve();
  recorder.ondataavailable = e => {
    if (!e.data.size || finished) return;
    const n = seq++;
    queue = queue.then(async () => { if (!(await post(n, e.data))) finish(failed ? 'The relay refused the stream (audio missing?).' : 'The encoder stopped, so the broadcast ended.'); });
  };
  recorder.onstop = () => { queue.then(() => finish('Sharing stopped. The broadcast has ended.', 'bad')); };
  video.onended = () => { if (recorder.state !== 'inactive') recorder.stop(); };
  recorder.start(250);
  startedAt = Date.now();
  $('go').disabled = true; $('stop').disabled = false;
  const tick = () => { if (!finished) { say('Streaming ' + ({browser:'this tab',window:'this window',monitor:'the ENTIRE SCREEN'}[surface] || 'your share') + ' (' + (stream.getAudioTracks().length ? 'with' : 'without') + ' sound) - ' +
    Math.round((Date.now() - startedAt) / 1000) + ' s, ' + (sent / 1048576).toFixed(1) + ' MB sent', 'live'); setTimeout(tick, 1000); } };
  tick();
};
$('stop').onclick = () => { if (recorder && recorder.state !== 'inactive') recorder.stop(); };
window.addEventListener('pagehide', () => { if (!finished) finish('Page closed.', 'bad'); });
</script></body></html>
'''


class Relay:
    """Serves the sharing page and pipes uploaded chunks, in order, into the encoder's stdin."""

    def __init__(self, token, start, port=0, want_audio=True):
        self.token = token
        self.want_audio = want_audio
        self.start = start            # start(has_audio) -> writable binary stream, or None
        self.sink = None
        self.next_seq = 0
        self.bytes = 0
        self.ended = threading.Event()
        self.lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def reply(self, code, body=b'', ctype='text/plain; charset=utf-8'):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)

            def allowed(self):
                port_text = str(relay.server.server_address[1])
                hosts = {'127.0.0.1:'+port_text, 'localhost:'+port_text}
                origin = self.headers.get('Origin')
                if self.headers.get('Host') not in hosts:
                    return False
                return origin is None or origin in {'http://'+h for h in hosts}

            def route(self):
                prefix = '/'+relay.token+'/'
                return self.path.split('?')[0][len(prefix):] if self.path.split('?')[0].startswith(prefix) else None

            def do_GET(self):
                name = self.route()
                if not self.allowed() or name is None:
                    return self.reply(404)
                if name == '':
                    page = PAGE.replace('__WANT_AUDIO__', 'true' if relay.want_audio else 'false')
                    return self.reply(200, page.encode(), 'text/html; charset=utf-8')
                if name == 'state':
                    body = json.dumps({'started': relay.sink is not None, 'ended': relay.ended.is_set(), 'bytes': relay.bytes})
                    return self.reply(200, body.encode(), 'application/json')
                self.reply(404)

            def do_POST(self):
                name = self.route()
                if not self.allowed() or name not in ('chunk', 'end'):
                    return self.reply(404)
                length = int(self.headers.get('Content-Length') or 0)
                if length > MAX_CHUNK:
                    self.close_connection = True
                    return self.reply(413)
                body = self.rfile.read(length) if length else b''
                if name == 'end':
                    relay.finish()
                    return self.reply(200)
                try:
                    seq = int(self.headers.get('X-Seq', ''))
                except ValueError:
                    return self.reply(400)
                has_audio = self.headers.get('X-Has-Audio') == '1'
                surface = self.headers.get('X-Surface', 'unknown')
                self.reply(relay.accept(seq, has_audio, body, surface))

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True

    @property
    def port(self):
        return self.server.server_address[1]

    def url(self):
        return 'http://127.0.0.1:%d/%s/' % (self.port, self.token)

    def accept(self, seq, has_audio, body, surface='browser'):
        """Returns an HTTP status: 200 stored (or already stored), 409 out of order, 410 ended, 400 refused, 503 no encoder."""
        with self.lock:
            if self.ended.is_set():
                return 410
            if seq < self.next_seq:
                return 200
            if seq != self.next_seq:
                return 409
            if self.sink is None:
                if self.want_audio and surface == 'browser' and not has_audio:
                    return 400   # a tab share must carry its sound; window/screen shares cannot in this browser
                self.sink = self.start(has_audio)
                if self.sink is None:
                    return 503
            try:
                self.sink.write(body)
                self.sink.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.ended.set()
                return 410
            self.next_seq += 1
            self.bytes += len(body)
            return 200

    def finish(self):
        with self.lock:
            self.ended.set()
            if self.sink is not None:
                try:
                    self.sink.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

    def serve(self):
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        return thread

    def close(self):
        self.finish()
        self.server.shutdown()
        self.server.server_close()

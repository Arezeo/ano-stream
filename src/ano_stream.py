"""One window or browser-tab capture, one encode, multiple RTMP destinations."""
from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
from pathlib import Path
import platform
import re
import secrets as token_source
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from tab_relay import Relay


def default_home():
    if platform.system() == 'Darwin':
        return Path.home()/'Library/Application Support/ANOStreaming'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'ANOStreaming'
    return Path.home()/'.local/state/ano-stream'


HOME = Path(os.environ.get('ANO_STREAM_HOME', default_home()))
CONFIG = HOME/'destinations.json'
STATE = HOME/'state.json'
DEFAULT_DESTINATIONS = {
    'youtube': {'kind':'rtmp', 'url':'rtmps://a.rtmps.youtube.com:443/live2',
                'key_file':'youtube', 'resolution':[1920,1080], 'bitrate':'6000k'},
    'twitch': {'kind':'rtmp', 'url':'rtmps://live.twitch.tv:443/app',
               'key_file':'twitch', 'resolution':[1920,1080], 'bitrate':'6000k'},
}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
    temp.replace(path)


def read_json(path, fallback=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback


def init_home():
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME/'keys').mkdir(exist_ok=True)
    (HOME/'logs').mkdir(exist_ok=True)
    if os.name != 'nt':
        HOME.chmod(0o700)
        (HOME/'keys').chmod(0o700)
    if not CONFIG.exists():
        write_json(CONFIG, DEFAULT_DESTINATIONS)
    if not STATE.exists():
        write_json(STATE, {'runs':[]})


def key_path(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('key_file must be a simple name inside the keys directory')
    return HOME/'keys'/name


def destinations(cfg, names, dry_run=False):
    targets, missing, secrets = [], [], []
    for name in dict.fromkeys(n.strip() for n in names.split(',') if n.strip()):
        if name not in cfg:
            raise ValueError('Unknown destination: '+name)
        spec = dict(cfg[name])
        base = spec.get('url', '')
        parsed = urlsplit(base)
        if parsed.scheme not in ('rtmp','rtmps') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Invalid RTMP server for '+name)
        key = None
        if spec.get('key_file'):
            path = key_path(spec['key_file'])
            key = path.read_text(encoding='utf-8').strip() if path.exists() else None
            if key and re.search(r'[\s|\[\]\\]',key):
                raise ValueError('Unsupported characters in stream key for '+name)
            if not key:
                missing.append(name)
                if not dry_run:
                    raise ValueError('Missing key for '+name+'; use: ano-stream key set '+name)
                key = 'MISSING_KEY_'+name
            else:
                secrets.append(key)
        path = parsed.path.rstrip('/')+('/'+key if key else '')
        url = urlunsplit((parsed.scheme,parsed.netloc,path,parsed.query,''))
        targets.append((name,spec,url))
        secrets.append(url)
    if not targets:
        raise ValueError('Choose at least one destination')
    if len({s.get('kind')=='rtmp-vertical' for _,s,_ in targets})>1:
        raise ValueError('Mixed orientations require separate captures; choose one orientation')
    return targets, missing, secrets


def redact(text, secrets):
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret,'<redacted>')
    return re.sub(r'rtmps?://[^\s|\]]+', '<redacted-rtmp-url>', text)


def ffmpeg_path():
    value = os.environ.get('ANO_STREAM_FFMPEG') or shutil.which('ffmpeg')
    if not value or not Path(value).is_file():
        raise ValueError('FFmpeg not found; set ANO_STREAM_FFMPEG to the native executable')
    return str(Path(value).resolve())


def native_windows():
    if os.name != 'nt':
        raise ValueError('Windows capture requires native Windows Python, not WSL Python')
    from ctypes import wintypes
    user = ctypes.windll.user32
    try:
        user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    class Rect(ctypes.Structure):
        _fields_ = [(k,wintypes.LONG)for k in ('left','top','right','bottom')]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND,ctypes.POINTER(Rect)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.EnumWindows.argtypes = [callback_type,wintypes.LPARAM]
    found = []
    @callback_type
    def visit(hwnd,_):
        if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
            return True
        n = user.GetWindowTextLengthW(hwnd)
        if n:
            title = ctypes.create_unicode_buffer(n+1)
            user.GetWindowTextW(hwnd,title,n+1)
            rect = Rect()
            if user.GetWindowRect(hwnd,ctypes.byref(rect)):
                found.append({'id':int(hwnd),'owner':'Windows','name':title.value,
                              'x':rect.left,'y':rect.top,'w':rect.right-rect.left,'h':rect.bottom-rect.top})
        return True
    user.EnumWindows(visit,0)
    return found


def helper():
    value = os.environ.get('ANO_HELPER')
    path = Path(value) if value else HOME/'tools/os-helper'
    if not path.is_file():
        raise ValueError('Build the macOS os-helper into ANO_STREAM_HOME/tools; see docs/WORKFLOW.md')
    return path


def capture_tool():
    value = os.environ.get('ANO_CAPTURE')
    path = Path(value) if value else HOME/'tools/window-capture'
    if not path.is_file():
        raise ValueError('Build window-capture into ANO_STREAM_HOME/tools or use --capture screen-crop; see docs/WORKFLOW.md')
    return path


def fit_size(win, size):
    """Largest even size with the window's aspect ratio that fits inside size."""
    scale = min(size[0]/win['w'],size[1]/win['h'])
    return [max(16,int(win['w']*scale)//2*2),max(16,int(win['h']*scale)//2*2)]


def windows(backend):
    if backend=='windows':
        return native_windows()
    if backend=='macos':
        return json.loads(subprocess.check_output([str(helper()),'windows'],text=True,timeout=10))
    raise ValueError('Window capture supports macOS and native Windows only')


def choose_window(items, match, window_id=None):
    candidates = [w for w in items if w['w']>320 and w['h']>200
                  and match.casefold() in (w['owner']+' '+w['name']).casefold()
                  and (window_id is None or w['id']==window_id)]
    if not candidates:
        raise ValueError('No visible window matches '+repr(match)+(' with id '+str(window_id) if window_id else '')+'; run ano-stream devices')
    return max(candidates,key=lambda w:w['w']*w['h'])


def backend_name(value='auto'):
    return ('windows' if os.name=='nt' else 'macos' if platform.system()=='Darwin' else 'unsupported') if value=='auto' else value


def resolution(value):
    if not re.fullmatch(r'[1-9][0-9]*x[1-9][0-9]*',value):
        raise ValueError('Resolution must be WIDTHxHEIGHT')
    w,h = map(int,value.split('x'))
    if min(w,h)<16 or max(w,h)>7680 or w%2 or h%2:
        raise ValueError('Resolution must use even dimensions from 16 to 7680')
    return [w,h]


def common_settings(targets, args):
    sizes = {tuple(spec.get('resolution',[1920,1080])) for _,spec,_ in targets}
    rates = {spec.get('bitrate','6000k') for _,spec,_ in targets}
    if not args.resolution and len(sizes)>1:
        raise ValueError('Destinations disagree on resolution; set --resolution for a shared encode')
    if not args.bitrate and len(rates)>1:
        raise ValueError('Destinations disagree on bitrate; set --bitrate for a shared encode')
    size = resolution(args.resolution) if args.resolution else resolution('x'.join(map(str,next(iter(sizes)))))
    rate = args.bitrate or next(iter(rates))
    if not re.fullmatch(r'[1-9][0-9]*[kM]',rate):
        raise ValueError('Bitrate must be a positive value such as 6000k')
    return size,rate


def mac_geometry(win):
    displays = json.loads(subprocess.check_output([str(helper()),'displays'],text=True,timeout=10))
    main = next((d for d in displays if d['main']),None)
    if not main or win['x']<main['x'] or win['y']<main['y'] or win['x']+win['w']>main['x']+main['w'] or win['y']+win['h']>main['y']+main['h']:
        raise ValueError('This macOS backend requires the entire window on the main display')
    sx,sy = main['pixel_w']/main['w'],main['pixel_h']/main['h']
    return [int((win['x']-main['x'])*sx),int((win['y']-main['y'])*sy),int(win['w']*sx)//2*2,int(win['h']*sy)//2*2]


AUDIO_FD = '@AUDIO_FD@'   # replaced by the worker with the real pipe descriptor


def make_command(executable, backend, win, targets, fps=60, size=(1920,1080), bitrate='6000k', output=None, mac_crop=None, window_size=None, cbr=False, audio=False, tab=False):
    if tab:
        # WebM from the browser's MediaRecorder (one shared tab), uploaded through the relay to stdin.
        source = ['-fflags','+genpts','-analyzeduration','1000000','-probesize','2000000','-thread_queue_size','512',
                  '-f','matroska','-i','pipe:0']
        crop,encoder = '','h264_nvenc' if backend=='windows' else 'h264_videotoolbox'
    elif backend=='windows':
        source = ['-f','gdigrab','-draw_mouse','0','-framerate',str(fps),'-i','hwnd='+str(win['id'])]
        crop,encoder = '','h264_nvenc'
    elif window_size:
        # Raw BGRA from window-capture (ScreenCaptureKit): only the chosen window's pixels.
        source = ['-f','rawvideo','-pix_fmt','bgra','-video_size','x'.join(map(str,window_size)),'-framerate',str(fps),
                  '-use_wallclock_as_timestamps','1','-i','pipe:0']
        crop,encoder = '','h264_videotoolbox'
    else:
        source = ['-f','avfoundation','-capture_cursor','0','-framerate',str(fps),'-i','Capture screen 0']
        if not mac_crop:
            raise ValueError('macOS capture requires verified display geometry')
        x,y,w,h = mac_crop
        crop,encoder = f'crop={w}:{h}:{x}:{y},','h264_videotoolbox'
    if cbr and backend=='macos':
        encoder = 'libx264'   # VideoToolbox spends almost nothing on a still picture and cannot pad
    w,h = size
    matrix = ':out_color_matrix=bt709:out_range=tv' if window_size else ''
    vf = crop+f'scale={w}:{h}:force_original_aspect_ratio=decrease{matrix},pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p'
    if audio=='tab':
        sound,audio_map = [],'0:a:0'   # the shared tab's own sound travels in the same WebM
    elif audio:
        # Raw float32 stereo from window-capture on an inherited pipe (the worker fills in the descriptor).
        sound,audio_map = ['-f','f32le','-ar','48000','-ac','2','-i','pipe:'+AUDIO_FD],'1:a:0'
    else:
        sound,audio_map = ['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=44100'],'1:a:0'
    command = [executable,'-hide_banner','-loglevel','warning','-nostdin','-y',*source,*sound,
               '-map','0:v:0','-map',audio_map,*(['-af','aresample=async=1:first_pts=0']if audio=='tab'else[]),'-vf',vf,'-c:v',encoder,'-b:v',bitrate,
               '-maxrate',bitrate,'-bufsize',str(2*int(bitrate[:-1]))+bitrate[-1],
               '-g',str(fps*2),'-c:a','aac','-b:a','128k','-ar','44100',
               '-progress','pipe:1','-stats_period','1','-nostats']
    if backend=='windows':
        command += ['-preset','p4','-rc','cbr']
    elif cbr:
        # True constant bitrate: x264 pads with filler so a still page still sends the full rate
        # (YouTube recommends CBR and a 2 s keyframe interval). Uses the CPU.
        g = fps*2
        command += ['-preset','veryfast','-profile:v','high','-minrate',bitrate,
                    '-x264-params',f'nal-hrd=cbr:force-cfr=1:keyint={g}:min-keyint={g}:scenecut=0']
    else:
        command += ['-realtime','1','-allow_sw','0']
    if window_size:
        command += ['-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709']
    if output:
        command += ['-t',str(output[1]),'-f','matroska',str(output[0])]
    elif len(targets)==1:
        command += ['-f','flv',targets[0][2]]
    else:
        slaves = '|'.join('[f=flv:onfail=ignore]'+url.replace('\\','\\\\').replace("'", "'\\''")for _,_,url in targets)
        command += ['-f','tee','-use_fifo','1','-fifo_options','attempt_recovery=1:recover_any_error=1:recovery_wait_time=2',slaves]
    return command


def process_birth(pid):
    if os.name=='nt':
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p,*([ctypes.c_void_p]*4)]
        handle = kernel.OpenProcess(0x1000,0,int(pid))
        if not handle:
            return None
        try:
            times = [ctypes.c_uint64()for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(t)for t in times)):
                return None
            return str(times[0].value)
        finally:
            kernel.CloseHandle(handle)
    path = Path('/proc')/str(pid)/'stat'
    try:
        if path.exists():
            return path.read_text().rsplit(')',1)[1].split()[19]
        out = subprocess.run(['ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=3)
        return out.stdout.strip() or None
    except OSError:
        return None


def owned(run):
    return bool(run.get('birth') and process_birth(run['pid'])==run['birth'])


def stop_owned(run):
    if not owned(run):
        return False
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(run['pid']),'/T','/F'],capture_output=True,timeout=10,check=True)
    else:
        os.killpg(run['pid'],signal.SIGTERM)
    return True


def inspect_plan(args, publish=True):
    backend = backend_name(args.backend)
    picking = bool(getattr(args,'pick',False))
    tabbing = getattr(args,'capture','window')=='tab'
    if tabbing:
        if picking or getattr(args,'window',None) or getattr(args,'guard',None):
            raise ValueError('--capture tab shares a browser tab chosen in the browser; do not combine it with --pick, --window or --guard')
        win = None
    elif picking:
        if backend!='macos' or getattr(args,'capture','window')!='window':
            raise ValueError('--pick needs macOS window capture (--capture window)')
        win = None
    else:
        if not args.window:
            raise ValueError('Give --window TEXT, or --pick to choose in the macOS sharing picker')
        win = choose_window(windows(backend),args.window,getattr(args,'window_id',None))
    cfg = read_json(CONFIG)
    if cfg is None:
        raise ValueError('No config; run ano-stream init')
    if publish:
        targets,missing,secrets = destinations(cfg,args.to,dry_run=args.dry_run)
    else:
        targets,missing,secrets = [('preview',{'resolution':[1920,1080],'bitrate':'6000k'},'')],[],[]
    size,rate = common_settings(targets,args)
    if picking:
        win = {'id':0,'owner':'macOS sharing picker','name':'chosen by you in the picker','x':0,'y':0,'w':size[0],'h':size[1]}
    if tabbing:
        win = {'id':0,'owner':'browser','name':'tab chosen by you in the browser','x':0,'y':0,'w':size[0],'h':size[1]}
    window_capture = backend=='macos' and getattr(args,'capture','window')=='window'
    crop = mac_geometry(win) if backend=='macos' and not window_capture and not tabbing else None
    window_size = fit_size(win,size) if window_capture else None
    if backend=='macos' and not tabbing:
        access = json.loads(subprocess.check_output([str(helper()),'permissions'],text=True,timeout=10))
        if not access.get('screen_recording') and not args.dry_run:
            raise ValueError('Screen Recording permission is not granted on this Mac; allow the capture process in System Settings > Privacy & Security > Screen & System Audio Recording')
    output = (HOME/'previews'/(args.run_id+'.mkv'),args.seconds) if not publish else None
    cbr = bool(getattr(args,'cbr',False))
    if cbr and backend!='macos':
        raise ValueError('--cbr is only for macOS; NVENC on Windows already uses constant bitrate')
    hours = float(getattr(args,'max_hours',0) or 0)
    if not 0<=hours<=48:
        raise ValueError('--max-hours must be between 0 (no limit) and 48')
    guard = (getattr(args,'guard',None) or '').strip()
    if guard and not picking:
        raise ValueError('--guard needs --pick; --window already requires the title text')
    audio = ('tab' if tabbing else True) if bool(getattr(args,'audio',True)) and (window_capture or tabbing) else False
    command = make_command(ffmpeg_path(),backend,win,targets,args.fps,size,rate,output,crop,window_size,cbr,audio,tabbing)
    # The tool re-resolves the window by this text, so a recreated window (Arc does this) is found again.
    capture_command = [str(capture_tool()),'--pick' if picking else '--match',*([] if picking else [args.window]),'--width',str(window_size[0]),
                       '--height',str(window_size[1]),'--fps',str(args.fps)] if window_capture else None
    if capture_command and getattr(args,'window_id',None) and not picking:
        capture_command += ['--window-id',str(args.window_id)]
    if capture_command and audio==True:
        capture_command += ['--audio-fd',AUDIO_FD]
    if capture_command and guard:
        capture_command += ['--guard',guard]
    plan = {'backend':backend,'host':platform.node(),'window':win,'resolution':size,'fps_target':args.fps,
            'bitrate':rate,'destinations':[name for name,_,_ in targets] if publish else [],
            'missing_keys':missing,'credential_ready':not missing,'dry_run':args.dry_run,
            'audio':'Audio of a shared tab only (its own sound, nothing from other tabs); a shared window or screen is silent in the browser' if audio=='tab' else 'Audio of the shared app (ScreenCaptureKit; every tab of that browser that plays sound, so mute the others); black and silent while the --guard title is missing' if audio and guard else 'Audio of the shared app (ScreenCaptureKit; every tab of that browser that plays sound, so mute the others)' if audio else 'Silent stereo; audio capture is off',
            'guard':guard or None,
            'one_encode':True,'command':[redact(a,secrets)for a in command],
            'capture':'browser share (tab, window or entire screen, chosen in the browser picker like Meet; a tab sends only its own picture and sound)' if tabbing else ('window or screen (ScreenCaptureKit, chosen in the macOS sharing picker; nothing is sent until you choose)' if picking else 'window (ScreenCaptureKit, matches '+repr(args.window)+', re-resolved if recreated)') if window_capture else 'screen-crop (whole main display cropped to the window; shows anything overlapping it)' if backend=='macos' else 'window (gdigrab)',
            'capture_command':capture_command,
            'tab_url':'http://127.0.0.1:%s/%s/'%(args.tab_port,args.tab_token) if tabbing and getattr(args,'tab_port',None) else None,
            'cbr_padding':cbr,'auto_stop_hours':hours if publish else 0,
            'observed_capture_fps':None,'platform_receipt':'unknown'}
    if backend=='macos' and not tabbing:
        plan['screen_recording_permission'] = access.get('screen_recording',False)
    if output:
        plan['preview_file'] = str(output[0])
    return plan,command,secrets


def start_auto_stop(hours, action):
    """Run action() after `hours`; returns the started daemon Timer, or None when there is no limit."""
    if not hours:
        return None
    timer = threading.Timer(hours*3600,action)
    timer.daemon = True
    timer.start()
    return timer


def terminate_process(process, grace=3):
    """Reap the owned encoder, including capture drivers that ignore SIGTERM."""
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=grace)


def worker(args):
    request = read_json(HOME/'requests'/(args.run_id+'.json'))
    args = argparse.Namespace(**request)
    plan,command,secrets = inspect_plan(args,publish=args.mode=='up')
    preview = plan.get('preview_file')
    if preview:
        Path(preview).parent.mkdir(parents=True,exist_ok=True)
    logfile = HOME/'logs'/(args.run_id+'.log')
    progressfile = HOME/'logs'/(args.run_id+'-progress.json')
    creation = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    capture = None
    relay = None
    if plan.get('tab_url'):
        # Browser tab sharing: the encoder starts when the first chunk arrives from the page.
        holder,ready = {},threading.Event()
        silent_args = argparse.Namespace(**{**vars(args),'audio':False})
        silent_command = inspect_plan(silent_args,publish=args.mode=='up')[1]
        def start_encoder(has_audio):
            holder['process'] = subprocess.Popen(command if has_audio else silent_command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                                 text=True,encoding='utf-8',errors='replace',**creation)
            ready.set()
            return holder['process'].stdin.buffer
        relay = Relay(args.tab_token,start_encoder,args.tab_port,want_audio=bool(getattr(args,'audio',True)))
        relay.serve()
        write_json(HOME/'logs'/(args.run_id+'-tab.json'),{'url':relay.url()})
        if os.name!='nt':
            def stop_waiting(_signal,_frame):
                relay.close()
                raise SystemExit(0)
            signal.signal(signal.SIGTERM,stop_waiting)
        if not ready.wait(600 if args.mode=='preview' else 3600):
            relay.close()
            with logfile.open('a',encoding='utf-8')as log:
                log.write('no browser tab was shared within the waiting time; giving up\n')
            return 4
    audio_pipe = os.pipe() if os.name!='nt' and AUDIO_FD in (plan.get('capture_command') or []) else None
    if relay:
        pass
    elif plan.get('capture_command'):
        capture_command = plan['capture_command']
        if audio_pipe:
            capture_command = [a.replace(AUDIO_FD,str(audio_pipe[1]))for a in capture_command]
            command = [a.replace(AUDIO_FD,str(audio_pipe[0]))for a in command]
        with logfile.open('a',encoding='utf-8')as log:
            capture = subprocess.Popen(capture_command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=log,
                                       pass_fds=(audio_pipe[1],) if audio_pipe else (),**creation)
    process = holder['process'] if relay else subprocess.Popen(command,stdin=capture.stdout if capture else subprocess.DEVNULL,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',
                               pass_fds=(audio_pipe[0],) if audio_pipe else (),**creation)
    if audio_pipe:
        os.close(audio_pipe[0])
        os.close(audio_pipe[1])
    if capture:
        capture.stdout.close()
    if os.name!='nt':
        def terminate_worker(_signal,_frame):
            if relay:
                relay.close()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM,terminate_worker)
    # Output duration alone cannot bound a capture device stuck before its first frame.
    def stop_stalled_preview():
        terminate_process(process)
        if relay:
            relay.close()
        if capture:
            terminate_process(capture)
    timer = threading.Timer(args.seconds+20+(60 if getattr(args,'pick',False) else 0),stop_stalled_preview) if preview else None
    if timer:
        timer.daemon = True
        timer.start()
    def errors():
        with logfile.open('a',encoding='utf-8')as log:
            for line in process.stderr:
                log.write(redact(line,secrets))
                log.flush()
    thread = threading.Thread(target=errors,daemon=True)
    thread.start()
    ended = {}
    def auto_stop():
        ended['by'] = 'max-hours'
        with logfile.open('a',encoding='utf-8')as log:
            log.write('auto-stop: reached --max-hours; ending the broadcast\n')
        terminate_process(process)
        if relay:
            relay.close()
        if capture:
            terminate_process(capture)
    stopper = start_auto_stop(float(getattr(args,'max_hours',0) or 0) if args.mode=='up' else 0,auto_stop)
    try:
        frame = {}
        for line in process.stdout:
            key,_,value = line.strip().partition('=')
            frame[key] = value
            if key=='progress':
                write_json(progressfile,{'at':time.time(),'encoder':frame})
                frame = {}
        code = process.wait()
        thread.join(timeout=2)
        if capture:
            terminate_process(capture)
        write_json(HOME/'logs'/(args.run_id+'-exit.json'),{'at':time.time(),'code':code,'capture_code':capture.returncode if capture else None,'ended_by':ended.get('by'),'platform_receipt':'unknown','preview_file':preview})
        return code
    finally:
        if timer:
            timer.cancel()
        if stopper:
            stopper.cancel()
        terminate_process(process)
        if relay:
            relay.close()
        if capture:
            terminate_process(capture)


def launch(args):
    init_home()
    state = read_json(STATE,{'runs':[]})
    if any(owned(run)for run in state['runs']):
        raise ValueError('An ano-stream process is already running; use status or down first')
    args.run_id = str(int(time.time()))+'-'+uuid.uuid4().hex[:8]
    if getattr(args,'capture','window')=='tab':
        with socket.socket()as probe:
            probe.bind(('127.0.0.1',0))
            args.tab_port = probe.getsockname()[1]
        args.tab_token = token_source.token_urlsafe(18)
    plan,_,_ = inspect_plan(args,publish=args.mode=='up')
    if args.dry_run:
        return plan
    if args.mode=='up' and not args.live:
        raise ValueError('Publishing requires --live. Inspect --dry-run first.')
    requests = HOME/'requests'
    requests.mkdir(exist_ok=True)
    write_json(requests/(args.run_id+'.json'),vars(args))
    kwargs = {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS} if os.name=='nt' else {'start_new_session':True}
    with (HOME/'logs'/(args.run_id+'-worker.log')).open('ab')as log:
        process = subprocess.Popen([sys.executable,str(ROOT/'bin/ano-stream'),'_worker','--run-id',args.run_id],
                   stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,**kwargs)
    run = {'id':args.run_id,'pid':process.pid,'birth':process_birth(process.pid),'started':time.time(),
           'mode':args.mode,'plan':plan,'worker_log':str(HOME/'logs'/(args.run_id+'-worker.log'))}
    if not run['birth']:
        process.terminate()
        raise ValueError('Could not establish child process identity')
    write_json(STATE,{'runs':[run]})
    time.sleep(.5)
    if process.poll() is not None:
        raise ValueError('Worker exited during startup; inspect '+run['worker_log'])
    result = {'worker_started':True,'run':run,'platform_receipt':'unknown; not proof of live broadcast'}
    if plan.get('tab_url'):
        result['tab_url'] = plan['tab_url']
        result['next_step'] = 'Open tab_url in the browser profile that holds the game (a private window needs it opened there), press the button and choose the game tab. Nothing is sent until you do.'
    return result


def status():
    state = read_json(STATE,{'runs':[]})
    for run in state['runs']:
        run['running'] = owned(run)
        run['progress'] = read_json(HOME/'logs'/(run['id']+'-progress.json'))
        run['exit'] = read_json(HOME/'logs'/(run['id']+'-exit.json'))
    return state


def main():
    if os.name!='nt':
        os.umask(0o077)
    parser = argparse.ArgumentParser(prog='ano-stream',description=__doc__)
    sub = parser.add_subparsers(dest='mode',required=True)
    sub.add_parser('init')
    device = sub.add_parser('devices')
    device.add_argument('--backend',choices=['auto','macos','windows'],default='auto')
    key = sub.add_parser('key')
    key.add_argument('operation',choices=['set'])
    key.add_argument('destination')
    for name in ('up','preview'):
        item = sub.add_parser(name)
        item.add_argument('--window',help='Window text to match (app name and title); selects window capture. Without --window or --pick the default is browser tab sharing')
        item.add_argument('--pick',action='store_true',help='macOS: choose any window or screen in the system sharing picker (like Meet)')
        item.add_argument('--guard',help='With --pick: the shared window title must contain this text (for example your game page); another tab shows black and silence instead')
        item.add_argument('--no-audio',dest='audio',action='store_false',help='send silence instead of the shared app or tab audio')
        item.add_argument('--window-id',type=int,help='Pin one window id (from devices); the window must also match --window. Use when several windows of one app share a title.')
        item.add_argument('--backend',choices=['auto','macos','windows'],default='auto')
        item.add_argument('--to',default='youtube')
        item.add_argument('--capture',choices=['window','screen-crop','tab'],default=None,
                          help='tab = share from the browser like Google Meet: a tab (its picture and sound only), a window or the entire screen (default); window = macOS ScreenCaptureKit single-window capture (default when --window or --pick is given); screen-crop = legacy whole-display grab cropped to the window')
        item.add_argument('--fps',type=int,choices=range(1,61),default=60,metavar='1..60')
        item.add_argument('--resolution')
        item.add_argument('--bitrate')
        item.add_argument('--cbr',action='store_true',help='macOS: true constant bitrate with x264 (CPU) so a still page still sends the full rate; VideoToolbox cannot pad')
        item.add_argument('--max-hours',type=float,default=3.0,help='Auto-stop a live broadcast after this many hours (default 3; 0 = no limit)')
        item.add_argument('--dry-run',action='store_true')
        item.add_argument('--live',action='store_true',help='Publish to the selected destinations (up only)')
        item.add_argument('--seconds',type=int,choices=range(1,61),default=3,metavar='1..60')
    sub.add_parser('status').add_argument('--json',action='store_true')
    sub.add_parser('down')
    internal = sub.add_parser('_worker',help=argparse.SUPPRESS)
    internal.add_argument('--run-id',required=True)
    args = parser.parse_args()
    if args.mode in ('up','preview') and args.capture is None:
        args.capture = 'window' if args.pick or args.window else 'tab'
    try:
        if args.mode=='init':
            init_home()
            result = {'config':str(CONFIG),'keys_dir':str(HOME/'keys'),'state_preserved':True}
        elif args.mode=='key':
            init_home()
            cfg = read_json(CONFIG)
            if args.destination not in cfg or not cfg[args.destination].get('key_file'):
                raise ValueError('Destination does not have a key_file')
            value = getpass.getpass('Stream key (hidden): ').strip()
            if not value or re.search(r'[\s|\[\]\\]',value):
                raise ValueError('Invalid key')
            path = key_path(cfg[args.destination]['key_file'])
            path.write_text(value+'\n',encoding='utf-8')
            if os.name!='nt':
                path.chmod(0o600)
            result = {'key_saved':args.destination}
        elif args.mode=='devices':
            result = windows(backend_name(args.backend))
        elif args.mode in ('up','preview'):
            result = launch(args)
        elif args.mode=='status':
            result = status()
        elif args.mode=='down':
            state = read_json(STATE,{'runs':[]})
            result = {'stopped':[r['pid']for r in state['runs']if stop_owned(r)],'inference_untouched':True}
        else:
            if not re.fullmatch(r'[0-9]+-[a-f0-9]{8}',args.run_id):
                raise ValueError('Invalid run id')
            return worker(args)
        print(json.dumps(result,indent=2))
        return 0
    except (ValueError,OSError,subprocess.SubprocessError)as error:
        print(json.dumps({'error':str(error)}),file=sys.stderr)
        return 2

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stream', ROOT/'src/ano_stream.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for attr,value in [('HOME',home),('CONFIG',home/'destinations.json'),('STATE',home/'state.json')]:
            p = patch.object(s,attr,value)
            p.start()
            self.addCleanup(p.stop)
        s.init_home()

    def test_init_keeps_state_and_user_destinations(self):
        s.write_json(s.STATE,{'runs':[{'pid':123,'birth':'old'}]})
        s.write_json(s.CONFIG,{'custom':{'url':'rtmp://localhost/app'}})
        s.init_home()
        self.assertEqual(s.read_json(s.STATE)['runs'][0]['pid'],123)
        self.assertIn('custom',s.read_json(s.CONFIG))

    def test_backup_playpath_and_all_secrets_redacted(self):
        cfg = {'youtube':{'url':'rtmp://b.rtmp.youtube.com/live2?backup=1','key_file':'youtube'},
               'twitch':{'url':'rtmps://live.twitch.tv/app','key_file':'twitch'}}
        for name in cfg:
            s.key_path(name).write_text(name+'-secret-value')
        targets,missing,secrets = s.destinations(cfg,'youtube,twitch')
        self.assertEqual(targets[0][2],'rtmp://b.rtmp.youtube.com/live2/youtube-secret-value?backup=1')
        self.assertEqual(missing,[])
        out = s.redact('|'.join(t[2]for t in targets)+' twitch-secret-value',secrets)
        self.assertNotIn('secret-value',out)
        self.assertNotIn('live.twitch.tv',out)

    def test_dry_run_missing_credentials_not_claimed_ready(self):
        targets,missing,_ = s.destinations(s.DEFAULT_DESTINATIONS,'youtube,twitch',True)
        self.assertEqual(missing,['youtube','twitch'])
        with self.assertRaisesRegex(ValueError,'Missing key'):
            s.destinations(s.DEFAULT_DESTINATIONS,'youtube,twitch')
        self.assertEqual(len(targets),2)

    def test_key_file_cannot_escape_private_directory(self):
        with self.assertRaises(ValueError):
            s.key_path('../token')

    def test_shared_settings_never_depend_on_destination_order(self):
        targets = [('a',{'resolution':[1280,720],'bitrate':'6000k'},''),
                   ('b',{'resolution':[1920,1080],'bitrate':'8000k'},'')]
        args = argparse.Namespace(resolution=None,bitrate=None)
        with self.assertRaisesRegex(ValueError,'resolution'):
            s.common_settings(targets,args)
        args.resolution='1280x720'
        with self.assertRaisesRegex(ValueError,'bitrate'):
            s.common_settings(targets,args)
        args.bitrate='6000k'
        self.assertEqual(s.common_settings(targets,args),([1280,720],'6000k'))
        self.assertEqual(s.common_settings(list(reversed(targets)),args),([1280,720],'6000k'))

    def test_windows_multi_has_mapping_one_encoder_and_exact_window(self):
        targets = [('a',{},'rtmp://localhost/a'),('b',{},'rtmp://localhost/b')]
        cmd = s.make_command('ffmpeg','windows',{'id':123},targets)
        self.assertIn('hwnd=123',cmd)
        self.assertEqual(cmd.count('-c:v'),1)
        self.assertIn('h264_nvenc',cmd)
        self.assertEqual(cmd.count('-map'),2)
        self.assertEqual(cmd[cmd.index('-g')+1],'120')
        self.assertIn('scale=1920:1080',cmd[cmd.index('-vf')+1])
        self.assertIn('tee',cmd)
        self.assertIn('-use_fifo',cmd)
        self.assertNotIn('desktop',cmd)

    def test_preview_has_bounded_file_and_no_network_target(self):
        cmd = s.make_command('ffmpeg','windows',{'id':123},[],output=(Path(self.tmp.name)/'preview.mkv',3))
        self.assertEqual(cmd[cmd.index('-t')+1],'3')
        self.assertEqual(cmd[-2],'matroska')
        self.assertFalse(any('rtmp://'in item or 'rtmps://'in item for item in cmd))

    def test_macos_uses_hardware_and_verified_geometry(self):
        cmd = s.make_command('ffmpeg','macos',{},[('youtube',{},'rtmp://localhost/test')],mac_crop=[10,20,3840,2160])
        self.assertEqual(cmd[cmd.index('-allow_sw')+1],'0')
        self.assertIn('crop=3840:2160:10:20',cmd[cmd.index('-vf')+1])
        self.assertIn('fps=60',cmd[cmd.index('-vf')+1])
        with self.assertRaisesRegex(ValueError,'verified display geometry'):
            s.make_command('ffmpeg','macos',{},[])

    def test_window_selection_filters_small_and_wrong_titles(self):
        items = [{'owner':'Edge','name':name,'w':w,'h':h}for name,w,h in [('game',1200,800),('game popup',100,100),('private',2000,1500)]]
        self.assertEqual(s.choose_window(items,'game')['name'],'game')
        with self.assertRaisesRegex(ValueError,'No visible'):
            s.choose_window(items,'missing')

    def test_down_never_kills_reused_pid(self):
        with patch.object(s,'process_birth',return_value='new'),patch.object(s.os,'killpg',create=True)as kill:
            self.assertFalse(s.stop_owned({'pid':123,'birth':'old'}))
            kill.assert_not_called()

    def test_real_process_identity(self):
        self.assertTrue(s.process_birth(__import__('os').getpid()))

    def test_encoder_that_ignores_termination_is_reaped(self):
        command = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
        child = subprocess.Popen([sys.executable,'-u','-c',command],stdout=subprocess.PIPE,text=True)
        self.addCleanup(lambda:child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(),'ready')
        start = time.monotonic()
        s.terminate_process(child,grace=.1)
        self.assertIsNotNone(child.poll())
        self.assertLess(time.monotonic()-start,3)
        child.stdout.close()

    def test_fit_size_keeps_window_aspect_and_even_dimensions(self):
        self.assertEqual(s.fit_size({'w':1000,'h':1000},(1920,1080)),[1080,1080])
        for w,h in [(2204,1122),(1001,777),(333,999)]:
            fitted = s.fit_size({'w':w,'h':h},(1920,1080))
            self.assertTrue(all(v%2==0 for v in fitted))
            self.assertLessEqual(fitted[0],1920)
            self.assertLessEqual(fitted[1],1080)

    def test_macos_window_capture_reads_raw_frames_from_stdin_not_the_screen(self):
        cmd = s.make_command('ffmpeg','macos',{'id':9},[('a',{},'rtmp://localhost/a')],window_size=(1920,976))
        self.assertIn('pipe:0',cmd)
        self.assertIn('rawvideo',cmd)
        self.assertEqual(cmd[cmd.index('-video_size')+1],'1920x976')
        self.assertNotIn('avfoundation',cmd)
        self.assertFalse(any('Capture screen' in item or 'crop=' in item for item in cmd))
        self.assertIn('h264_videotoolbox',cmd)
        self.assertEqual(cmd[cmd.index('-colorspace')+1],'bt709')

    def test_macos_screen_crop_is_still_available(self):
        cmd = s.make_command('ffmpeg','macos',{'id':9},[('a',{},'rtmp://localhost/a')],mac_crop=(0,60,4408,2244))
        self.assertIn('Capture screen 0',cmd)
        self.assertNotIn('pipe:0',cmd)

    def test_window_id_pins_one_of_several_windows_with_the_same_title(self):
        items = [{'id':1,'owner':'Arc','name':'AGENT NUMBER ONE','w':2000,'h':1300},
                 {'id':2,'owner':'Arc','name':'AGENT NUMBER ONE','w':1000,'h':800}]
        self.assertEqual(s.choose_window(items,'Arc AGENT NUMBER ONE')['id'],1)
        self.assertEqual(s.choose_window(items,'Arc AGENT NUMBER ONE',2)['id'],2)
        with self.assertRaisesRegex(ValueError,'id 3'):
            s.choose_window(items,'Arc AGENT NUMBER ONE',3)
        with self.assertRaisesRegex(ValueError,'id 2'):
            s.choose_window(items,'Safari',2)

    def test_pick_mode_needs_no_window_and_never_runs_the_screen_crop_path(self):
        args = argparse.Namespace(window=None,pick=True,backend='macos',capture='window',window_id=None,to='youtube',
                                  fps=60,resolution='1920x1080',bitrate=None,dry_run=True,seconds=3,mode='preview',run_id='x')
        with patch.object(s,'ffmpeg_path',return_value='ffmpeg'),patch.object(s,'capture_tool',return_value=Path('/x/window-capture')),\
             patch.object(s,'helper',return_value=Path('/x/os-helper')),patch.object(s.subprocess,'check_output',return_value='{"screen_recording": true}'):
            plan,cmd,_ = s.inspect_plan(args,publish=False)
        self.assertEqual(plan['capture_command'][:2],[str(Path('/x/window-capture')),'--pick'])
        self.assertNotIn('--match',plan['capture_command'])
        self.assertIn('pipe:0',cmd)
        args.capture='screen-crop'
        with self.assertRaisesRegex(ValueError,'--pick needs'):
            s.inspect_plan(args,publish=False)
        args.capture,args.pick,args.window = 'window',False,None
        with self.assertRaisesRegex(ValueError,'--window TEXT'):
            s.inspect_plan(args,publish=False)
    def test_cbr_on_macos_uses_x264_padding_not_videotoolbox(self):
        cmd = s.make_command('ffmpeg','macos',{'id':9},[('a',{},'rtmp://localhost/a')],bitrate='17000k',window_size=(1920,1080),cbr=True)
        self.assertEqual(cmd[cmd.index('-c:v')+1],'libx264')
        self.assertNotIn('h264_videotoolbox',cmd)
        self.assertNotIn('-allow_sw',cmd)
        self.assertEqual(cmd[cmd.index('-minrate')+1],'17000k')
        self.assertEqual(cmd[cmd.index('-maxrate')+1],'17000k')
        self.assertIn('nal-hrd=cbr',cmd[cmd.index('-x264-params')+1])
        self.assertIn('keyint=120',cmd[cmd.index('-x264-params')+1])
        plain = s.make_command('ffmpeg','macos',{'id':9},[('a',{},'rtmp://localhost/a')],window_size=(1920,1080))
        self.assertIn('h264_videotoolbox',plain)
        self.assertNotIn('-x264-params',plain)

    def test_window_audio_reads_a_pipe_instead_of_silence(self):
        cmd = s.make_command('ffmpeg','macos',{'id':9},[('a',{},'rtmp://localhost/a')],window_size=(1920,1080),audio=True)
        self.assertNotIn('anullsrc=channel_layout=stereo:sample_rate=44100',cmd)
        i = cmd.index('pipe:'+s.AUDIO_FD)
        self.assertEqual(cmd[i-7:i],['-f','f32le','-ar','48000','-ac','2','-i'])
        self.assertIn('1:a:0',cmd)
        silent = s.make_command('ffmpeg','macos',{'id':9},[('a',{},'rtmp://localhost/a')],window_size=(1920,1080))
        self.assertIn('anullsrc=channel_layout=stereo:sample_rate=44100',silent)

    def test_guard_and_audio_reach_the_capture_tool_only_where_valid(self):
        args = argparse.Namespace(window=None,pick=True,backend='macos',capture='window',window_id=None,to='youtube',guard='AGENT NUMBER ONE',
                                  fps=60,resolution='1920x1080',bitrate=None,dry_run=True,seconds=3,mode='preview',run_id='x',audio=True)
        with patch.object(s,'ffmpeg_path',return_value='ffmpeg'),patch.object(s,'capture_tool',return_value=Path('/x/window-capture')),\
             patch.object(s,'helper',return_value=Path('/x/os-helper')),patch.object(s.subprocess,'check_output',return_value='{"screen_recording": true}'):
            plan,cmd,_ = s.inspect_plan(args,publish=False)
            capture = plan['capture_command']
            self.assertEqual(capture[capture.index('--guard')+1],'AGENT NUMBER ONE')
            self.assertEqual(capture[capture.index('--audio-fd')+1],s.AUDIO_FD)
            self.assertIn('pipe:'+s.AUDIO_FD,cmd)
            args.audio = False
            plan,cmd,_ = s.inspect_plan(args,publish=False)
            self.assertNotIn('--audio-fd',plan['capture_command'])
            self.assertIn('anullsrc=channel_layout=stereo:sample_rate=44100',cmd)
            self.assertIn('Silent',plan['audio'])
            args.pick,args.window,args.guard = False,'Game',None
            args.guard = 'x'
            with patch.object(s,'windows',return_value=[{'id':1,'owner':'App','name':'Game','x':0,'y':0,'w':1000,'h':800}]):
                with self.assertRaisesRegex(ValueError,'--guard needs --pick'):
                    s.inspect_plan(args,publish=False)

    def test_auto_stop_fires_after_the_limit_and_zero_means_no_limit(self):
        self.assertIsNone(s.start_auto_stop(0,lambda:None))
        fired = threading.Event()
        timer = s.start_auto_stop(0.00002,fired.set)   # ~0.07 s
        self.addCleanup(timer.cancel)
        self.assertTrue(fired.wait(3))

    def test_max_hours_and_cbr_are_validated(self):
        args = argparse.Namespace(window='Game',pick=False,backend='macos',capture='window',window_id=None,to='youtube',fps=60,
                                  resolution='1920x1080',bitrate=None,dry_run=True,seconds=3,mode='up',run_id='x',max_hours=99,cbr=False)
        with patch.object(s,'windows',return_value=[{'id':1,'owner':'App','name':'Game','x':0,'y':0,'w':1000,'h':800}]),\
             patch.object(s,'ffmpeg_path',return_value='ffmpeg'),patch.object(s,'capture_tool',return_value=Path('/x/wc')),\
             patch.object(s,'helper',return_value=Path('/x/h')),patch.object(s.subprocess,'check_output',return_value='{"screen_recording": true}'):
            with self.assertRaisesRegex(ValueError,'--max-hours'):
                s.inspect_plan(args)
            args.max_hours = 3
            plan,cmd,_ = s.inspect_plan(args)
            self.assertEqual(plan['auto_stop_hours'],3.0)
            args.backend,args.cbr = 'windows',True
            with self.assertRaisesRegex(ValueError,'--cbr is only for macOS'):
                s.inspect_plan(args)


if __name__=='__main__':
    unittest.main()

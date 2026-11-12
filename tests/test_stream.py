import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
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


if __name__=='__main__':
    unittest.main()

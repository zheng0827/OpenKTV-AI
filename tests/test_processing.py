import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openktv_ai.config import AppSettings, load_settings
from openktv_ai.processing import (
    build_demucs_command,
    build_mix_filter,
    demucs_weights_ready,
    ensure_demucs_weights,
    _export_instrumental_track,
    resolve_device,
)


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings(
            base_dir=Path('/tmp/app'),
            templates_dir=Path('/tmp/app/templates'),
            songs_dir=Path('/tmp/app/ktv_songs'),
            temp_base_dir=Path('/tmp/app/temp_processing'),
            ffmpeg_dir=Path('/tmp/app/ffmpeg/bin'),
            yt_dlp_path=Path('/tmp/app/yt-dlp.exe'),
            host='0.0.0.0',
            port=5000,
            secret_key='ktv_secret',
            demucs_cache_dir=Path('/tmp/app/model_cache/demucs'),
            demucs_model='htdemucs_ft',
            separator_stems=2,
            device_preference='auto',
            mix_mode='pseudo-spatial',
            stereo_balance_left_original=0.65,
            stereo_balance_right_original=0.35,
            pseudo_delay_ms=12,
            pseudo_reflection_gain=0.12,
            pseudo_original_gain=1.0,
            pseudo_accompaniment_gain=0.95,
            pseudo_left_original=0.72,
            pseudo_left_accompaniment=0.28,
            pseudo_right_original=0.28,
            pseudo_right_accompaniment=0.72,
        )

    @patch('openktv_ai.config.load_dotenv')
    def test_load_settings_reads_project_dotenv_without_overriding_system_values(self, mock_load_dotenv):
        load_settings(Path('/tmp/app'))
        mock_load_dotenv.assert_called_once_with(
            dotenv_path=Path('/tmp/app/.env'),
            override=False,
        )

    def test_build_demucs_command_two_stems(self):
        command = build_demucs_command(Path('/tmp/in.mp4'), Path('/tmp/out'), 'htdemucs_ft', 2, 'cpu')
        self.assertIn('--two-stems', command)
        self.assertIn('vocals', command)

    def test_build_demucs_command_four_stems(self):
        command = build_demucs_command(Path('/tmp/in.mp4'), Path('/tmp/out'), 'htdemucs_ft', 4, 'cuda')
        self.assertNotIn('--two-stems', command)

    @patch('openktv_ai.processing._run_command')
    def test_export_instrumental_track_creates_aac_sidecar(self, mock_run_command):
        _export_instrumental_track(
            Path('/tmp/accompaniment.wav'),
            Path('/tmp/song.instrumental.m4a'),
            'pseudo-spatial',
            self.settings,
        )
        command = mock_run_command.call_args.args[0]
        self.assertEqual(command[0], 'ffmpeg')
        self.assertIn('aac', command)
        self.assertIn('-filter_complex', command)
        self.assertTrue(any('equalizer=f=180:t=q:w=0.8:g=-1.2' in value for value in command))
        self.assertIn('[mastered_acc]', command)
        self.assertEqual(Path(command[-1]), Path('/tmp/song.instrumental.m4a'))

    def test_build_mix_filter_modes(self):
        legacy = build_mix_filter('legacy', self.settings)
        balance = build_mix_filter('stereo-balance', self.settings)
        pseudo = build_mix_filter('pseudo-spatial', self.settings)
        self.assertIn('join=inputs=2', legacy)
        self.assertIn('pan=stereo', balance)
        self.assertIn('adelay=', pseudo)
        self.assertIn('[1:a]anull[vocals]', pseudo)
        self.assertIn('[vocals][acc]amix=inputs=2:normalize=0,volume=0.5[a]', pseudo)
        self.assertIn('highpass=f=55', pseudo)
        self.assertIn('equalizer=f=180:t=q:w=0.8:g=-1.2', pseudo)
        self.assertIn('[acc_dry][acc_er]amix=inputs=2:normalize=0,highpass=f=55', pseudo)

    @patch('openktv_ai.processing._is_cuda_available', return_value=True)
    def test_resolve_device_prefers_cuda_when_auto(self, _mock_available):
        self.assertEqual(resolve_device('auto'), 'cuda')

    @patch('openktv_ai.processing._is_cuda_available', return_value=False)
    def test_resolve_device_falls_back_to_cpu(self, _mock_available):
        self.assertEqual(resolve_device('cuda'), 'cpu')

    @patch('openktv_ai.processing._demucs_required_cache_files', return_value=['a.th', 'b.th'])
    @patch('pathlib.Path.exists')
    def test_demucs_weights_ready_true(self, mock_exists, _mock_required):
        fake_torch = SimpleNamespace(hub=SimpleNamespace(get_dir=lambda: '/tmp/torch'))
        mock_exists.side_effect = [True, True]
        with patch.dict('sys.modules', {'torch': fake_torch}):
            self.assertTrue(demucs_weights_ready('htdemucs_ft'))

    @patch('openktv_ai.processing.demucs_weights_ready', side_effect=[False, True])
    def test_ensure_demucs_weights_downloads_when_missing(self, _mock_ready):
        fake_get_model = Mock()
        fake_pretrained = SimpleNamespace(get_model=fake_get_model)
        fake_demucs = SimpleNamespace(pretrained=fake_pretrained)
        with patch.dict('sys.modules', {'demucs': fake_demucs, 'demucs.pretrained': fake_pretrained}):
            ensure_demucs_weights('htdemucs_ft', log_cb=lambda *_args, **_kwargs: None)
        fake_get_model.assert_called_once_with('htdemucs_ft')


if __name__ == '__main__':
    unittest.main()

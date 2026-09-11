import unittest
from pathlib import Path
from unittest.mock import patch

from openktv_ai.config import AppSettings
from openktv_ai.processing import build_demucs_command, build_mix_filter, resolve_device


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

    def test_build_demucs_command_two_stems(self):
        command = build_demucs_command(Path('/tmp/in.mp4'), Path('/tmp/out'), 'htdemucs_ft', 2, 'cpu')
        self.assertIn('--two-stems', command)
        self.assertIn('vocals', command)

    def test_build_demucs_command_four_stems(self):
        command = build_demucs_command(Path('/tmp/in.mp4'), Path('/tmp/out'), 'htdemucs_ft', 4, 'cuda')
        self.assertNotIn('--two-stems', command)

    def test_build_mix_filter_modes(self):
        legacy = build_mix_filter('legacy', self.settings)
        balance = build_mix_filter('stereo-balance', self.settings)
        pseudo = build_mix_filter('pseudo-spatial', self.settings)
        self.assertIn('join=inputs=2', legacy)
        self.assertIn('pan=stereo', balance)
        self.assertIn('adelay=', pseudo)
        self.assertIn('alimiter=limit=0.95', pseudo)

    @patch('openktv_ai.processing._is_cuda_available', return_value=True)
    def test_resolve_device_prefers_cuda_when_auto(self, _mock_available):
        self.assertEqual(resolve_device('auto'), 'cuda')

    @patch('openktv_ai.processing._is_cuda_available', return_value=False)
    def test_resolve_device_falls_back_to_cpu(self, _mock_available):
        self.assertEqual(resolve_device('cuda'), 'cpu')


if __name__ == '__main__':
    unittest.main()

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openktv_ai.config import AppSettings
from openktv_ai.library import find_intro_skip_seconds, parse_first_lyric_time
from openktv_ai.processing import (
    build_demucs_command,
    build_mix_filter,
    demucs_weights_ready,
    ensure_demucs_weights,
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
            separator_stems=4,
            device_preference='auto',
            mix_mode='pseudo-spatial',
            pseudo_delay_ms=12,
            pseudo_reflection_gain=0.12,
            pseudo_reverb_room=0.45,
            pseudo_reverb_damping=0.35,
            pseudo_backing_gain=0.9,
            intro_skip_lead_seconds=5.0,
            download_retry_count=2,
            library_index_path=Path('/tmp/app/ktv_songs/library_index.csv'),
        )

    def test_build_demucs_command_two_stems(self):
        command = build_demucs_command(Path('/tmp/in.mp4'), Path('/tmp/out'), 'htdemucs_ft', 2, 'cpu')
        self.assertIn('--two-stems', command)
        self.assertIn('vocals', command)

    def test_build_demucs_command_four_stems(self):
        command = build_demucs_command(Path('/tmp/in.mp4'), Path('/tmp/out'), 'htdemucs_ft', 4, 'cuda')
        self.assertNotIn('--two-stems', command)

    def test_build_mix_filter_pseudo_only(self):
        pseudo = build_mix_filter('legacy', self.settings)
        self.assertIn('vocal_mono', pseudo)
        self.assertIn('aecho=', pseudo)
        self.assertNotIn('equalizer=', pseudo)

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


class LibraryTests(unittest.TestCase):
    def test_parse_first_lyric_time(self):
        path = Path('/tmp/test-first-line.lrc')
        path.write_text('%12.340 18.520 只是我回憶的音樂盒\n', encoding='utf-8')
        self.assertEqual(parse_first_lyric_time(path), 12.34)

    def test_find_intro_skip_seconds(self):
        songs_dir = Path('/tmp/ktv-test-songs')
        songs_dir.mkdir(parents=True, exist_ok=True)
        (songs_dir / 'abc.ktv.lrc').write_text('%15.000 20.000 lyric\n', encoding='utf-8')
        skip_to, hide_after = find_intro_skip_seconds(songs_dir, 'abc.mp4', 5.0)
        self.assertEqual(skip_to, 10.0)
        self.assertEqual(hide_after, 15.0)


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AppSettings
from .downloader import download_youtube_video
from .library import fetch_lrclib_lyrics
from .lyrics_alignment import run_alignment_workflow
from .processing import extract_timed_vocals, export_instrumental_track, mix_video_audio
from .runtime import compute_type_for, resolve_device
from .separators import separate_audio
from .transcription import transcribe_segments


@dataclass(frozen=True)
class PipelineArtifacts:
    lyrics_vocals: Path
    dialogue_vocals: Path
    lrc: Path
    dialogue_audit: Path
    detected_language: str


def run_unified_nightingale(
    *,
    settings: AppSettings,
    vocals_wav: Path,
    output_lyrics_vocals: Path,
    output_dialogue_vocals: Path,
    output_lrc: Path,
    output_dialogue_audit_json: Path,
    song_name: str,
    singer: str,
    lyrics_text: str,
    device_preference: str,
    alignment_backend: str,
) -> PipelineArtifacts:
    device = resolve_device(device_preference)
    compute_type = settings.whisper_compute_type if settings.whisper_compute_type != "auto" else compute_type_for(device)
    transcript_segments, detected_language = transcribe_segments(
        vocals_wav=vocals_wav,
        model_name=settings.whisper_model,
        device=device,
        compute_type=compute_type,
        language=(settings.whisper_language or "").strip() or None,
        alignment_backend=alignment_backend,
    )
    aligned_lines, dialogue = run_alignment_workflow(
        lyrics_text=lyrics_text,
        transcript_segments=transcript_segments,
        detected_language=detected_language,
        output_lrc=output_lrc,
        output_dialogue_audit_json=output_dialogue_audit_json,
        song_name=song_name,
        singer=singer,
    )
    extract_timed_vocals(vocals_wav, [{"start": line.start, "end": line.end} for line in aligned_lines], output_lyrics_vocals)
    extract_timed_vocals(vocals_wav, dialogue, output_dialogue_vocals)
    return PipelineArtifacts(output_lyrics_vocals, output_dialogue_vocals, output_lrc, output_dialogue_audit_json, detected_language)


class KTVProcessor:
    def __init__(self, settings: AppSettings, log_cb):
        self.settings = settings
        self.log = log_cb

    def sanitize_filename(self, name: str) -> str:
        return "".join([char for char in name if char not in r'\\/:*?"<>|'])

    def _extract_song_artist(self, title: str) -> tuple[str, str]:
        if " - " in title:
            artist, song = title.split(" - ", 1)
            return song.strip(), artist.strip()
        return title.strip(), ""

    def process_song(self, url: str, manual_title: str, options: dict | None = None) -> bool:
        options = options or {}
        stems = 4 if str(options.get("stems", self.settings.separator_stems)) == "4" else 2
        device_pref = str(options.get("device", self.settings.device_preference)).lower()
        separator_backend = str(options.get("separator_backend", self.settings.separator_backend)).lower()
        alignment_backend = str(options.get("alignment_backend", self.settings.alignment_backend)).lower()
        lyrics_text = (options.get("lyrics_text") or "").strip()
        singer = (options.get("singer") or "").strip()

        job_temp_dir: Path | None = None
        try:
            safe_title = self.sanitize_filename(manual_title)
            self.log(f"目標歌曲：{safe_title}")
            song_name, inferred_singer = self._extract_song_artist(safe_title)
            if not singer:
                singer = inferred_singer

            job_id = str(int(time.time()))
            job_temp_dir = self.settings.temp_base_dir / job_id
            job_temp_dir.mkdir(parents=True, exist_ok=True)

            temp_input = job_temp_dir / "input.mp4"
            temp_output = job_temp_dir / "output.mp4"
            temp_instrumental = job_temp_dir / "instrumental.m4a"
            temp_lrc = job_temp_dir / "lyrics.lrc"
            temp_dialogue_audit = job_temp_dir / "dialogue_audit.json"
            temp_lyrics_vocals = job_temp_dir / "lyrics.vocals.wav"
            temp_dialogue_vocals = job_temp_dir / "dialogue.vocals.wav"

            self.log("步驟 1/8: 下載影片...")
            download_youtube_video(url, temp_input, self.settings.ffmpeg_dir)

            self.log(f"步驟 2/8: 分離人聲/伴奏 ({separator_backend})...")
            separated = separate_audio(
                input_path=temp_input,
                work_dir=job_temp_dir,
                settings=self.settings,
                stems=stems,
                device_preference=device_pref,
                backend=separator_backend,
                log_cb=self.log,
            )

            if not lyrics_text:
                self.log("步驟 3/8: 從 lrclib 取得歌詞...")
                lyrics_text = fetch_lrclib_lyrics(song_name, singer or "") or ""
            else:
                self.log("步驟 3/8: 使用手動提供歌詞...")

            self.log(f"步驟 4-6/8: {alignment_backend} 對齊歌詞、偵測對白、切出 lyrics/dialogue vocals...")
            run_unified_nightingale(
                settings=self.settings,
                vocals_wav=separated.vocals_path,
                output_lyrics_vocals=temp_lyrics_vocals,
                output_dialogue_vocals=temp_dialogue_vocals,
                output_lrc=temp_lrc,
                output_dialogue_audit_json=temp_dialogue_audit,
                song_name=song_name or safe_title,
                singer=singer or "",
                lyrics_text=lyrics_text,
                device_preference=device_pref,
                alignment_backend=alignment_backend,
            )

            self.log("步驟 7/8: 混音 instrumental 與 mp4...")
            export_instrumental_track(separated.accompaniment_path, temp_dialogue_vocals, temp_instrumental, self.settings)
            mix_video_audio(temp_input, temp_lyrics_vocals, temp_dialogue_vocals, separated.accompaniment_path, temp_output, self.settings)

            self.log(f"步驟 8/8: 儲存為 {safe_title}.mp4")
            final = self.settings.songs_dir / f"{safe_title}.mp4"
            if final.exists():
                final = self.settings.songs_dir / f"{safe_title}_{job_id}.mp4"
            final_instrumental = final.with_name(f"{final.stem}.instrumental.m4a")
            final_lyrics_vocals = final.with_name(f"{final.stem}.lyrics.vocals.wav")
            final_dialogue_vocals = final.with_name(f"{final.stem}.dialogue.vocals.wav")
            final_lrc = final.with_name(f"{final.stem}.lrc")
            final_dialogue_audit = final.with_name(f"{final.stem}.dialogue_audit.json")

            shutil.move(str(temp_output), str(final))
            shutil.move(str(temp_instrumental), str(final_instrumental))
            shutil.move(str(temp_lyrics_vocals), str(final_lyrics_vocals))
            shutil.move(str(temp_dialogue_vocals), str(final_dialogue_vocals))
            shutil.move(str(temp_lrc), str(final_lrc))
            if temp_dialogue_audit.exists():
                shutil.move(str(temp_dialogue_audit), str(final_dialogue_audit))
            self.log("✅ 製作完成！已自動同步至歌單。")
            return True
        except subprocess.CalledProcessError as error:
            self.log(f"❌ 執行失敗 (Code {error.returncode})")
            return False
        except Exception as error:
            self.log(f"❌ 錯誤: {error}")
            return False
        finally:
            if job_temp_dir and job_temp_dir.exists():
                shutil.rmtree(job_temp_dir, ignore_errors=True)

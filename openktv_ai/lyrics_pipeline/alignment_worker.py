from __future__ import annotations

import argparse
import json
from pathlib import Path


def run_alignment_worker() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alignment-input", required=True)
    parser.add_argument("--alignment-output", required=True)
    args = parser.parse_args()

    try:
        import whisperx  # pylint: disable=import-outside-toplevel
    except Exception as error:
        raise SystemExit(f"missing whisperx in alignment environment: {error}") from error

    payload = json.loads(Path(args.alignment_input).read_text(encoding="utf-8"))
    model, metadata = whisperx.load_align_model(language_code=payload["language"], device=payload["device"])
    aligned = whisperx.align(
        payload["segments"],
        model,
        metadata,
        whisperx.load_audio(payload["wav"]),
        payload["device"],
        return_char_alignments=True,
    )
    Path(args.alignment_output).write_text(
        json.dumps(aligned.get("segments", []), ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    run_alignment_worker()

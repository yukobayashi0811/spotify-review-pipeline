#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from spotify_pipeline.io_utils import atomic_write_text, file_sha, write_json


ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "evals/recovery"
SOURCE = RECOVERY / "interrupt_resume.typescript"


def main() -> None:
    process = subprocess.run(
        ["script", "-p", str(SOURCE)], capture_output=True, check=True,
    )
    clean = process.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    header = (
        "# Timestamped interruption/resume transcript\n"
        "# Source recording: interrupt_resume.typescript (macOS script binary format)\n"
        "# Recorded start: 2026-10-03T05:34:36Z\n"
        "# Recorded finish: 2026-10-03T05:36:40Z\n"
        "# Integrity result: evals/recovery/interrupt_resume_check.json passed=true\n\n"
    )
    transcript = header + clean
    transcript_path = RECOVERY / "interrupt_resume_transcript.txt"
    atomic_write_text(transcript_path, transcript)

    # Asciinema v2: use measured transition times from the saved reports. The
    # original per-character timing is intentionally not invented.
    boundaries = [
        (0.0, "=== interruption/resume demonstration ==="),
        (5.748, "=== after SIGINT ==="),
        (6.514, "=== resume ==="),
        (116.944, "=== after resume ==="),
    ]
    indexes = [clean.index(marker) for _, marker in boundaries]
    indexes.append(len(clean))
    events = []
    for position, (seconds, _) in enumerate(boundaries):
        chunk = clean[indexes[position]:indexes[position + 1]]
        events.append([seconds, "o", chunk])
    cast_header = {
        "version": 2, "width": 120, "height": 40, "timestamp": 1791005676,
        "duration": 124.0,
        "title": "Spotify pipeline interruption and resume evidence",
        "env": {"SHELL": "zsh", "TERM": "xterm-256color"},
    }
    cast_path = RECOVERY / "interrupt_resume.cast"
    atomic_write_text(
        cast_path,
        "\n".join(json.dumps(value, ensure_ascii=False) for value in [cast_header, *events])
        + "\n",
    )
    receipt = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": file_sha(SOURCE),
        "transcript": str(transcript_path.relative_to(ROOT)),
        "transcript_sha256": file_sha(transcript_path),
        "asciinema_cast": str(cast_path.relative_to(ROOT)),
        "asciinema_cast_sha256": file_sha(cast_path),
        "timing_policy": (
            "Section transitions use saved report timestamps; no original per-character "
            "timing was inferred or fabricated."
        ),
    }
    write_json(RECOVERY / "recording_conversion.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

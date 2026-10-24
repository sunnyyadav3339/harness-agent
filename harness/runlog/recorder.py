"""Screen recording with ffmpeg (gdigrab), segmented so a crash loses at most one segment."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for pattern in (
        rf"{local}\Microsoft\WinGet\Links\ffmpeg.exe",
        rf"{local}\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ):
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


class ScreenRecorder:
    def __init__(self, out_dir: Path, fps: int = 10, segment_s: int = 120) -> None:
        self.out_dir = out_dir
        self.fps = fps
        self.segment_s = segment_s
        self.proc: subprocess.Popen | None = None

    def start(self) -> bool:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log.warning("ffmpeg not found; screen recording disabled")
            return False
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # Burn the wall clock into the corner so the video doubles as timing proof.
        clock = (
            "drawtext=fontfile='C\\:/Windows/Fonts/consola.ttf':"
            "text='%{localtime\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}':"
            "x=w-tw-20:y=20:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=8"
        )
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "gdigrab", "-framerate", str(self.fps), "-draw_mouse", "1", "-i", "desktop",
            "-vf", clock,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-crf", "28",
            "-f", "segment", "-segment_time", str(self.segment_s), "-reset_timestamps", "1",
            str(self.out_dir / "screen_%03d.mp4"),
        ]
        try:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=open(self.out_dir / "ffmpeg.log", "wb"), creationflags=0x08000000,
            )
        except Exception as exc:
            log.error("could not start ffmpeg: %s", exc)
            return False
        log.info("screen recording -> %s", self.out_dir)
        return True

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write(b"q")  # graceful: finalises the current segment
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.proc = None

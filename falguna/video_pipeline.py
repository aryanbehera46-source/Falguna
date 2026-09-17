"""TTT Media/Growth Engine v1 -- video/edit pipeline (Section 16, Pass C).

Assembling already-generated assets (images, an optional voiceover track)
into a real, playable video file is genuinely available here via ffmpeg,
already present in this environment and free/open-source -- this is not
"AI video generation" (there is no free/local model for that; see
`falguna.media_providers`'s honest UnavailableProvider for that gap), it
is mechanical sequencing: a scene list becomes an image slideshow, sized
to the requested aspect ratio, muxed with the audio track, with real
captions written out as a companion .srt file computed from the actual
per-scene durations. If ffmpeg is missing, or a referenced asset file does
not exist, this reports BLOCKED/FAILED honestly -- it never claims a video
was produced without a real, playable file and ffprobe-verified metadata
to back it up.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ASPECT_RATIOS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}


class VideoPipelineError(ValueError):
    pass


@dataclass
class SceneSpec:
    image_path: str
    duration_seconds: float
    caption_text: Optional[str] = None


@dataclass
class VideoPipelineResult:
    status: str  # "COMPLETED" | "BLOCKED" | "FAILED"
    output_path: Optional[str] = None
    captions_path: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _write_srt(scenes: List[SceneSpec], path: Path) -> bool:
    lines = []
    t = 0.0
    index = 1
    wrote_any = False
    for scene in scenes:
        start, end = t, t + scene.duration_seconds
        if scene.caption_text:
            lines.append(str(index))
            lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
            lines.append(scene.caption_text)
            lines.append("")
            index += 1
            wrote_any = True
        t = end
    if wrote_any:
        path.write_text("\n".join(lines), encoding="utf-8")
    return wrote_any


def _srt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _probe(path: Path) -> Optional[Dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=20,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.decode())
    except Exception:
        return None


class VideoPipeline:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)

    def assemble(
        self, content_id: str, scenes: List[SceneSpec], aspect_ratio: str = "9:16",
        audio_path: Optional[str] = None, output_name: Optional[str] = None,
    ) -> VideoPipelineResult:
        if not _ffmpeg_available():
            return VideoPipelineResult(status="BLOCKED", error="ffmpeg/ffprobe are not available in this environment")
        if not scenes:
            return VideoPipelineResult(status="FAILED", error="no scenes provided")
        if aspect_ratio not in ASPECT_RATIOS:
            return VideoPipelineResult(status="FAILED", error=f"aspect_ratio must be one of {sorted(ASPECT_RATIOS)}")
        for scene in scenes:
            if not Path(scene.image_path).is_file():
                return VideoPipelineResult(status="FAILED", error=f"missing scene asset: {scene.image_path}")
            if scene.duration_seconds <= 0:
                return VideoPipelineResult(status="FAILED", error=f"scene duration must be > 0: {scene.image_path}")
        if audio_path and not Path(audio_path).is_file():
            return VideoPipelineResult(status="FAILED", error=f"missing audio asset: {audio_path}")

        work_dir = self.output_dir / content_id
        work_dir.mkdir(parents=True, exist_ok=True)
        width, height = ASPECT_RATIOS[aspect_ratio]

        concat_list = work_dir / "scenes.txt"
        concat_list.write_text(
            "\n".join(f"file '{Path(s.image_path).resolve()}'\nduration {s.duration_seconds}" for s in scenes)
            + f"\nfile '{Path(scenes[-1].image_path).resolve()}'\n",  # ffmpeg concat quirk: last file needs no trailing duration to not get dropped
            encoding="utf-8",
        )

        output_path = work_dir / (output_name or "assembled.mp4")
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list)]
        if audio_path:
            cmd += ["-i", str(audio_path)]
        cmd += ["-vf", vf, "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio_path:
            cmd += ["-c:a", "aac", "-shortest"]
        cmd += [str(output_path)]

        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            return VideoPipelineResult(status="FAILED", error="ffmpeg assembly timed out")
        if proc.returncode != 0 or not output_path.is_file():
            return VideoPipelineResult(status="FAILED", error=f"ffmpeg assembly failed: {proc.stderr.decode(errors='replace')[-500:]}")

        captions_path = work_dir / "captions.srt"
        has_captions = _write_srt(scenes, captions_path)

        probe = _probe(output_path)
        if not probe:
            return VideoPipelineResult(status="FAILED", error="assembly produced a file but ffprobe could not verify it")
        video_stream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        evidence = {
            "duration_seconds": float(probe["format"].get("duration", 0)),
            "width": video_stream.get("width"), "height": video_stream.get("height"),
            "codec": video_stream.get("codec_name"), "file_size_bytes": int(probe["format"].get("size", 0)),
            "format": probe["format"].get("format_name"), "aspect_ratio": aspect_ratio,
            "scene_count": len(scenes), "has_audio": bool(audio_path), "export_metadata_source": "ffprobe",
        }
        return VideoPipelineResult(
            status="COMPLETED", output_path=str(output_path),
            captions_path=str(captions_path) if has_captions else None, evidence=evidence,
        )

"""TTT Media/Growth Engine v1 -- media asset provider abstraction
(Section 15, Pass C).

Free-first, not tightly coupled to one provider: every asset_type (image,
voice, video, music, editing) is served by a provider with an honest
`kind` -- "local_free" (a real, working, zero-cost capability already in
this environment), "external_api" (a paid/credentialed API, wired in
later, never faked now), or "unavailable" (no capability exists yet; the
honest default). A provider never claims COMPLETED without a real,
inspectable output file -- same evidence-required posture as
`WorkerResult`/`BillingStore.record_payment`.

What is genuinely real in this pass, and why:
  * Images: `LocalPillowImageProvider` actually renders a PNG (background +
    wrapped title/caption text) with Pillow, already a dependency here.
  * Voice: `LocalFliteVoiceProvider` actually synthesizes speech with
    ffmpeg's built-in `flite` source filter (a real, free, offline TTS
    engine bundled into a standard ffmpeg build) -- no paid API, no
    fabricated audio. It probes for real flite support at call time and
    is honestly BLOCKED if this ffmpeg build lacks it.
  * Video (raw AI clip generation from a prompt) and Music (real,
    listenable generated music) have no free/local capability in this
    codebase -- `UnavailableProvider` is the correct, honest default for
    both rather than a synthetic tone dressed up as "music" or a slideshow
    misrepresented as "AI video". Assembling already-generated assets into
    a finished video is a different, real capability -- see
    `falguna/video_pipeline.py`.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .store import StateStore, utcnow

ASSET_TYPES = {"image", "video", "voice", "music", "editing"}
PROVIDER_KINDS = {"local_free", "external_api", "unavailable"}
ASSET_STATUSES = {"COMPLETED", "BLOCKED", "FAILED"}


class MediaProviderError(ValueError):
    pass


class ProviderResult:
    """What every provider returns. Mirrors WorkerResult's contract:
    COMPLETED requires real evidence (at minimum, an output_path that
    exists on disk); BLOCKED/FAILED never do."""

    def __init__(self, status: str, output_path: Optional[str] = None, cost: Optional[float] = None,
                 error: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None):
        if status not in ASSET_STATUSES:
            raise MediaProviderError(f"status must be one of {sorted(ASSET_STATUSES)}")
        if status == "COMPLETED" and not output_path:
            raise MediaProviderError("a provider cannot report COMPLETED without a real output_path")
        self.status = status
        self.output_path = output_path
        self.cost = cost
        self.error = error
        self.evidence = evidence or {}


class MediaProvider:
    name = "provider"
    kind = "unavailable"

    def generate(self, asset_type: str, spec: Dict[str, Any], output_dir: Path) -> ProviderResult:
        raise NotImplementedError


class UnavailableProvider(MediaProvider):
    """The honest default for any asset_type with no free/local capability
    and no configured paid API -- never fabricates output, never silently
    downgrades to a fake placeholder presented as real."""

    name = "unavailable"
    kind = "unavailable"

    def __init__(self, reason: str = "no free/local capability and no paid API configured for this asset type"):
        self.reason = reason

    def generate(self, asset_type: str, spec: Dict[str, Any], output_dir: Path) -> ProviderResult:
        return ProviderResult(status="BLOCKED", error=self.reason, evidence={"asset_type": asset_type, "reason": self.reason})


class LocalPillowImageProvider(MediaProvider):
    """Real, local, free image generation: a solid background with
    wrapped title/caption text -- practical for social posts, thumbnails,
    and placeholder visuals. Not a photorealistic generator; that would be
    an external_api provider, wired in later, never faked here."""

    name = "local_pillow"
    kind = "local_free"

    _PALETTE = [(17, 24, 39), (30, 58, 138), (6, 78, 59), (120, 53, 15), (76, 5, 25)]

    def generate(self, asset_type: str, spec: Dict[str, Any], output_dir: Path) -> ProviderResult:
        if asset_type != "image":
            return ProviderResult(status="BLOCKED", error=f"{self.name} only handles asset_type='image', got {asset_type!r}")
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return ProviderResult(status="BLOCKED", error="Pillow is not installed in this environment")

        text = (spec.get("text") or "").strip()
        if not text:
            return ProviderResult(status="FAILED", error="spec['text'] is required")
        width = int(spec.get("width", 1080))
        height = int(spec.get("height", 1080))
        color_index = abs(hash(spec.get("seed", text))) % len(self._PALETTE)
        background = self._PALETTE[color_index]

        img = Image.new("RGB", (width, height), color=background)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=max(28, width // 18))
        except TypeError:
            font = ImageFont.load_default()
        wrapped = textwrap.fill(text, width=max(10, width // 36))
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center")
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.multiline_text(((width - text_w) / 2, (height - text_h) / 2), wrapped, font=font, fill=(245, 245, 245), align="center")

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = spec.get("filename") or f"image_{abs(hash((text, width, height)))}.png"
        output_path = output_dir / filename
        img.save(output_path, format="PNG")

        return ProviderResult(
            status="COMPLETED", output_path=str(output_path), cost=0.0,
            evidence={"width": width, "height": height, "file_size_bytes": output_path.stat().st_size, "provider": self.name},
        )


def _escape_lavfi_text(text: str) -> str:
    """Escape text for use inside a single-quoted ffmpeg lavfi filter
    option (flite=text='...'). ffmpeg's filtergraph parser treats ':' as
    an option separator, ',' as a filter separator, and '[]' as pad
    labels even *inside* single quotes -- confirmed by direct testing,
    not assumed -- so real voiceover text containing normal punctuation
    ("Tip 1: do X.", "step one, then two") would otherwise silently break
    the filtergraph and fail synthesis entirely."""
    for ch in ("\\", "'", ":", ",", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


def _flite_available() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    try:
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "flite=text='ok'", "-t", "0.1", "-f", "null", "-"],
            capture_output=True, timeout=15,
        )
        return probe.returncode == 0
    except Exception:
        return False


class LocalFliteVoiceProvider(MediaProvider):
    """Real, local, free text-to-speech via ffmpeg's bundled flite source
    filter. Honestly BLOCKED (not a fake silent file) if this ffmpeg build
    was not compiled with flite support -- probed at call time rather than
    assumed, since that varies by build/OS."""

    name = "local_flite"
    kind = "local_free"

    def generate(self, asset_type: str, spec: Dict[str, Any], output_dir: Path) -> ProviderResult:
        if asset_type != "voice":
            return ProviderResult(status="BLOCKED", error=f"{self.name} only handles asset_type='voice', got {asset_type!r}")
        text = (spec.get("text") or "").strip()
        if not text:
            return ProviderResult(status="FAILED", error="spec['text'] is required")
        if not _flite_available():
            return ProviderResult(status="BLOCKED", error="ffmpeg on this system was not built with flite support -- no free local TTS available")

        voice = spec.get("voice", "kal")
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = spec.get("filename") or f"voice_{abs(hash((text, voice)))}.wav"
        output_path = output_dir / filename
        escaped_text = _escape_lavfi_text(text)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", f"flite=text='{escaped_text}':voice={voice}", "-ar", "22050", str(output_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return ProviderResult(status="FAILED", error="ffmpeg/flite synthesis timed out")
        if proc.returncode != 0 or not output_path.exists():
            return ProviderResult(status="FAILED", error=f"ffmpeg/flite synthesis failed: {proc.stderr.decode(errors='replace')[-400:]}")

        duration = _probe_duration_seconds(output_path)
        return ProviderResult(
            status="COMPLETED", output_path=str(output_path), cost=0.0,
            evidence={"voice": voice, "duration_seconds": duration, "file_size_bytes": output_path.stat().st_size, "provider": self.name},
        )


def _probe_duration_seconds(path: Path) -> Optional[float]:
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        info = json.loads(proc.stdout.decode())
        return float(info["format"]["duration"])
    except Exception:
        return None


class MediaProviderRegistry:
    """Routes an asset_type to its configured provider. Free-first
    default: image -> Pillow, voice -> flite, video/music/editing ->
    Unavailable until a real adapter (local or paid) is wired in.
    Callers can override any entry (e.g. to plug in a paid API later)
    without touching anything else in this module."""

    def __init__(self, providers: Optional[Dict[str, MediaProvider]] = None):
        self._providers: Dict[str, MediaProvider] = {
            "image": LocalPillowImageProvider(),
            "voice": LocalFliteVoiceProvider(),
            "video": UnavailableProvider("no free/local AI video-clip generator; configure a paid provider to enable"),
            "music": UnavailableProvider("no free/local original-music generator; configure a paid provider to enable"),
            "editing": UnavailableProvider("use falguna.video_pipeline.VideoPipeline for asset assembly/editing"),
        }
        if providers:
            self._providers.update(providers)

    def set_provider(self, asset_type: str, provider: MediaProvider) -> None:
        if asset_type not in ASSET_TYPES:
            raise MediaProviderError(f"asset_type must be one of {sorted(ASSET_TYPES)}")
        self._providers[asset_type] = provider

    def get(self, asset_type: str) -> MediaProvider:
        if asset_type not in ASSET_TYPES:
            raise MediaProviderError(f"asset_type must be one of {sorted(ASSET_TYPES)}")
        return self._providers[asset_type]


class MediaAssetStore:
    """Persists the real outcome of a provider call against
    `media_assets` -- provider, provider_kind, cost, output_path, status,
    error, all real, never guessed."""

    def __init__(self, store: StateStore, audit: AuditLog, registry: Optional[MediaProviderRegistry] = None, output_root: Optional[Path] = None):
        self.store = store
        self.audit = audit
        self.registry = registry or MediaProviderRegistry()
        self.output_root = Path(output_root) if output_root else Path("/tmp/ttt_media_assets")

    def generate(self, content_id: str, asset_type: str, spec: Dict[str, Any], actor: str = "system") -> Dict[str, Any]:
        if asset_type not in ASSET_TYPES:
            raise MediaProviderError(f"asset_type must be one of {sorted(ASSET_TYPES)}")
        provider = self.registry.get(asset_type)
        result = provider.generate(asset_type, spec, self.output_root / content_id)
        now = utcnow()
        asset_id = self.store.create("media_assets", {
            "content_id": content_id, "asset_type": asset_type, "provider": provider.name, "provider_kind": provider.kind,
            "cost": result.cost, "output_path": result.output_path, "status": result.status, "error": result.error,
            "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("MEDIA_ASSET_GENERATED", {
            "asset_id": asset_id, "content_id": content_id, "asset_type": asset_type,
            "provider": provider.name, "provider_kind": provider.kind, "status": result.status, "actor": actor,
        })
        return self.get(asset_id)

    def get(self, asset_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("media_assets", asset_id)

    def list(self, content_id: Optional[str] = None, asset_type: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if content_id:
            clauses.append("content_id=?")
            params.append(content_id)
        if asset_type:
            clauses.append("asset_type=?")
            params.append(asset_type)
        rows = self.store.list("media_assets", " AND ".join(clauses), tuple(params)) if clauses else self.store.list("media_assets")
        return list(reversed(rows))

    def total_cost(self, content_id: str) -> float:
        return sum((a.get("cost") or 0.0) for a in self.list(content_id=content_id))

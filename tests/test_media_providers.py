"""Tests for the media asset provider abstraction (falguna/media_providers.py).

Image and voice generation are real, free, local capabilities in this
environment (Pillow, ffmpeg+flite) -- these tests exercise the actual
generation, not a mock. Video/music have no free/local capability, so the
registry's defaults for them must honestly report BLOCKED via
UnavailableProvider, never a fabricated success.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.media import BrandStore, ContentStore
from falguna.media_providers import (
    ASSET_TYPES,
    LocalFliteVoiceProvider,
    LocalPillowImageProvider,
    MediaAssetStore,
    MediaProviderError,
    MediaProviderRegistry,
    ProviderResult,
    UnavailableProvider,
)
from falguna.store import StateStore


class ProviderResultTests(unittest.TestCase):
    def test_completed_requires_output_path(self):
        with self.assertRaises(MediaProviderError):
            ProviderResult(status="COMPLETED")

    def test_blocked_and_failed_do_not_require_output_path(self):
        ProviderResult(status="BLOCKED", error="no capability")
        ProviderResult(status="FAILED", error="bad input")

    def test_invalid_status_raises(self):
        with self.assertRaises(MediaProviderError):
            ProviderResult(status="MAYBE")


class UnavailableProviderTests(unittest.TestCase):
    def test_always_blocks_and_never_fabricates(self):
        provider = UnavailableProvider("no capability configured")
        result = provider.generate("video", {}, Path(tempfile.mkdtemp()))
        self.assertEqual(result.status, "BLOCKED")
        self.assertIsNone(result.output_path)
        self.assertIn("no capability", result.error)


class LocalPillowImageProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "assets"
        self.provider = LocalPillowImageProvider()

    def tearDown(self):
        self._tmp.cleanup()

    def test_generates_a_real_png_file(self):
        result = self.provider.generate("image", {"text": "Hello world"}, self.out_dir)
        self.assertEqual(result.status, "COMPLETED")
        self.assertTrue(Path(result.output_path).is_file())
        self.assertGreater(Path(result.output_path).stat().st_size, 0)
        self.assertEqual(result.cost, 0.0)
        self.assertIn("width", result.evidence)
        self.assertIn("height", result.evidence)

    def test_missing_text_fails(self):
        result = self.provider.generate("image", {}, self.out_dir)
        self.assertEqual(result.status, "FAILED")

    def test_wrong_asset_type_blocks(self):
        result = self.provider.generate("voice", {"text": "x"}, self.out_dir)
        self.assertEqual(result.status, "BLOCKED")

    def test_custom_dimensions_respected(self):
        result = self.provider.generate("image", {"text": "Sized", "width": 640, "height": 480}, self.out_dir)
        self.assertEqual(result.evidence["width"], 640)
        self.assertEqual(result.evidence["height"], 480)
        from PIL import Image
        with Image.open(result.output_path) as img:
            self.assertEqual(img.size, (640, 480))


class LocalFliteVoiceProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "assets"
        self.provider = LocalFliteVoiceProvider()

    def tearDown(self):
        self._tmp.cleanup()

    def test_generates_real_audio_with_positive_duration(self):
        result = self.provider.generate("voice", {"text": "This is a real synthesis test."}, self.out_dir)
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertTrue(Path(result.output_path).is_file())
        self.assertGreater(Path(result.output_path).stat().st_size, 0)
        self.assertGreater(result.evidence.get("duration_seconds") or 0, 0)
        self.assertEqual(result.cost, 0.0)

    def test_missing_text_fails(self):
        result = self.provider.generate("voice", {}, self.out_dir)
        self.assertEqual(result.status, "FAILED")

    def test_wrong_asset_type_blocks(self):
        result = self.provider.generate("image", {"text": "x"}, self.out_dir)
        self.assertEqual(result.status, "BLOCKED")

    def test_text_with_filtergraph_special_characters_still_synthesizes(self):
        # Regression: ffmpeg's lavfi parser treats ':', ',', '[', ']' as
        # filtergraph syntax even inside single quotes, so ordinary
        # voiceover text like "Tip 1: do X, then Y." used to crash
        # synthesis outright instead of producing real audio.
        text = "Tip 1: automate testing, Tip 2: use agents [really]. 100% real, no fluff."
        result = self.provider.generate("voice", {"text": text}, self.out_dir)
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertTrue(Path(result.output_path).is_file())
        self.assertGreater(result.evidence.get("duration_seconds") or 0, 0)


class MediaProviderRegistryTests(unittest.TestCase):
    def test_defaults_cover_every_asset_type(self):
        registry = MediaProviderRegistry()
        for asset_type in ASSET_TYPES:
            self.assertIsNotNone(registry.get(asset_type))

    def test_image_and_voice_default_to_local_free(self):
        registry = MediaProviderRegistry()
        self.assertEqual(registry.get("image").kind, "local_free")
        self.assertEqual(registry.get("voice").kind, "local_free")

    def test_video_and_music_default_to_unavailable(self):
        registry = MediaProviderRegistry()
        self.assertEqual(registry.get("video").kind, "unavailable")
        self.assertEqual(registry.get("music").kind, "unavailable")

    def test_unknown_asset_type_raises(self):
        registry = MediaProviderRegistry()
        with self.assertRaises(MediaProviderError):
            registry.get("holograms")

    def test_set_provider_overrides_default(self):
        registry = MediaProviderRegistry()

        class FakeExternalVideoProvider:
            name = "fake_external_video"
            kind = "external_api"

            def generate(self, asset_type, spec, output_dir):
                raise NotImplementedError

        registry.set_provider("video", FakeExternalVideoProvider())
        self.assertEqual(registry.get("video").kind, "external_api")


class MediaAssetStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.audit = AuditLog(root / "audit.jsonl")
        self.assets = MediaAssetStore(self.store, self.audit, output_root=root / "assets")
        brands = BrandStore(self.store, self.audit)
        contents = ContentStore(self.store, self.audit)
        brand_id = brands.create("TTT", actor="Aryan")
        self.content_id_1 = contents.create(brand_id, "First piece", "short_form_video", actor="Aryan")
        self.content_id_2 = contents.create(brand_id, "Second piece", "short_form_video", actor="Aryan")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_generate_image_persists_real_completed_asset(self):
        asset = self.assets.generate(self.content_id_1, "image", {"text": "A real asset"}, actor="system")
        self.assertEqual(asset["status"], "COMPLETED")
        self.assertEqual(asset["provider"], "local_pillow")
        self.assertEqual(asset["provider_kind"], "local_free")
        self.assertTrue(Path(asset["output_path"]).is_file())
        self.assertEqual(asset["cost"], 0.0)

    def test_generate_video_persists_honest_blocked_asset(self):
        asset = self.assets.generate(self.content_id_1, "video", {}, actor="system")
        self.assertEqual(asset["status"], "BLOCKED")
        self.assertIsNone(asset["output_path"])
        self.assertEqual(asset["provider_kind"], "unavailable")
        self.assertIsNotNone(asset["error"])

    def test_unknown_asset_type_raises(self):
        with self.assertRaises(MediaProviderError):
            self.assets.generate(self.content_id_1, "hologram", {}, actor="system")

    def test_list_and_total_cost(self):
        self.assets.generate(self.content_id_1, "image", {"text": "one"}, actor="system")
        self.assets.generate(self.content_id_1, "voice", {"text": "two"}, actor="system")
        self.assets.generate(self.content_id_2, "image", {"text": "three"}, actor="system")
        self.assertEqual(len(self.assets.list(content_id=self.content_id_1)), 2)
        self.assertEqual(len(self.assets.list(asset_type="image")), 2)
        self.assertEqual(self.assets.total_cost(self.content_id_1), 0.0)


if __name__ == "__main__":
    unittest.main()

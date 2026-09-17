"""Tests for the video/edit pipeline (falguna/video_pipeline.py).

Assembly is exercised end-to-end against real ffmpeg -- these produce an
actual playable .mp4 and verify it with ffprobe, not a mocked subprocess.
Failure paths (missing assets, no scenes, bad aspect ratio) must never
report COMPLETED.
"""

import tempfile
import unittest
from pathlib import Path

from falguna.media_providers import LocalPillowImageProvider
from falguna.video_pipeline import ASPECT_RATIOS, SceneSpec, VideoPipeline


class VideoPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pipeline = VideoPipeline(self.root / "video_out")
        self.image_provider = LocalPillowImageProvider()

    def tearDown(self):
        self._tmp.cleanup()

    def _scene_image(self, text):
        result = self.image_provider.generate("image", {"text": text}, self.root / "assets")
        self.assertEqual(result.status, "COMPLETED")
        return result.output_path

    def test_no_scenes_fails(self):
        result = self.pipeline.assemble("c1", [])
        self.assertEqual(result.status, "FAILED")

    def test_missing_image_asset_fails(self):
        result = self.pipeline.assemble("c1", [SceneSpec(image_path="/does/not/exist.png", duration_seconds=1.0)])
        self.assertEqual(result.status, "FAILED")
        self.assertIn("missing scene asset", result.error)

    def test_zero_or_negative_duration_fails(self):
        img = self._scene_image("scene")
        result = self.pipeline.assemble("c1", [SceneSpec(image_path=img, duration_seconds=0)])
        self.assertEqual(result.status, "FAILED")

    def test_invalid_aspect_ratio_fails(self):
        img = self._scene_image("scene")
        result = self.pipeline.assemble("c1", [SceneSpec(image_path=img, duration_seconds=1.0)], aspect_ratio="21:9")
        self.assertEqual(result.status, "FAILED")

    def test_missing_audio_asset_fails(self):
        img = self._scene_image("scene")
        result = self.pipeline.assemble("c1", [SceneSpec(image_path=img, duration_seconds=1.0)], audio_path="/no/audio.wav")
        self.assertEqual(result.status, "FAILED")

    def test_assembles_real_playable_video_with_correct_dimensions(self):
        img1 = self._scene_image("Scene one")
        img2 = self._scene_image("Scene two")
        scenes = [
            SceneSpec(image_path=img1, duration_seconds=1.0, caption_text="Scene one"),
            SceneSpec(image_path=img2, duration_seconds=1.0, caption_text="Scene two"),
        ]
        result = self.pipeline.assemble("c1", scenes, aspect_ratio="16:9")
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertTrue(Path(result.output_path).is_file())
        self.assertGreater(Path(result.output_path).stat().st_size, 0)
        self.assertEqual(result.evidence["width"], ASPECT_RATIOS["16:9"][0])
        self.assertEqual(result.evidence["height"], ASPECT_RATIOS["16:9"][1])
        self.assertGreater(result.evidence["duration_seconds"], 0)
        self.assertEqual(result.evidence["export_metadata_source"], "ffprobe")

    def test_captions_srt_reflects_real_scene_timing(self):
        img1 = self._scene_image("Scene one")
        img2 = self._scene_image("Scene two")
        scenes = [
            SceneSpec(image_path=img1, duration_seconds=1.5, caption_text="First caption"),
            SceneSpec(image_path=img2, duration_seconds=2.0, caption_text="Second caption"),
        ]
        result = self.pipeline.assemble("c1", scenes, aspect_ratio="9:16")
        self.assertEqual(result.status, "COMPLETED", result.error)
        srt = Path(result.captions_path).read_text()
        self.assertIn("First caption", srt)
        self.assertIn("Second caption", srt)
        self.assertIn("00:00:00,000 --> 00:00:01,500", srt)
        self.assertIn("00:00:01,500 --> 00:00:03,500", srt)

    def test_no_caption_text_produces_no_srt(self):
        img = self._scene_image("Scene")
        result = self.pipeline.assemble("c1", [SceneSpec(image_path=img, duration_seconds=1.0)])
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertIsNone(result.captions_path)

    def test_square_aspect_ratio(self):
        img = self._scene_image("Square scene")
        result = self.pipeline.assemble("c1", [SceneSpec(image_path=img, duration_seconds=1.0)], aspect_ratio="1:1")
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertEqual(result.evidence["width"], result.evidence["height"])


if __name__ == "__main__":
    unittest.main()

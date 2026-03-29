import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from tts_file import (
    calculate_tts_cost_usd,
    normalize_model_for_pricing,
    probe_audio_duration_seconds,
)


class TtsCostTests(unittest.TestCase):
    def test_gpt_4o_mini_tts_uses_audio_duration_pricing(self) -> None:
        cost = calculate_tts_cost_usd("hello world", "gpt-4o-mini-tts", 60.0)
        self.assertAlmostEqual(cost, 0.015, places=6)

    def test_snapshot_alias_normalizes_to_model_family(self) -> None:
        self.assertEqual(
            normalize_model_for_pricing("gpt-4o-mini-tts-2025-03-20"),
            "gpt-4o-mini-tts",
        )
        cost = calculate_tts_cost_usd(
            "hello world", "gpt-4o-mini-tts-2025-03-20", 60.0
        )
        self.assertAlmostEqual(cost, 0.015, places=6)

    def test_tts_1_uses_character_pricing(self) -> None:
        cost = calculate_tts_cost_usd("a" * 1_000_000, "tts-1", None)
        self.assertAlmostEqual(cost, 15.0, places=6)

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required")
    def test_probe_wav_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.wav"
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24_000)
                wav_file.writeframes(b"\x00\x00" * 24_000)

            duration = probe_audio_duration_seconds(path, "wav")
            self.assertIsNotNone(duration)
            self.assertAlmostEqual(duration or 0.0, 1.0, places=2)


if __name__ == "__main__":
    unittest.main()

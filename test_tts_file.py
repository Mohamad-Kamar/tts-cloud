import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from tts_file import (
    calculate_tts_cost_usd,
    is_priced_model,
    probe_audio_duration_seconds,
)


class TtsHelpersTests(unittest.TestCase):
    def test_gpt_4o_mini_tts_uses_audio_duration_pricing(self) -> None:
        cost = calculate_tts_cost_usd("gpt-4o-mini-tts", 60.0)
        self.assertAlmostEqual(cost, 0.015, places=6)

    def test_snapshot_alias_is_treated_as_priced_model(self) -> None:
        self.assertTrue(is_priced_model("gpt-4o-mini-tts-2025-03-20"))
        cost = calculate_tts_cost_usd("gpt-4o-mini-tts-2025-03-20", 60.0)
        self.assertAlmostEqual(cost, 0.015, places=6)

    def test_unknown_model_has_no_cost(self) -> None:
        self.assertFalse(is_priced_model("some-other-model"))
        self.assertIsNone(calculate_tts_cost_usd("some-other-model", 60.0))

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

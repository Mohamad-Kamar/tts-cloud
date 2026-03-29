#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "alloy"
DEFAULT_FORMAT = "mp3"
SUPPORTED_PRICING_MODEL_PREFIX = "gpt-4o-mini-tts"
GPT_4O_MINI_TTS_PRICE_USD_PER_MINUTE = 0.015
PCM_SAMPLE_RATE_HZ = 24_000
PCM_BYTES_PER_SAMPLE = 2
PROGRESS_BAR_WIDTH = 28
PROGRESS_MIN_SECONDS = 2.0
PROGRESS_MAX_SECONDS = 180.0
PROGRESS_TICK_SECONDS = 0.1

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a text file into an audio file with an OpenAI-compatible TTS API."
    )
    parser.add_argument("input_file", help="Path to the input .txt file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output audio file path (.mp3 by default)",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=os.getenv("OPENAI_TTS_MODEL", DEFAULT_MODEL),
        help="TTS model to use (default: OPENAI_TTS_MODEL or gpt-4o-mini-tts)",
    )
    parser.add_argument(
        "-v",
        "--voice",
        default=os.getenv("OPENAI_TTS_VOICE", DEFAULT_VOICE),
        help="Voice to use (default: OPENAI_TTS_VOICE or alloy)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI-compatible base URL (default: OPENAI_BASE_URL or https://api.openai.com/v1)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY"),
        help="OpenAI API key (default: OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--format",
        default=os.getenv("OPENAI_TTS_FORMAT", DEFAULT_FORMAT),
        choices=["mp3", "wav", "aac", "flac", "opus", "pcm"],
        help="Audio output format (default: OPENAI_TTS_FORMAT or mp3)",
    )
    return parser.parse_args()


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Input file is empty: {path}")
    return text


def infer_output_path(input_path: Path, explicit: str | None, fmt: str) -> Path:
    if explicit:
        return Path(explicit)
    return input_path.with_suffix(f".{fmt}")


def build_request(
    base_url: str, api_key: str, model: str, voice: str, audio_format: str, text: str
) -> urllib.request.Request:
    payload = json.dumps(
        {
            "model": model,
            "voice": voice,
            "response_format": audio_format,
            "input": text,
        }
    ).encode("utf-8")

    return urllib.request.Request(
        url=f"{base_url.rstrip('/')}/audio/speech",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )


def build_ssl_context() -> ssl.SSLContext | None:
    if certifi is None:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def estimate_duration_seconds(text: str, audio_format: str) -> float:
    words = len(text.split())
    chars = len(text)
    sentences = max(1, sum(text.count(ch) for ch in ".!?"))

    estimate = 1.25 + (words * 0.07) + (chars * 0.0015) + (sentences * 0.08)

    format_factor = {
        "mp3": 1.0,
        "wav": 0.92,
        "aac": 1.02,
        "flac": 1.05,
        "opus": 0.96,
        "pcm": 0.88,
    }.get(audio_format, 1.0)

    estimate *= format_factor
    return max(PROGRESS_MIN_SECONDS, min(estimate, PROGRESS_MAX_SECONDS))


def is_priced_model(model: str) -> bool:
    return model.startswith(SUPPORTED_PRICING_MODEL_PREFIX)


def probe_audio_duration_seconds(path: Path, audio_format: str) -> float | None:
    if audio_format == "pcm":
        # OpenAI PCM output is raw 24kHz, 16-bit, mono samples.
        return path.stat().st_size / (PCM_SAMPLE_RATE_HZ * PCM_BYTES_PER_SAMPLE)

    if shutil.which("ffprobe") is None:
        return None

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    duration_text = completed.stdout.strip()
    if not duration_text:
        return None

    try:
        return float(duration_text)
    except ValueError:
        return None


def calculate_tts_cost_usd(
    model: str, audio_duration_seconds: float | None
) -> float | None:
    if not is_priced_model(model) or audio_duration_seconds is None:
        return None
    return (
        audio_duration_seconds / 60.0
    ) * GPT_4O_MINI_TTS_PRICE_USD_PER_MINUTE


def print_result_summary(
    output_path: Path,
    elapsed: float,
    model: str,
    audio_format: str,
) -> None:
    size_kb = output_path.stat().st_size / 1024
    print(f"Wrote {output_path} ({size_kb:.1f} KiB) in {elapsed:.2f}s")

    audio_duration_seconds = probe_audio_duration_seconds(output_path, audio_format)
    estimated_cost = calculate_tts_cost_usd(model, audio_duration_seconds)
    if estimated_cost is None:
        print(f"TTS cost: unavailable for model {model}")
        return

    print(
        f"TTS cost: ${estimated_cost:.4f} "
        f"(model: {model}, {audio_duration_seconds:.2f}s of audio)"
    )


def render_progress_line(label: str, elapsed: float, estimated_seconds: float) -> str:
    progress = min(elapsed / estimated_seconds, 0.999)
    filled = int(progress * PROGRESS_BAR_WIDTH)
    bar = "=" * filled
    if filled < PROGRESS_BAR_WIDTH:
        bar += ">"
        bar += " " * (PROGRESS_BAR_WIDTH - filled - 1)
    percent = progress * 100
    remaining = max(estimated_seconds - elapsed, 0.0)
    return (
        f"\r[{bar}] {percent:5.1f}% {label} "
        f"{elapsed:0.1f}s ETA {remaining:0.1f}s"
    )


def clear_progress_line() -> None:
    sys.stderr.write("\r" + " " * 120 + "\r")
    sys.stderr.flush()


def start_progress(
    label: str, estimated_seconds: float, enabled: bool
) -> tuple[threading.Event, threading.Thread | None]:
    stop_event = threading.Event()
    if not enabled:
        return stop_event, None

    def run() -> None:
        started = time.perf_counter()
        while not stop_event.is_set():
            elapsed = time.perf_counter() - started
            sys.stderr.write(render_progress_line(label, elapsed, estimated_seconds))
            sys.stderr.flush()
            time.sleep(PROGRESS_TICK_SECONDS)

        clear_progress_line()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop_event, thread


def main() -> int:
    args = parse_args()

    if not args.api_key:
        print("Missing API key. Set OPENAI_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    input_path = Path(args.input_file).expanduser().resolve()
    output_path = (
        infer_output_path(input_path, args.output, args.format).expanduser().resolve()
    )
    text = read_text(input_path)
    estimated_seconds = estimate_duration_seconds(text, args.format)
    request = build_request(
        args.base_url, args.api_key, args.model, args.voice, args.format, text
    )
    ssl_context = build_ssl_context()
    stop_event, progress_thread = start_progress(
        "Converting text to speech", estimated_seconds, sys.stderr.isatty()
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(
            request, timeout=120, context=ssl_context
        ) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                print(body.decode("utf-8", errors="replace"), file=sys.stderr)
                return 1
            output_path.write_bytes(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} {exc.reason}", file=sys.stderr)
        if body:
            print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Request failed: {exc.reason}", file=sys.stderr)
        return 1
    finally:
        stop_event.set()
        if progress_thread is not None:
            progress_thread.join(timeout=1)

    elapsed = time.perf_counter() - started
    print_result_summary(output_path, elapsed, args.model, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

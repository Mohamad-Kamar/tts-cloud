#!/usr/bin/env python3
import argparse
import json
import ipaddress
import os
import shutil
import ssl
import socket
import subprocess
import sys
import threading
import time
import tempfile
import urllib.error
import urllib.request
import urllib.parse
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
DEFAULT_MAX_AUDIO_BYTES = 50 * 1024 * 1024  # 50 MiB

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
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print full server error payloads (may include your input text)",
    )
    parser.add_argument(
        "--allow-internal-base-url",
        action="store_true",
        help="Allow base URLs that resolve to private/loopback IPs (SSRF risk)",
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


def validate_base_url(base_url: str, *, allow_internal: bool) -> str:
    """
    Reduce SSRF risk when using OpenAI-compatible endpoints by:
    - requiring https
    - rejecting URLs with credentials in the authority
    - blocking hostnames that resolve to private/loopback/link-local IPs (unless allowed)
    """

    if not base_url:
        raise ValueError("Missing base-url")
    if "\n" in base_url or "\r" in base_url:
        raise ValueError("Invalid base-url")

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme.lower() != "https":
        raise ValueError("base-url must use https")
    if not parsed.hostname:
        raise ValueError("base-url must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("base-url must not include credentials")

    hostname = parsed.hostname.lower()
    port = parsed.port or 443
    if hostname in {"localhost"}:
        if not allow_internal:
            raise ValueError("base-url resolves to localhost (use --allow-internal-base-url to override)")

    try:
        addrinfo = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"base-url hostname cannot be resolved: {hostname}") from e

    blocked_addrs: list[str] = []
    for info in addrinfo:
        sockaddr = info[-1]
        ip_str = sockaddr[0]
        ip_obj = ipaddress.ip_address(ip_str)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
        ):
            blocked_addrs.append(ip_str)

    if blocked_addrs and not allow_internal:
        raise ValueError(
            "base-url resolves to private/loopback IPs; use --allow-internal-base-url to override "
            f"(resolved: {sorted(set(blocked_addrs))})"
        )
    return base_url


def ensure_safe_output_path(output_path: Path, *, force: bool) -> None:
    output_path_parent = output_path.parent
    output_path_parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not force:
        raise FileExistsError(f"Output file already exists: {output_path} (use --force to overwrite)")
    if output_path.is_dir():
        raise IsADirectoryError(f"Output path is a directory: {output_path}")
    if output_path.is_symlink():
        raise ValueError(f"Refusing to write to symlink output path: {output_path}")


def read_limited_text(file_like, *, max_bytes: int) -> str:
    raw = file_like.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        return text + "\n[truncated]"
    return text


def stream_audio_to_output(response, output_path: Path, *, max_bytes: int) -> None:
    fd, tmp_path = tempfile.mkstemp(prefix=output_path.name + ".", dir=str(output_path.parent))
    try:
        total = 0
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Response too large (>{max_bytes} bytes)")
                f.write(chunk)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


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
    try:
        ensure_safe_output_path(output_path, force=args.force)
    except (FileExistsError, IsADirectoryError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    text = read_text(input_path)
    estimated_seconds = estimate_duration_seconds(text, args.format)
    try:
        base_url = validate_base_url(args.base_url, allow_internal=args.allow_internal_base_url)
    except ValueError as e:
        print(f"Invalid base-url: {e}", file=sys.stderr)
        return 2
    request = build_request(
        base_url, args.api_key, args.model, args.voice, args.format, text
    )
    ssl_context = build_ssl_context()
    stop_event, progress_thread = start_progress(
        "Converting text to speech", estimated_seconds, sys.stderr.isatty()
    )

    try:
        max_audio_bytes = int(
            os.getenv("OPENAI_TTS_MAX_AUDIO_BYTES", str(DEFAULT_MAX_AUDIO_BYTES))
        )
    except ValueError:
        print(
            "Invalid OPENAI_TTS_MAX_AUDIO_BYTES; must be an integer number of bytes.",
            file=sys.stderr,
        )
        return 2
    if max_audio_bytes <= 0:
        print("OPENAI_TTS_MAX_AUDIO_BYTES must be > 0.", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(
            request, timeout=120, context=ssl_context  # nosec B310
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                err_text = read_limited_text(response, max_bytes=8192)
                if args.debug:
                    print(err_text, file=sys.stderr)
                else:
                    print(
                        "Server returned a JSON error payload. Enable --debug to view full details "
                        "(this may include your input text).",
                        file=sys.stderr,
                    )
                return 1

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    content_length_value = int(content_length)
                except ValueError:
                    print(
                        f"Invalid Content-Length header: {content_length}",
                        file=sys.stderr,
                    )
                    return 1
                if content_length_value > max_audio_bytes:
                    print(
                        f"Response too large (Content-Length={content_length_value} bytes)",
                        file=sys.stderr,
                    )
                    return 1

            stream_audio_to_output(response, output_path, max_bytes=max_audio_bytes)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} {exc.reason}", file=sys.stderr)
        if args.debug:
            body_text = read_limited_text(exc, max_bytes=8192)
            if body_text:
                print(body_text, file=sys.stderr)
        else:
            body_preview = read_limited_text(exc, max_bytes=256).strip()
            if body_preview:
                print(
                    "Server error details suppressed. Enable --debug for full payload.",
                    file=sys.stderr,
                )
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

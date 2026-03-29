# openai-tts-file

Small Python CLI for converting a text file into speech with an OpenAI-compatible TTS API.

## Features

- Reads plain UTF-8 text from a file
- Sends it to `audio/speech`
- Writes the returned audio file locally
- Shows an ETA-based progress bar in interactive terminals
- Reports total request time
- Calculates TTS cost from published pricing
  - `gpt-4o-mini-tts`: based on generated audio duration
  - `tts-1` / `tts-1-hd`: based on input character count

## Requirements

- Python 3.11+
- An API key in `OPENAI_API_KEY`
- Optional: `ffprobe` for duration-based cost calculation on compressed formats like `mp3`

## Installation

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Export variables in your shell or load them from your preferred env manager:

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

`OPENAI_BASE_URL` is optional. Leave it unset to use the official OpenAI API, or set it to any OpenAI-compatible endpoint.

## Usage

```bash
python3 tts_file.py sample.txt
python3 tts_file.py sample.txt -o sample.wav --format wav
python3 tts_file.py sample.txt --model gpt-4o-mini-tts --voice alloy
```

## Configuration

The CLI accepts flags directly and also supports env-based defaults:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_TTS_MODEL`
- `OPENAI_TTS_VOICE`
- `OPENAI_TTS_FORMAT`

CLI flags override env values:

- `input_file`: required path to a UTF-8 text file
- `-o, --output`: output audio path; defaults to the input stem plus the chosen format
- `-m, --model`: TTS model to use
- `-v, --voice`: voice name
- `--format`: one of `mp3`, `wav`, `aac`, `flac`, `opus`, `pcm`
- `--base-url`: OpenAI-compatible base URL
- `--api-key`: API key override

## Voice Options

OpenAI's current TTS guide documents these voices for the speech API:

- `alloy`
- `ash`
- `ballad`
- `cedar`
- `coral`
- `echo`
- `fable`
- `marin`
- `nova`
- `onyx`
- `sage`
- `shimmer`
- `verse`

OpenAI recommends `marin` or `cedar` for the best quality on the newer speech models.

## Testing

```bash
python3 -m unittest -v
```

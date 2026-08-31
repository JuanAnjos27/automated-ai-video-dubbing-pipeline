# Language-Agnostic Video Dubbing Pipeline

Automated dubbing pipeline that turns a video in **any source language** into
**any target language**, using a **cloned reference voice** (OmniVoice).

It is **pre-configured for Portuguese/English → Simplified Chinese (Mandarin)**
with a reference voice speaking Chinese, but the target language is a CLI flag:
`--lang` (and the translation stage also accepts `--idioma`). Inspired by the
*canal-dublagem-jiang* PT-BR dubbing channel.

> ⚠️ **Before picking a target language, check that OmniVoice was trained on
> it.** Voice cloning quality depends on the model's training data — languages
> it was not trained on produce degraded or wrong speech. See the
> [OmniVoice model card](https://huggingface.co/k2-fsa/OmniVoice) for the
> supported language list (e.g. zh, en, ja, ko, fr, es, de — verify).

The TTS deliberately **does not control duration** — audio is generated at its
natural length and fitted into the subtitle slots during synchronization
(ffmpeg `atempo`, up to 2.0x).

---

## How it works (5 stages)

| # | Stage | Tool | Output |
|---|-------|------|--------|
| 1 | Dual transcription | mlx-whisper (medium + large-v3) | `<base>_medium_en.srt`, `<base>_large-v3_en.srt` |
| 2 | Diagnostics | medium × large comparison (repetitions, gaps, severe windows) | report (read-only) |
| 3 | Translation → target | DeepSeek API (`deepseek-chat`, JSON chunks of 15 blocks) | `<base>_medium_<lang>.srt` |
| 4 | TTS | OmniVoice voice cloning, **no duration control** | `audios_blocos/bloco_N.mp3` |
| 5 | Synchronization | pydub + ffmpeg `atempo` (original audio at 0.5% volume, dub 2.5x) | `<base>_dublado_<lang>.mp4` |

Everything a video produces lives in `Prontos/<base>/` — nothing is left loose
in the project root. When `--step full` finishes, the source video is moved
into that folder too.

---

## Requirements

- **macOS with Apple Silicon** (mlx-whisper runs on the Metal GPU); the sync
  scripts also work on other platforms with system `ffmpeg`
- **Python 3.11+** with the packages in `requirements.txt`
- **DeepSeek API key** for the translation stage
- **Reference voice** for TTS: `referencias/voz_juan_zh.mp3` (a short clip of
  the target voice speaking Chinese) — provide it locally, it is not committed

## Installation

```bash
git clone <this-repo> Juan_bilibili
cd Juan_bilibili

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# ffmpeg: install system-wide (brew install ffmpeg) or let imageio-ffmpeg provide it

# Config
cp .env.example .env        # then edit .env with your DEEPSEEK_API_KEY
export $(cat .env | xargs)  # or set DEEPSEEK_API_KEY in your shell
```

The pipeline auto-discovers the Python interpreter via the `PIPELINE_PYTHON`
environment variable (falls back to the current interpreter).

## Usage

```bash
# Full pipeline (transcription + translation + TTS + sync)
python scripts/orquestrador_zh.py --step full --video "video.mp4"

# Only transcription + translation
python scripts/orquestrador_zh.py --step srt --video "video.mp4"

# Only diagnostics (medium × large)
python scripts/orquestrador_zh.py --step diag --video "video.mp4"

# Only TTS (from an existing _zh SRT)
python scripts/orquestrador_zh.py --step tts --video "video.mp4"

# Only synchronization
python scripts/orquestrador_zh.py --step sync --video "video.mp4"

# Flags: --idioma en|pt (force SOURCE transcription language),
#        --lang zh|es|en|... (TARGET dubbing language, default: zh)
#        --inicio N, --fim N, --num-step N, --no-qa
```

### Changing the target language (multi-language)

The pipeline is **language-agnostic**: dubbing from any source into any target.

```bash
# Dubbed into Spanish (target) from any source language
python scripts/orquestrador_zh.py --step full --video "video.mp4" --lang es

# Dubbed into Japanese
python scripts/orquestrador_zh.py --step full --video "video.mp4" --lang ja
```

Three things to do when switching the target language:

1. **Verify OmniVoice supports it** — check the
   [OmniVoice model card](https://huggingface.co/k2-fsa/OmniVoice) for the
   supported languages before running TTS.
2. **Provide a reference voice in that language** — the voice clone must hear
   the target language:
   ```bash
   python scripts/gerar_audios_zh.py --srt out.srt --output-dir audios_blocos \
     --idioma es --ref-audio referencias/minha_voz_es.mp3 --ref-text "transcripción de la voz"
   ```
3. **Optionally tune the translation style** (default is the academic/lecture
   style tuned for Simplified Chinese):
   ```bash
   python scripts/traduzir_srt_zh.py --input <base>_medium_en.srt --output <base>_medium_es.srt \
     --idioma es --style-hint "Natural European Spanish, informal tone."
   ```

The file suffixes follow the target (`_medium_<lang>.srt`, `_dublado_<lang>.mp4`),
so multiple targets can coexist in the same `Prontos/<base>/` folder.

### Per-stage scripts

```bash
python scripts/transcritor.py video.mp4 --modelo medium      # transcription
python scripts/traduzir_srt_zh.py --input <base>_medium_en.srt --output <base>_medium_zh.srt
python scripts/gerar_audios_zh.py --srt <base>_medium_zh.srt --output-dir Prontos/<base>/audios_blocos
python scripts/verificar_audios.py --audio-dir Prontos/<base>/audios_blocos --srt Prontos/<base>/<base>_medium_zh.srt
```

## Output structure

```
Prontos/<base>/
  <base>_medium_en.srt            # original transcription (never edit)
  <base>_large-v3_en.srt          # reference transcription (never edit)
  <base>_medium_zh.srt            # translated (TTS source)
  <base>_dublado_zh.mp4           # final dubbed video
  <base>_acelerados.log           # segments accelerated >1.5x
  audios_blocos/                  # TTS audio blocks (bloco_N.mp3)
  <base>.mov / <base>.mp4         # source video (moved here on full completion)
```

## Speaking-rate limits (Mandarin)

Mandarin is measured in **characters per second (字/秒)** — pyphen syllables
do not apply. Natural Mandarin speech runs ~3.5–5.5 chars/s.

- `CHARS_HI` = 6.5 字/s — pre-TTS predictor: above this, the TTS audio will likely overflow the slot
- `ATEMPO_ALERT` = 1.5x — logged during sync
- `ATEMPO_MAX` = 2.0x — ffmpeg limit; beyond this the audio cannot fit the slot

## Skills (DeepSeek Harness)

`.dsh/skills/` contains reusable skill docs for the DeepSeek Harness agent
(discovered automatically from `<project>/.dsh/skills`): `dub-parse`,
`dub-diagnostics`, `dub-translation`, `dub-syllable-rate`,
`dub-pipeline-orchestrator` and `dub-pipeline-agent`.

## Project layout

```
Juan_bilibili/
  scripts/                  # pipeline scripts (entry point: orquestrador_zh.py)
  .dsh/skills/              # agent skills
  pipeline_llm_local/       # diagnostics module (medium × large comparison)
  referencias/              # reference voice (provide locally)
  Prontos/<base>/           # finished video bundles
  Em produção/              # videos waiting to be processed
```

## Credits

Adapted from the PT-BR dubbing pipeline of **canal-dublagem-jiang**
(transcription, diagnostics, translation and synchronization flow), re-targeted
to Simplified Chinese with a duration-free TTS approach.

---
*Made with mlx-whisper, DeepSeek, OmniVoice and ffmpeg.*

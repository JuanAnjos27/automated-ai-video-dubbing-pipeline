#!/usr/bin/env python3
"""
Verifica qualidade dos áudios TTS transcrevendo cada MP3 individualmente com Whisper
e comparando com o texto esperado do SRT.

USO:
  python3 verificar_audios.py --audio-dir audios_blocos/hist-secret26_ptbr --srt hist-secret26_ptbr.srt
  python3 verificar_audios.py --audio-dir audios/ --srt legenda.srt --model tiny --resume

Requisitos: pip install openai-whisper rapidfuzz
"""

import argparse
import json
import os
import re
import sys
import time

from rapidfuzz import fuzz

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_MODEL = "small"
SIMILARITY_THRESHOLD = 0.60
LANGUAGE = "zh"  # idioma dos áudios TTS (default zh — sobrescreva com --idioma)
BATCH_SIZE = 200  # show progress every N files

# ffmpeg no PATH para o Whisper: sistema → imageio-ffmpeg
import shutil as _shutil
_ffmpeg_exe = _shutil.which("ffmpeg")
if not _ffmpeg_exe:
    try:
        import imageio_ffmpeg
        _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _ffmpeg_exe = None
if _ffmpeg_exe:
    os.environ["PATH"] = os.path.dirname(_ffmpeg_exe) + os.pathsep + os.environ.get("PATH", "")


def to_seconds(ts):
    h, m, s_ms = ts.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(srt_path):
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = {}
    for b in content.strip().split("\n\n"):
        lines = b.strip().split("\n")
        if len(lines) < 3:
            continue
        idx = int(lines[0])
        text = " ".join(lines[2:]).strip()
        blocks[idx] = text
    return blocks


def normalize(text):
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity_score(a, b):
    na = normalize(a)
    nb = normalize(b)
    if not na or not nb:
        return 0.0
    # token_set_ratio: robusto para frases curtas e diferenças de ordem
    ratio = fuzz.token_set_ratio(na, nb) / 100.0
    # word overlap
    wa, wb = set(na.split()), set(nb.split())
    if not wa:
        return 0.0
    overlap = len(wa & wb) / len(wa)
    return 0.7 * ratio + 0.3 * overlap


def list_mp3s(audio_dir):
    files = sorted(
        [f for f in os.listdir(audio_dir) if f.endswith(".mp3")],
        key=lambda x: int(re.search(r"(\d+)", x).group(1))
    )
    return files


def verify_per_file(audio_dir, srt_path, model_name=DEFAULT_MODEL, threshold=None, resume=False):
    thr = threshold if threshold is not None else SIMILARITY_THRESHOLD

    print("=" * 60)
    print("  VERIFICAÇÃO DE ÁUDIOS TTS (por arquivo)")
    print("=" * 60)

    # Load SRT
    print(f"\n[1/3] Lendo SRT: {srt_path}")
    srt_blocks = parse_srt(srt_path)
    print(f"  {len(srt_blocks)} blocos")

    # List MP3s
    print(f"\n[2/3] Listando MP3s em: {audio_dir}")
    mp3_files = list_mp3s(audio_dir)
    print(f"  {len(mp3_files)} arquivos")

    # Resume
    output_json = os.path.splitext(srt_path)[0] + "_audios_ruins.json"
    already_checked = set()
    if resume and os.path.exists(output_json):
        with open(output_json, "r", encoding="utf-8") as f:
            prev = json.load(f)
        for b in prev.get("bad_blocks", []):
            already_checked.add(b["id"])
        print(f"  Retomando: {len(already_checked)} já verificados anteriormente")

    # Load Whisper model once
    import whisper
    print(f"\n[3/3] Carregando Whisper '{model_name}'...")
    t0 = time.time()
    model = whisper.load_model(model_name)
    print(f"  Modelo carregado em {time.time()-t0:.1f}s")

    # Process each MP3
    bad_blocks = []
    ok_count = 0
    fillers = 0
    errors = 0
    t_start = time.time()

    for i, fname in enumerate(mp3_files):
        # Extract block ID from filename
        m = re.search(r"(\d+)", fname)
        if not m:
            continue
        blk_id = int(m.group(1))

        # Get expected text
        expected = srt_blocks.get(blk_id, "")
        if not expected:
            continue

        # Skip fillers (single words, punctuation-only)
        norm = normalize(expected)
        if len(norm) <= 2 and len(expected.strip()) <= 4:
            fillers += 1
            continue

        # Skip already checked on resume
        if blk_id in already_checked:
            ok_count += 1
            continue

        filepath = os.path.join(audio_dir, fname)

        try:
            result = model.transcribe(filepath, language=LANGUAGE, verbose=False,
                                      word_timestamps=False)
            whisper_text = result["text"].strip()
        except Exception as e:
            whisper_text = "(erro transcrevendo)"
            errors += 1

        sim = similarity_score(expected, whisper_text)

        if sim < thr:
            bad_blocks.append({
                "id": blk_id,
                "expected": expected,
                "whisper": whisper_text,
                "similarity": round(sim, 3),
                "file": fname,
            })

        if sim >= thr:
            ok_count += 1

        # Progress
        if (i + 1) % BATCH_SIZE == 0:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed
            remaining = (len(mp3_files) - i - 1) / rate
            print(f"  [{i+1}/{len(mp3_files)}] {rate:.1f} arq/s | ~{remaining/60:.0f}min restantes | OK={ok_count} ruins={len(bad_blocks)}")

    # Final progress
    elapsed = time.time() - t_start
    print(f"  [{len(mp3_files)}/{len(mp3_files)}] concluído em {elapsed/60:.1f} min | {len(mp3_files)/elapsed:.1f} arq/s")

    # Report
    total = ok_count + len(bad_blocks)
    print(f"\n{'='*60}")
    print(f"  RESULTADO")
    print(f"{'='*60}")
    print(f"  ✅ OK:            {ok_count} ({100*ok_count/total:.1f}%)")
    print(f"  ❌ Baixa similaridade: {len(bad_blocks)} ({100*len(bad_blocks)/total:.1f}%)")
    if fillers:
        print(f"  ℹ️  Fillers ignorados: {fillers}")
    if errors:
        print(f"  ⚠️  Erros de transcrição: {errors}")

    if bad_blocks:
        print(f"\n── BLOCOS COM BAIXA SIMILARIDADE (< {thr:.0%}) ──\n")
        for b in sorted(bad_blocks, key=lambda x: x["similarity"])[:40]:
            print(f"  #{b['id']} | {b['file']} | sim={b['similarity']:.2f}")
            print(f"    Esperado: \"{b['expected'][:100]}\"")
            print(f"    Whisper : \"{b['whisper'][:100]}\"")
            print()

    # Save JSON
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump({
            "total": total,
            "ok": ok_count,
            "bad": len(bad_blocks),
            "fillers": fillers,
            "errors": errors,
            "threshold": thr,
            "bad_blocks": bad_blocks,
        }, f, ensure_ascii=False, indent=2)
    print(f"  Relatório: {output_json}")

    return bad_blocks


def main():
    parser = argparse.ArgumentParser(description="Verifica áudios TTS com Whisper — um MP3 por vez")
    parser.add_argument("--audio-dir", required=True, help="Pasta com MP3s da dublagem")
    parser.add_argument("--srt", required=True, help="Arquivo SRT de referência")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--idioma", default=LANGUAGE, help="Idioma dos áudios (default: zh). Ex.: zh, en, pt")
    parser.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD)
    parser.add_argument("--resume", action="store_true", help="Retoma de execução anterior")
    args = parser.parse_args()

    if not os.path.isdir(args.audio_dir):
        print(f"ERRO: Pasta não encontrada: {args.audio_dir}")
        sys.exit(1)
    if not os.path.exists(args.srt):
        print(f"ERRO: SRT não encontrado: {args.srt}")
        sys.exit(1)

    globals()["LANGUAGE"] = args.idioma
    verify_per_file(args.audio_dir, args.srt, args.model, args.threshold, args.resume)


if __name__ == "__main__":
    main()

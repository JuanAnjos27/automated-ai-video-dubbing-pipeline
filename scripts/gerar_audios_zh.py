#!/usr/bin/env python3
"""
Gerador de áudios por bloco SRT via OmniVoice — chinês, voz do Juan (SEM duração)
===================================================================================

Equivalente ao gerar_audios_omnivoice.py do canal-dublagem-jiang, adaptado para:
  - Idioma chinês (zh)
  - Voice cloning da VOZ DO JUAN (referencias/voz_juan_zh.mp3)
  - SEM controle de duração: o áudio sai com a duração natural que o modelo
    escolhe. O encaixe no slot do SRT (speedup / atempo) é feito na etapa de
    SINCRONIZAÇÃO, não aqui.

USO:
    python3 gerar_audios_zh.py --srt legenda_zh.srt --output-dir audios_blocos
    python3 gerar_audios_zh.py --srt legenda_zh.srt --output-dir audios_blocos --inicio 5 --fim 10
    python3 gerar_audios_zh.py --texto "大家好，我是江。" --output referencias/abertura.mp3

Saída: bloco_1.mp3, bloco_2.mp3, ... (PCM WAV renomeado para .mp3, como no projeto original)
"""

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from omnivoice import OmniVoice

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACAO
# ═══════════════════════════════════════════════════════════════════════════════

DEVICE = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
DTYPE = torch.float16
SAMPLE_RATE = 24000  # OmniVoice gera sempre em 24 kHz
NUM_STEP = 32

# Idioma alvo — default: chinês mandarim (pode ser sobrescrito com --idioma)
LANGUAGE = "zh"

RAIZ = Path(__file__).resolve().parent.parent  # Juan_bilibili/
REFERENCIAS_DIR = RAIZ / "referencias"

# ── Voz de referência padrão: Juan falando chinês (sobrescreva com --ref-audio/--ref-text) ──
REF_AUDIO = str(REFERENCIAS_DIR / "voz_juan_zh.mp3")
REF_TEXT = (
    "来来来,孩子,坐下喝杯茶,"
    "你看这院子里的老槐树跟着咱家几十年了,"
    "你爷爷在的时候,每年夏天都在这树底下乘凉下棋。"
)

# Idiomas suportados pelo OmniVoice — ANTES de escolher o alvo, confira se o
# idioma de destino está entre os que o modelo foi treinado (model card
# k2-fsa/OmniVoice). Idiomas sem suporte produzem fala degradada/errada.
# Ex.: zh, en, ja, ko, fr, es, de (consultar a lista oficial do modelo).
SUPPORTED_LANGS_HINT = "zh, en, ja, ko, fr, es, de (verificar model card do OmniVoice)"

AUDIO_PREFIX = "bloco_"


def to_seconds(ts: str) -> float:
    """Converte timestamp SRT (HH:MM:SS,mmm) para segundos."""
    ts = ts.strip().replace(",", ".")
    h, m, rest = ts.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(srt_path: str) -> list[dict]:
    """Le o SRT e retorna blocos com index, timestamps e texto."""
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = []
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n"
        r"([\s\S]*?)(?=\n\d+\s*\n|\Z)",
        re.MULTILINE,
    )
    for m in pattern.finditer(content):
        text = m.group(4).strip()
        if not text:
            continue
        start_s = to_seconds(m.group(2))
        end_s = to_seconds(m.group(3))
        blocks.append({
            "index": int(m.group(1)),
            "start_s": start_s,
            "end_s": end_s,
            "slot_dur": round(end_s - start_s, 2),
            "text": text,
        })
    return blocks


def gerar_audio(model: OmniVoice, texto: str, num_step: int = NUM_STEP) -> np.ndarray:
    """Clona a voz do Juan e fala o texto em chinês. SEM duration — o modelo escolhe a duração."""
    result = model.generate(
        text=texto,
        language=LANGUAGE,
        ref_audio=REF_AUDIO,
        ref_text=REF_TEXT,
        num_step=num_step,
    )
    return result[0]


def main():
    parser = argparse.ArgumentParser(
        description="Gera áudios via OmniVoice (voice cloning, sem duration). Default: chinês, voz do Juan."
    )
    parser.add_argument("--srt", default=None, help="Arquivo SRT de entrada (modo batch)")
    parser.add_argument("--output-dir", default=None, help="Pasta de saída dos áudios (modo batch)")
    parser.add_argument("--inicio", type=int, default=1, help="Índice do bloco inicial (default: 1)")
    parser.add_argument("--fim", type=int, default=None, help="Índice do bloco final (default: último)")

    parser.add_argument("--idioma", default="zh", help=f"Idioma da fala (default: zh). Suportados (confirmar no model card do OmniVoice): {SUPPORTED_LANGS_HINT}")
    parser.add_argument("--ref-audio", default=None, help="Voz de referência (default: referencias/voz_juan_zh.mp3)")
    parser.add_argument("--ref-text", default=None, help="Transcrição da voz de referência (default: texto da voz do Juan)")

    parser.add_argument("--texto", default=None, help="Texto único a gerar (modo avulso, ex.: abertura)")
    parser.add_argument("--output", default=None, help="Saída do modo --texto (.wav ou .mp3)")

    parser.add_argument("--num-step", type=int, default=NUM_STEP, help=f"Passos de difusão (default: {NUM_STEP})")
    args = parser.parse_args()

    global LANGUAGE, REF_AUDIO, REF_TEXT
    LANGUAGE = args.idioma
    if args.ref_audio:
        REF_AUDIO = args.ref_audio
    if args.ref_text:
        REF_TEXT = args.ref_text

    if args.texto and args.srt:
        print("[ERRO] Use --texto OU --srt, não ambos.")
        sys.exit(1)

    if not args.texto and not args.srt:
        print("[ERRO] Informe --srt (batch) ou --texto (avulso).")
        sys.exit(1)

    if args.texto:
        if not args.output:
            print("[ERRO] Modo --texto requer --output (caminho do arquivo).")
            sys.exit(1)
        print(f"[AVULSO] \"{args.texto[:60]}{'...' if len(args.texto) > 60 else ''}\"")
        print(f"[SAIDA]  {args.output}")
    else:
        if not os.path.exists(args.srt):
            print(f"[ERRO] SRT não encontrado: {args.srt}")
            sys.exit(1)
        if not args.output_dir:
            print("[ERRO] Modo batch requer --output-dir.")
            sys.exit(1)
        os.makedirs(args.output_dir, exist_ok=True)

    print(f"[MODELO] OmniVoice | device: {DEVICE} | language: {LANGUAGE} | num_step: {args.num_step}")
    print(f"[VOZ]    {REF_AUDIO}")

    print("Carregando OmniVoice (k2-fsa/OmniVoice)...")
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=DEVICE, dtype=DTYPE)
    print("Modelo carregado.\n")

    # ── Modo avulso (texto único) ──
    if args.texto:
        audio_np = gerar_audio(model, args.texto, num_step=args.num_step)
        destino = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        sf.write(destino, audio_np, SAMPLE_RATE)
        dur = len(audio_np) / SAMPLE_RATE
        print(f"\n✅ Concluído! {destino} ({dur:.1f}s)")
        return

    # ── Modo batch (SRT) ──
    blocks = parse_srt(args.srt)
    if not blocks:
        print("[ERRO] Nenhum bloco encontrado no SRT.")
        sys.exit(1)

    blocks = [b for b in blocks if b["index"] >= args.inicio]
    if args.fim:
        blocks = [b for b in blocks if b["index"] <= args.fim]

    total = len(blocks)
    print(f"[BATCH] {total} blocos (índices {blocks[0]['index']}–{blocks[-1]['index']})")
    print("Sem controle de duração — speedup será feito na sincronização.\n")

    for i, bloco in enumerate(blocks):
        idx = bloco["index"]
        nome = f"{AUDIO_PREFIX}{idx}.mp3"
        saida = os.path.join(args.output_dir, nome)

        progresso = f"[{i+1}/{total}]"
        if os.path.exists(saida):
            print(f"{progresso} #{idx} já existe, pulando: {nome}")
            continue

        print(f"{progresso} #{idx} | slot {bloco['slot_dur']:.1f}s | {bloco['text'][:80]}{'...' if len(bloco['text']) > 80 else ''}")
        try:
            audio_np = gerar_audio(model, bloco["text"], num_step=args.num_step)
            tmp_wav = saida.replace(".mp3", "_tmp.wav")
            sf.write(tmp_wav, audio_np, SAMPLE_RATE)
            os.replace(tmp_wav, saida)
            dur = len(audio_np) / SAMPLE_RATE
            print(f"       Salvo: {nome} ({dur:.1f}s)")
        except Exception as e:
            print(f"[ERRO] Bloco {idx}: {e}")

    print(f"\nConcluído! {total} áudios em {os.path.abspath(args.output_dir)}")


if __name__ == "__main__":
    main()

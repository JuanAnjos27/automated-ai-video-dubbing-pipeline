#!/usr/bin/env python3
"""
Orquestrador do Pipeline de Dublagem MULTI-IDIOMA (Juan_bilibili)
=================================================================

Pipeline agnóstico de idioma: dubla de QUALQUER idioma de origem para
QUALQUER idioma de destino. Pré-configurado para PT/EN → ZH (chinês),
mas o idioma ALVO é controlado por --lang (e a voz de referência deve
ser do idioma alvo).

  1. Transcricao dupla → <base>_medium_en.srt + <base>_large-v3_en.srt
  2. Diagnostico comparativo (medium × large) + validacao de timestamps
  3. Traducao para o alvo → <base>_medium_<lang>.srt (traduzir_srt_zh.py, DeepSeek)
  4. TTS → Prontos/<base>/audios_blocos/ (gerar_audios_zh.py, SEM duration)
  5. Sincronizacao → <base>_dublado_<lang>.mp4 (sincronizar_dublagem.py, com atempo)

Versionamento — NUNCA editar originais:
  <base>_medium_en.srt      ← transcricao original (lingua do video, nunca mexer)
  <base>_large-v3_en.srt    ← transcricao referencia (lingua do video, nunca mexer)
  <base>_medium_<lang>.srt  ← pos-traducao (idioma alvo) — fonte do TTS

Modos:
  python orquestrador_zh.py --step full --video video.mp4              # completo (alvo default zh)
  python orquestrador_zh.py --step full --video video.mp4 --lang es    # dublar para espanhol
  python orquestrador_zh.py --step srt  --video video.mp4              # so transcricao + traducao
  python orquestrador_zh.py --step diag --video video.mp4              # so diagnostico
  python orquestrador_zh.py --step tts  --video video.mp4 --lang zh    # so TTS (a partir do _zh)
  python orquestrador_zh.py --step sync --video video.mp4 --lang zh    # so sincronizacao
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ── Caminhos base ──────────────────────────────────────────────────────────
CANAL_DIR = Path(__file__).resolve().parent.parent   # Juan_bilibili/
BASE_DIR  = CANAL_DIR.parent                          # diretório acima do projeto (ex.: pasta de automações)

# Tudo que um vídeo produz (SRTs, áudios, log, vídeo final) vai para Prontos/<base>/
PRONTOS_DIR = CANAL_DIR / "Prontos"

# Python do pipeline: use PIPELINE_PYTHON (ex.: caminho do venv), senão o python atual.
_py_env = os.environ.get("PIPELINE_PYTHON")
VENV_311 = Path(_py_env) if _py_env else (BASE_DIR / ".venv311/bin/python")
PYTHON_311 = str(VENV_311) if VENV_311.exists() else sys.executable


def garantir_dirs(nomes):
    """Garante que a pasta do vídeo em Prontos/<base>/ exista."""
    os.makedirs(nomes["vid_dir"], exist_ok=True)
    os.makedirs(nomes["audio_dir"], exist_ok=True)


def run(venv_python, *args, input=None):
    """Executa um script do pipeline com o venv. input = bytes enviados via stdin."""
    full = [venv_python] + [str(a) for a in args]
    print(f"\n$ {' '.join(full)}\n")
    subprocess.run(full, input=input, cwd=str(CANAL_DIR), check=True)


def derivar_nomes(video_path, alvo="zh"):
    """Deriva todos os nomes de saida a partir do video.
    alvo = idioma ALVO da dublagem (default zh). Os sufixos acompanham.
    Tudo vai para Prontos/<base>/ — nada fica solto na raiz do projeto."""
    base = Path(video_path).stem
    video_abs = str(CANAL_DIR / video_path) if not os.path.isabs(video_path) else video_path
    vid_dir = PRONTOS_DIR / base
    return {
        "video":                video_abs,
        "base":                 base,
        "alvo":                 alvo,
        "vid_dir":              str(vid_dir),
        "srt_medium_en":        str(vid_dir / f"{base}_medium_en.srt"),
        "srt_large_en":         str(vid_dir / f"{base}_large-v3_en.srt"),
        "srt_medium_en_corrigido": str(vid_dir / f"{base}_medium_en_corrigido.srt"),
        "srt_medium_lang":      str(vid_dir / f"{base}_medium_{alvo}.srt"),
        "srt_medium_lang_virgem": str(vid_dir / f"{base}_medium_{alvo}_virgem.srt"),
        "audio_dir":            str(vid_dir / "audios_blocos"),
        "output":               str(vid_dir / f"{base}_dublado_{alvo}.mp4"),
        "accel_log":            str(vid_dir / f"{base}_acelerados.log"),
    }


# ── srt-parse: parse e validacao ───────────────────────────────────────────

def srt_parse(path):
    """Le um arquivo SRT e retorna lista de blocos (dicts)."""
    with open(path, encoding='utf-8') as f:
        content = f.read()
    blocks = re.split(r'\n\n+', content.strip())
    result = []
    for b in blocks:
        lines = b.strip().split('\n')
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
        except ValueError:
            continue
        tm = re.match(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', lines[1])
        if not tm:
            continue
        result.append({
            'id': int(idx),
            'start': tm.group(1),
            'end': tm.group(2),
            'text': ' '.join(lines[2:]).strip(),
        })
    return result


def parse_time(t):
    h, m, s = t.split(':')
    s, ms = s.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def format_time(secs):
    secs = max(0, secs)
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    ms = round((s % 1) * 1000)
    s = int(s)
    if ms >= 1000:
        ms -= 1000
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_check_integrity(blocks):
    """Sobreposicoes, duracoes negativas, blocos curtos."""
    overlaps = sum(
        1 for i in range(len(blocks) - 1)
        if parse_time(blocks[i + 1]['start']) < parse_time(blocks[i]['end']) - 0.001
    )
    neg = sum(1 for b in blocks if parse_time(b['end']) - parse_time(b['start']) <= 0)
    short = sum(1 for b in blocks if 0 < parse_time(b['end']) - parse_time(b['start']) < 0.5)
    return {'overlaps': overlaps, 'neg_dur': neg, 'short_blocks': short, 'total': len(blocks)}


def srt_fix_negatives(blocks):
    """Corrige duracoes negativas: se end < start, troca (timestamps invertidos)."""
    fixed = 0
    for b in blocks:
        if parse_time(b['end']) - parse_time(b['start']) <= 0:
            b['start'], b['end'] = b['end'], b['start']
            fixed += 1
    return fixed


def srt_fix_overlaps(blocks):
    """Corrige sobreposicoes apos ajuste de timestamp."""
    fixed = 0
    for i in range(len(blocks) - 1):
        if parse_time(blocks[i + 1]['start']) < parse_time(blocks[i]['end']) - 0.001:
            blocks[i + 1]['start'] = format_time(parse_time(blocks[i]['end']))
            fixed += 1
    return fixed


def srt_save(blocks, path):
    output = '\n\n'.join(
        f"{b['id']}\n{b['start']} --> {b['end']}\n{b['text']}" for b in blocks
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(output)


def srt_diagnostics(blocks, label="SRT"):
    """Analisa qualidade de um SRT: negativos, curtos, repeticoes, gaps, alucinacoes."""
    import statistics

    durs = [parse_time(b['end']) - parse_time(b['start']) for b in blocks]
    valid = [d for d in durs if d > 0]

    neg = [(b['id'], round(parse_time(b['end']) - parse_time(b['start']), 3))
           for b in blocks if parse_time(b['end']) - parse_time(b['start']) <= 0]

    short = [(b['id'], round(parse_time(b['end']) - parse_time(b['start']), 3))
             for b in blocks if 0 < parse_time(b['end']) - parse_time(b['start']) < 0.3]

    reps = []
    for i in range(1, len(blocks)):
        a = blocks[i - 1]['text'].strip().lower()
        bt = blocks[i]['text'].strip().lower()
        if a == bt or (len(a) > 20 and (a in bt or bt in a)):
            reps.append((blocks[i - 1]['id'], blocks[i]['id']))

    gaps = []
    for i in range(len(blocks) - 1):
        gap = parse_time(blocks[i + 1]['start']) - parse_time(blocks[i]['end'])
        if gap > 8:
            gaps.append((blocks[i]['id'], blocks[i + 1]['id'], round(gap, 1)))

    hall_phrases = ['thank you for watching', 'please subscribe', 'like and subscribe',
                    'see you next', '.com', '♪', '[music]', '[applause]']
    hall = [(b['id'], p) for b in blocks for p in hall_phrases if p in b['text'].lower()]

    return {
        'label': label, 'total': len(blocks),
        'dur_mean': round(statistics.mean(valid), 2) if valid else 0,
        'neg_timestamps': neg, 'short_blocks': short,
        'repetitions': reps, 'gaps': gaps, 'hallucinations': hall,
    }


def print_diagnostics(report):
    r = report
    print(f"\n  DIAGNOSTICO — {r['label']}")
    print(f"  Blocos: {r['total']} | Dur. media: {r['dur_mean']}s")
    print(f"  Negativos : {len(r['neg_timestamps'])} {'✅' if not r['neg_timestamps'] else '❌'}")
    print(f"  Curtos<0.3: {len(r['short_blocks'])} {'✅' if not r['short_blocks'] else '⚠️'}")
    print(f"  Repeticoes: {len(r['repetitions'])} {'✅' if not r['repetitions'] else '⚠️'}")
    print(f"  Gaps >8s  : {len(r['gaps'])} {'✅' if not r['gaps'] else '⚠️'}")
    print(f"  Alucinacao: {len(r['hallucinations'])} {'✅' if not r['hallucinations'] else '❌'}")


# ═══════════════════════════════════════════════════════════════════════════
# ETAPAS
# ═══════════════════════════════════════════════════════════════════════════

def etapa_transcricao_dupla(video, lang=None):
    """
    Etapa 1: Transcricao dupla (mlx-whisper).
    Gera <base>_medium_en.srt e <base>_large-v3_en.srt validados.
    A transcricao é na LINGUA DO VIDEO (por padrao ingles); --idioma força.
    """
    print("\n── Transcricao Dupla (mlx-whisper) ──")
    nomes = derivar_nomes(video)
    garantir_dirs(nomes)

    run_args = [nomes["video"]]
    if lang:
        run_args += ["--idioma", lang]

    # ── Medium ──
    print(f"\n[1/2] Transcrevendo com modelo medium...")
    run(PYTHON_311, "scripts/transcritor.py", *run_args, "--modelo", "medium")
    srt_gerado = str(Path(nomes["video"]).with_suffix(".srt"))

    blocks = srt_parse(srt_gerado)
    print_diagnostics(srt_diagnostics(blocks, f"{nomes['base']} (medium)"))

    neg_fixed = srt_fix_negatives(blocks)
    ovl_fixed = srt_fix_overlaps(blocks)
    if neg_fixed or ovl_fixed:
        print(f"  Correcoes: {neg_fixed} negativos (swap), {ovl_fixed} overlaps")
        srt_save(blocks, srt_gerado)

    integ = srt_check_integrity(blocks)
    if integ['neg_dur'] > 0 or integ['overlaps'] > 0:
        print(f"  ⚠️  Ainda ha {integ['neg_dur']} neg, {integ['overlaps']} ovl — verificar manualmente")

    shutil.move(srt_gerado, nomes["srt_medium_en"])
    print(f"       → {nomes['srt_medium_en']}")

    # ── Large-v3 ──
    print(f"\n[2/2] Transcrevendo com modelo large-v3...")
    run(PYTHON_311, "scripts/transcritor.py", *run_args, "--modelo", "large-v3")
    srt_gerado = str(Path(nomes["video"]).with_suffix(".srt"))

    blocks = srt_parse(srt_gerado)
    print_diagnostics(srt_diagnostics(blocks, f"{nomes['base']} (large-v3)"))

    neg_fixed = srt_fix_negatives(blocks)
    ovl_fixed = srt_fix_overlaps(blocks)
    if neg_fixed or ovl_fixed:
        print(f"  Correcoes: {neg_fixed} negativos (swap), {ovl_fixed} overlaps")
        srt_save(blocks, srt_gerado)

    shutil.move(srt_gerado, nomes["srt_large_en"])
    print(f"       → {nomes['srt_large_en']}")

    return nomes


def etapa_diagnostico(video):
    """Diagnostico comparativo: medium vs large-v3 (gaps, repeticoes, janelas severas).
    NUNCA modifica os SRTs — apenas analisa e exibe resultados."""
    nomes = derivar_nomes(video)
    srt_en = nomes["srt_medium_en"]
    compare_srt = nomes["srt_large_en"]

    if not os.path.exists(srt_en) or not os.path.exists(compare_srt):
        print("[ERRO] Transcreva primeiro (--step srt ou full). SRTs _en ausentes.")
        sys.exit(1)

    print("\n── Diagnostico Comparativo ──")

    # Garante que pipeline_llm_local (raiz do projeto) esteja no sys.path
    if str(CANAL_DIR) not in sys.path:
        sys.path.insert(0, str(CANAL_DIR))

    from pipeline_llm_local.srt_utils import parse_srt as pll_parse
    from pipeline_llm_local.diagnostics import build_diagnostic_summary, detect_repetition_runs

    base_blocks = pll_parse(srt_en)
    cmp_blocks = pll_parse(compare_srt)

    print(f"  Base:    {len(base_blocks)} blocos ({srt_en})")
    print(f"  Compare: {len(cmp_blocks)} blocos ({compare_srt})")

    diag = build_diagnostic_summary(base_blocks, cmp_blocks)

    print(f"\n  Repeticoes base:     {diag['base_repetition_count']}")
    print(f"  Repeticoes compare:  {diag['compare_repetition_count']}")
    print(f"  Janelas severas:     {diag['severe_windows_count']}")
    print(f"  Gaps de cobertura:   {diag['coverage_gaps_count']} ({diag['coverage_gaps_total_s']}s)")

    if diag['severe_windows']:
        print(f"\n  Janelas severas (divergencia >=65%):")
        for w in diag['severe_windows'][:8]:
            min_s = w['start_ms'] // 60000
            fim_s = w['end_ms'] // 60000
            print(f"    {min_s:02d}:{(w['start_ms']%60000)//1000:02d} -> {fim_s:02d}:{(w['end_ms']%60000)//1000:02d}  divergencia {w['divergence']*100:.0f}%")

    if diag['coverage_gaps']:
        print(f"\n  Gaps de cobertura (medium perdeu, large capturou):")
        for g in diag['coverage_gaps'][:8]:
            print(f"    [{g['start']} -> {g['end']}]  {g['duration_s']}s  ({g['compare_block_count']} blocos)")

    cmp_rep = detect_repetition_runs(cmp_blocks)
    if cmp_rep:
        print(f"\n  ⚠️  Repeticoes no LARGE:")
        for r in cmp_rep[:5]:
            print(f"    #{r['start_index']}-#{r['end_index']} ({r['run_length']}x): \"{r['text'][:50]}\"")


def etapa_traducao(video, alvo="zh", inicio=1, no_qa=False):
    """Etapa 2: Traducao para o idioma ALVO (default chinês) via DeepSeek.
    Le <base>_medium_en.srt, escreve <base>_medium_<alvo>.srt.
    NUNCA modifica o _en original."""
    print(f"\n── Traducao para {alvo} (DeepSeek) ──")
    nomes = derivar_nomes(video, alvo=alvo)
    garantir_dirs(nomes)

    srt_en = nomes["srt_medium_en"]
    out_lang = nomes["srt_medium_lang"]
    if not os.path.exists(srt_en):
        print(f"[ERRO] SRT base não encontrado: {srt_en}")
        sys.exit(1)

    run_args = [
        "scripts/traduzir_srt_zh.py",
        "--input", srt_en,
        "--output", out_lang,
        "--idioma", alvo,
        "--inicio", str(inicio),
    ]
    if no_qa:
        run_args += ["--no-qa"]

    run(PYTHON_311, *run_args)
    return out_lang


def etapa_tts(video, srt=None, inicio=1, fim=None, num_step=32, alvo="zh"):
    """Etapa 3: TTS (OmniVoice, voz de referência, SEM duration).
    alvo = idioma da fala; a voz de referência deve ser desse idioma."""
    print(f"\n── Geracao de Audios (OmniVoice {alvo}, sem duration) ──")
    nomes = derivar_nomes(video, alvo=alvo)
    garantir_dirs(nomes)

    srt_lang = srt or nomes["srt_medium_lang"]
    if not os.path.exists(srt_lang):
        print(f"[ERRO] SRT ({alvo}) não encontrado: {srt_lang}")
        sys.exit(1)

    run_args = [
        "scripts/gerar_audios_zh.py",
        "--srt", srt_lang,
        "--output-dir", nomes["audio_dir"],
        "--idioma", alvo,
        "--inicio", str(inicio),
        "--num-step", str(num_step),
    ]
    if fim:
        run_args += ["--fim", str(fim)]

    run(PYTHON_311, *run_args)


def etapa_sincronizacao(video, srt=None, alvo="zh"):
    """Etapa 4: Sincronizacao com video original (speedup via atempo)."""
    print("\n── Sincronizacao de Dublagem ──")
    nomes = derivar_nomes(video, alvo=alvo)

    video_original = nomes["video"]
    srt_file = srt or nomes["srt_medium_lang"]
    audio_dir = nomes["audio_dir"]
    output = nomes["output"]
    accel_log = nomes["accel_log"]
    garantir_dirs(nomes)

    # 7 inputs pedidos pelo sincronizador (mesma ordem do orquestrador original)
    # Volumes: original bem baixo (0.005) para a dublagem dominar; dub 2.5 compensa TTS quieto (amix normalize=0)
    respostas = f"{video_original}\n{srt_file}\n{audio_dir}\n{output}\n{accel_log}\n0.005\n2.5\n"
    run(PYTHON_311, "scripts/sincronizar_dublagem.py", input=respostas.encode())


# ═══════════════════════════════════════════════════════════════════════════
# PIPELINES
# ═══════════════════════════════════════════════════════════════════════════

def pipeline_srt(video, lang=None, alvo="zh", inicio=1, no_qa=False):
    nomes = etapa_transcricao_dupla(video, lang=lang)
    etapa_traducao(nomes["video"], alvo=alvo, inicio=inicio, no_qa=no_qa)
    print(f"\n✅ Pipeline SRT finalizado para: {nomes['base']} → {nomes['alvo']}")


def mover_video_fonte(nomes):
    """Ao terminar o pipeline, move o vídeo fonte para Prontos/<base>/ (nada solto)."""
    src = nomes["video"]
    dest = os.path.join(nomes["vid_dir"], os.path.basename(src))
    if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dest):
        shutil.move(src, dest)
        print(f"  📦 Vídeo fonte movido → {dest}")


def pipeline_full(video, lang=None, alvo="zh", inicio=1, fim=None, no_qa=False, num_step=32):
    nomes = etapa_transcricao_dupla(video, lang=lang)
    etapa_traducao(nomes["video"], alvo=alvo, inicio=inicio, no_qa=no_qa)
    etapa_tts(nomes["video"], inicio=inicio, fim=fim, num_step=num_step, alvo=alvo)
    etapa_sincronizacao(nomes["video"], alvo=alvo)
    mover_video_fonte(nomes)
    print(f"\n✅ Pipeline finalizado para: {nomes['base']} → {nomes['vid_dir']} (alvo: {alvo})")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Orquestrador do pipeline de dublagem chinês (Juan_bilibili)")
    parser.add_argument("--step", choices=["full", "srt", "diag", "tts", "sync"], default="full",
                        help="Etapa: full (tudo), srt (transcrição+tradução), diag, tts, sync")
    parser.add_argument("--video", required=True, help="Caminho do vídeo/áudio fonte")
    parser.add_argument("--idioma", default=None, help="Forçar idioma da FONTE/transcrição (ex: en, pt). Default: auto")
    parser.add_argument("--lang", default="zh", help="Idioma ALVO da dublagem (ex: zh, en, pt, es...). Default: zh (pré-configurado)")
    parser.add_argument("--inicio", type=int, default=1, help="Bloco inicial (tradução/TTS)")
    parser.add_argument("--fim", type=int, default=None, help="Bloco final (TTS)")
    parser.add_argument("--no-qa", action="store_true", help="Pula QA de fronteiras na tradução")
    parser.add_argument("--num-step", type=int, default=32, help="Passos de difusão do TTS")
    args = parser.parse_args()

    if not os.path.exists(args.video) and not os.path.exists(str(CANAL_DIR / args.video)):
        print(f"[ERRO] Vídeo não encontrado: {args.video}")
        sys.exit(1)

    print("=" * 60)
    print("  Orquestrador de Dublagem — Juan_bilibili (PT/EN → ZH)")
    print("=" * 60)
    print(f"  Vídeo: {args.video}")
    print(f"  Etapa: {args.step}")
    print("=" * 60)

    if args.step == "full":
        pipeline_full(args.video, lang=args.idioma, alvo=args.lang, inicio=args.inicio,
                      fim=args.fim, no_qa=args.no_qa, num_step=args.num_step)
    elif args.step == "srt":
        pipeline_srt(args.video, lang=args.idioma, alvo=args.lang, inicio=args.inicio, no_qa=args.no_qa)
    elif args.step == "diag":
        etapa_diagnostico(args.video)
    elif args.step == "tts":
        etapa_tts(args.video, inicio=args.inicio, fim=args.fim, num_step=args.num_step, alvo=args.lang)
    elif args.step == "sync":
        etapa_sincronizacao(args.video, alvo=args.lang)


if __name__ == "__main__":
    main()

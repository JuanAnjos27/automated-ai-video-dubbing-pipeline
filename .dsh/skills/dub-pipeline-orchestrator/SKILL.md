---
name: dub-pipeline-orchestrator
description: "Orquestrador do pipeline completo de dublagem chinês (Juan_bilibili): sequência de etapas EN/PT → ZH com voz do Juan, checkpoints de qualidade, critérios de aprovação e relatório final. Entry point — referencia as outras skills (dub-srt-parse, dub-diagnostics, dub-translation, dub-syllable-rate). Fluxo real implementado em scripts/orquestrador_zh.py com modos full/srt/diag/tts/sync."
compatibility: "Python 3.x — depende das outras skills do pipeline"
---

# Pipeline Orchestrator — Dublagem Chinês (Juan_bilibili)

## Stack

- **LLM**: DeepSeek API (`deepseek-chat`) — tradução para mandarim, QA
- **Python**: operações determinísticas (parse, timestamps, hanzi/s)
- **mlx-whisper**: transcrição dupla (medium + large-v3), Apple Silicon
- **OmniVoice**: TTS voice cloning — `referencias/voz_juan_zh.mp3`, **SEM duration**
- **pydub + ffmpeg (imageio)**: sincronização com `atempo` (speedup ≤2.0x)

## Sequência obrigatória (5 etapas — fluxo do orquestrador_zh.py)

```
Etapa 1: Transcrição dupla →  transcritor.py (medium + large-v3) → _medium_en.srt + _large-v3_en.srt
Etapa 2: Diagnóstico       →  dub-diagnostics (medium × large, gaps, repetições, janelas severas)
Etapa 3: Tradução → zh     →  dub-translation (DeepSeek, deepseek-chat) → _medium_zh.srt
Etapa 4: TTS               →  gerar_audios_zh.py (OmniVoice, voz_juan_zh, SEM duration)
                              + dub-syllable-rate (chars/s pré-TTS + check_fit pós-TTS)
Etapa 5: Sincronização     →  sincronizar_dublagem.py (atempo, volume orig 0.005, dub 2.5)
                              → <base>_dublado_zh.mp4 + <base>_acelerados.log
```

**Diferenças vs canal PT-BR:**
- Tradução vira **chinês simplificado**, não PT-BR
- Voz de referência: **voz_juan_zh.mp3** (Juan falando chinês)
- **TTS sem controle de duração** — o encaixe no slot do SRT é feito na sincronização
  (`atempo`). NÃO existe etapa de ajuste de timestamps obrigatória antes do TTS;
  a análise de ritmo vira previsão + verificação pós-TTS.

## Comandos (scripts/orquestrador_zh.py)

```bash
cd Juan_bilibili
PY=${PIPELINE_PYTHON:-python3}

# Pipeline completo (1 vídeo)
$PY scripts/orquestrador_zh.py --step full --video "video.mp4"

# Só SRT (transcrição + tradução)
$PY scripts/orquestrador_zh.py --step srt --video "video.mp4"

# Só diagnóstico (medium × large)
$PY scripts/orquestrador_zh.py --step diag --video "video.mp4"

# Só TTS (a partir do _zh existente)
$PY scripts/orquestrador_zh.py --step tts --video "video.mp4"

# Só sincronização
$PY scripts/orquestrador_zh.py --step sync --video "video.mp4"

# Flags: --idioma en|pt (força idioma da FONTE), --lang zh|es|... (idioma ALVO, default zh),
#        --inicio N, --fim N, --num-step N, --no-qa
# Multi-idioma: pré-configurado PT/EN → ZH; para outro alvo, confirmar suporte do
# OmniVoice e fornecer voz de referência no idioma alvo.
# Saídas vão para Prontos/<base>/ — ao final do full, o vídeo fonte também.
```

## Checkpoints — nunca avançar sem aprovação

```python
def checkpoint(label, metrics, required_ok=None):
    """
    Exibe métricas e aguarda confirmação antes de avançar.
    required_ok: lista de chaves que devem ser True/0 para liberar avanço.
    """
    print(f"\n{'='*50}")
    print(f"CHECKPOINT — {label}")
    print(f"{'='*50}")
    for k, v in metrics.items():
        status = ""
        if required_ok and k in required_ok:
            status = " ✅" if v == 0 or v is True else " ❌ BLOQUEADO"
        print(f"  {k}: {v}{status}")
    blocked = required_ok and any(
        (metrics.get(k) != 0 and metrics.get(k) is not True) for k in required_ok
    )
    if blocked:
        print("\n⛔ AVANÇO BLOQUEADO — corrigir antes de continuar")
        return False
    print("\n✅ Pronto para avançar?")
    return True
```

## Relatório completo

```python
def generate_report(state):
    return {
        "total_blocks":          state.get("total_blocks", 0),
        "translated_blocks":     state.get("translated_blocks", 0),
        "zh_qa_ok":              state.get("zh_qa_ok", 0),
        "zh_qa_problems":        state.get("zh_qa_problems", 0),
        "fast_blocks_predicted": state.get("fast_predicted", 0),
        "atempo_alerts":         state.get("atempo_alerts", 0),
        "atempo_overflows":      state.get("atempo_overflows", 0),
        "tts_blocks":            state.get("tts_blocks", 0),
        "tts_errors":            state.get("tts_errors", 0),
        "overlap_errors":        state.get("overlaps", 0),
        "negative_duration_errors": state.get("neg_dur", 0),
        "short_block_errors":    state.get("short_blocks", 0),
        "steps":                 state.get("steps", []),
        "failures":              state.get("failures", []),
    }
```

## Critérios de qualidade

```python
QUALITY_THRESHOLDS = {
    "overlaps":         0,   # deve ser exatamente 0
    "neg_dur":          0,   # deve ser exatamente 0
    "short_blocks":     0,   # deve ser exatamente 0
    "atempo_overflows": 0,   # blocos que não cabem no slot (ratio > 2.0) — bloquear
    "zh_problems":      0,   # marcadores/inglês residual no SRT _zh
    "ok_pct_min":       90.0,  # % de blocos OK no ritmo (chars/s pré-TTS)
}

def evaluate_quality(classification, integrity, zh_problems=0, atempo_overflows=0):
    total = sum(len(v) for v in classification.values() if isinstance(v, list))
    ok_pct = 100 * len(classification["ok"]) / total if total > 0 else 0
    issues = []
    if integrity["overlaps"] > 0:
        issues.append(f"❌ {integrity['overlaps']} sobreposições")
    if integrity["neg_dur"] > 0:
        issues.append(f"❌ {integrity['neg_dur']} durações negativas")
    if integrity["short_blocks"] > 0:
        issues.append(f"❌ {integrity['short_blocks']} blocos <0.5s")
    if ok_pct < QUALITY_THRESHOLDS["ok_pct_min"]:
        issues.append(f"⚠️  OK={ok_pct:.1f}% (mínimo {QUALITY_THRESHOLDS['ok_pct_min']}%)")
    if zh_problems > 0:
        issues.append(f"⚠️  {zh_problems} problemas no SRT _zh (marcadores/inglês residual)")
    if atempo_overflows > 0:
        issues.append(f"⛔ {atempo_overflows} blocos estouram o slot (atempo > 2.0x)")
    return {"approved": len(issues) == 0, "ok_pct": round(ok_pct, 1), "issues": issues}
```

## Regras invioláveis

1. **Sobreposições = bloqueio** — nenhum arquivo entregue com timestamps sobrepostos
2. **Nunca avançar etapas sem checkpoint** — cada etapa tem validação explícita
3. **Análise de ritmo SEMPRE no texto chinês traduzido** — nunca no inglês original
4. **Reduções de texto sempre com aprovação humana** — LLM propõe, humano decide
5. **Numeração original dos blocos preservada** em todos os arquivos derivados
6. **Nunca editar os `_en.srt` originais** — só derivados `_zh`
7. **TTS sem duration** — o encaixe acontece na sincronização; verificar o log de
   acelerados (>1.5x) ao final e tratar estouros (>2.0x) antes da entrega
8. **Chunk size = 15** para tradução — nunca menos (perde contexto)
9. **Nunca deixar `[FALTA TRADUZIR]`/tags no SRT que vai pro TTS** — OmniVoice lê em voz alta
10. **Se >40% rápidos (pré-TTS)**: sinalizar anomalia antes de continuar

## Parâmetros de referência

```python
PARAMS = {
    "CHARS_HI":      6.5,   # previsor chars/s (字/秒) — acima disso provável estouro
    "CHARS_LO":      1.5,   # limite inferior chars/s
    "MIN_DUR":       1.0,   # duração mínima após ceder tempo
    "WINDOW":        5,     # vizinhos para ajuste de timestamp
    "CHUNK_SIZE":    15,    # blocos por chunk de tradução
    "ATEMPO_ALERT":  1.5,   # aceleração que entra no log da sincronização
    "ATEMPO_MAX":    2.0,   # limite do ffmpeg — acima = não cabe no slot
    "VOL_ORIGINAL":  0.005, # volume do áudio original na sync — baixo p/ dublagem dominar
    "VOL_DUB":       2.5,   # volume da dublagem na sync (compensa TTS quieto; amix normalize=0)
}
```

## Estrutura de arquivos

```
Juan_bilibili/
  .dsh/skills/                  ← skills deste pipeline (DeepSeek Harness)
    dub-parse/SKILL.md
    dub-diagnostics/SKILL.md
    dub-translation/SKILL.md
    dub-syllable-rate/SKILL.md
    dub-pipeline-orchestrator/SKILL.md   ← este arquivo (entry point)
    dub-pipeline-agent/SKILL.md          ← agente orquestrador
  scripts/
    orquestrador_zh.py           ← CLI (--step full/srt/diag/tts/sync)
    transcritor.py               ← mlx-whisper
    traduzir_srt_zh.py           ← tradução → zh (DeepSeek)
    gerar_audios_zh.py           ← TTS OmniVoice (voz_juan_zh, SEM duration)
    sincronizar_dublagem.py      ← merge vídeo (atempo)
    verificar_audios.py          ← QC pós-TTS (Whisper, LANGUAGE="zh")
    converter_wav_mp3.py         ← wav → mp3
    split_video.py               ← split de vídeo
  referencias/
    voz_juan_zh.mp3              ← voz de referência (Juan falando chinês)
  pipeline_llm_local/            ← diagnóstico comparativo (build_diagnostic_summary)
  Em produção/                   ← vídeos aguardando processamento
  Prontos/<base>/                ← TUDO do vídeo finalizado (nada solto na raiz)
    <base>_medium_en.srt         ← transcrição original (nunca mexer)
    <base>_large-v3_en.srt       ← transcrição referência (nunca mexer)
    <base>_medium_zh.srt         ← traduzido (fonte do TTS)
    <base>_dublado_zh.mp4        ← vídeo final dublado
    <base>_acelerados.log        ← trechos acelerados >1.5x
    audios_blocos/               ← áudios TTS (bloco_N.mp3)
    <base>.mov / <base>.mp4      ← vídeo fonte (movido ao terminar o full)
```

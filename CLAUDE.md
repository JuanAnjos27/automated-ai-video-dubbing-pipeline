---
# Juan_bilibili — Manual de Bordo
*Lido automaticamente pelo agente em toda sessão (equivalente ao CLAUDE.md do canal-dublagem-jiang)*

---

## O que é este projeto

Pipeline de dublagem EN/PT → **chinês (mandarim)** automatizado, com a **voz do Juan**.
Replica o fluxo do canal-dublagem-jiang, mas com alvo chinês: transcrição dupla,
diagnóstico, tradução para chinês simplificado, TTS OmniVoice clonando a voz do Juan
falando chinês, e sincronização com o vídeo original.

---

## Estrutura da pasta

```
Juan_bilibili/
  .dsh/skills/                     ← skills do pipeline (DeepSeek Harness)
    dub-pipeline-agent/SKILL.md         ← agente orquestrador (entry point)
    dub-pipeline-orchestrator/SKILL.md  ← sequência + checkpoints + critérios
    dub-diagnostics/SKILL.md            ← diagnóstico medium×large + QA do _zh
    dub-translation/SKILL.md            ← tradução → zh via DeepSeek
    dub-syllable-rate/SKILL.md          ← ritmo mandarim (字/秒) + check_fit pós-TTS
    dub-parse/SKILL.md                  ← parse/escrita SRT
  scripts/
    orquestrador_zh.py             ← entry point: --step full/srt/diag/tts/sync
    transcritor.py                 ← mlx-whisper (medium + large-v3)
    traduzir_srt_zh.py             ← tradução → zh via DeepSeek API (JSON chunks 15)
    gerar_audios_zh.py             ← TTS OmniVoice (voz_juan_zh, SEM duration)
    sincronizar_dublagem.py        ← sincronização + merge vídeo (atempo)
    verificar_audios.py            ← QC pós-TTS (Whisper, LANGUAGE="zh")
    converter_wav_mp3.py           ← converte .wav → .mp3
    split_video.py                 ← split vídeo em partes
  referencias/
    voz_juan_zh.mp3                ← voz de referência (Juan falando chinês)
  pipeline_llm_local/              ← diagnóstico comparativo (build_diagnostic_summary)
    diagnostics.py, srt_utils.py, models.py, ...
    reports/                       ← saídas de QA e diagnósticos (.json)
  Em produção/                     ← vídeos aguardando processamento
  Prontos/<base>/                  ← TUDO do vídeo finalizado (nada fica solto na raiz)
    <base>_medium_en.srt           ← transcrição original (nunca mexer)
    <base>_large-v3_en.srt         ← transcrição referência (nunca mexer)
    <base>_medium_zh.srt           ← pós-tradução (chinês) — FONTE DO TTS
    <base>_dublado_zh.mp4          ← vídeo final dublado
    <base>_acelerados.log          ← trechos acelerados >1.5x
    audios_blocos/                 ← saída TTS: bloco_N.mp3
    <base>.mov / <base>.mp4        ← vídeo fonte (movido ao terminar o full)
```

**Python:** `python3` com o venv do projeto (torch, omnivoice, pydub, requests, mlx-whisper)

---

## Pipeline (5 etapas)

```
1. Transcrição dupla  → _medium_en.srt + _large-v3_en.srt  (mlx-whisper, Apple Silicon)
2. Diagnóstico         → compara medium × large, detecta repetições/gaps/janelas severas
3. Tradução + QA       → _medium_zh.srt via DeepSeek (deepseek-chat), QA de idioma chinês
4. TTS                 → OmniVoice voice cloning com voz_juan_zh.mp3, SEM duration
5. Sincronização       → merge áudio dublado + vídeo original (atempo, volume orig 0.005, dub 2.5)
```

### Comandos principais

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

# Flags: --idioma en|pt (força idioma da FONTE), --lang zh|es|en|... (idioma ALVO, default zh)
#        --inicio N, --fim N, --num-step N, --no-qa

# Multi-idioma: o pipeline é agnóstico de idioma (qualquer fonte → qualquer alvo).
# Pré-configurado PT/EN → ZH. Ao trocar o alvo: 1) confirmar suporte do OmniVoice,
# 2) fornecer voz de referência no idioma alvo (--ref-audio/--ref-text no TTS),
# 3) opcional: --style-hint próprio na tradução.

# Tudo gerado vai para Prontos/<base>/ — ao final do --step full o vídeo fonte
# também é movido para lá. A raiz do projeto fica sempre limpa.
```

---

## Convenções de arquivo (NUNCA editar originais)

```
<base>_medium_en.srt           ← transcrição original (idioma do vídeo, nunca mexer)
<base>_large-v3_en.srt         ← transcrição referência (idioma do vídeo, nunca mexer)
<base>_medium_zh.srt           ← pós-tradução (chinês) — FONTE DO TTS
<base>_medium_zh_virgem.srt    ← tradução crua (pré-correções)
<base>_medium_zh_correcoes.srt ← registro do que foi corrigido no QA (diff)
<base>_dublado_zh.mp4          ← vídeo final dublado
<base>_acelerados.log          ← trechos acelerados >1.5x na sincronização

TODOS os arquivos acima vivem em Prontos/<base>/ — nunca na raiz do projeto.
```

---

## Voice cloning — Juan (chinês)

- Ref audio: `referencias/voz_juan_zh.mp3`
- Ref text: "来来来,孩子,坐下喝杯茶,你看这院子里的老槐树跟着咱家几十年了,你爷爷在的时候,每年夏天都在这树底下乘凉下棋。"
- O TTS (`gerar_audios_zh.py`) **NÃO passa duration** — o áudio sai com a duração natural
  que o modelo escolhe. O encaixe no slot do SRT (speedup via `atempo`) é feito na
  etapa de SINCRONIZAÇÃO.

---

## Limites de ritmo (mandarim — 字/秒)

- CHARS_HI = 6.5 字/s (previsor pré-TTS: acima disso, provável estouro do slot)
- CHARS_LO = 1.5 字/s (abaixo → lento/pausa)
- Fala mandarim natural: ~3.5–5.5 字/s
- ATEMPO_ALERT = 1.5x (entra no log da sincronização)
- ATEMPO_MAX = 2.0x (limite do ffmpeg — acima, o áudio NÃO cabe no slot)
- pyphen NÃO se aplica ao chinês — contar hanzi (CJK), não sílabas

---

## Decisões importantes

1. **TTS sem duration** — diferente do canal PT-BR (worker com duration), aqui o OmniVoice gera com duração natural e o `atempo` da sincronização faz o encaixe (limite 2.0x)
2. **Whisper em sequência, nunca paralelo** — CPU/GPU satura e cada processo fica mais lento
3. **Tags no SRT** — nunca deixar `[QA-FALHA]`/`[FALTA TRADUZIR]` no texto que vai pro TTS, o OmniVoice lê em voz alta
4. **Versionamento SRT** — nunca editar os originais `_en.srt`, sempre criar novos com sufixo
5. **Nomes próprios em chinês** — manter em latim OU transliterar, mas de forma consistente no vídeo inteiro
6. **verificar_audios.py** usa Whisper com LANGUAGE="zh" (já corrigido) — transcreve cada MP3 e compara com o SRT esperado
7. **Skills no `.dsh/skills/`** — o DeepSeek Harness descobre automaticamente (formato `<nome>/SKILL.md` com frontmatter `name` + `description`)
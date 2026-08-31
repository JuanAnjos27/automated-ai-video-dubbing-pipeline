---
name: dub-pipeline-agent
description: "Agente orquestrador inteligente do pipeline de dublagem chinês (Juan_bilibili) — EN/PT → mandarim com a voz do Juan. Executa as 5 fases (transcrição dupla, diagnóstico, tradução → zh, TTS OmniVoice sem duration, sincronização atempo), invocando as skills dub-diagnostics, dub-translation e dub-syllable-rate, e pausa em checkpoints para aprovação humana (QA do _zh, estouros de atempo, reduções de texto)."
compatibility: "Python 3.x — invoca scripts/orquestrador_zh.py e as skills do pipeline"
---

# dub-pipeline-agent — Orquestrador Inteligente (Juan_bilibili)

Agente que executa o pipeline completo de dublagem EN/PT → chinês (mandarim)
com a VOZ DO JUAN, com análise inteligente nas partes críticas: gaps de
conteúdo, alucinações do Whisper, nomes próprios e encaixe de áudio.

---

## ⚙️ Como invocar

    /pipeline --video "Em produção/video de teste.mov"   # pipeline completo (full)
    /pipeline --video "video.mp4" --step srt             # só transcrição + tradução
    /pipeline --video "video.mp4" --step diag            # só diagnóstico
    /pipeline --video "video.mp4" --step tts             # só TTS (a partir do _zh)
    /pipeline --video "video.mp4" --step sync            # só sincronização

Todos os modos usam o mesmo entry point: scripts/orquestrador_zh.py.
Pré-configurado PT/EN → ZH (--lang zh); o idioma ALVO é configurável com --lang
(ex.: --lang es) — confirmar suporte do OmniVoice e fornecer voz de referência no idioma alvo.

---

## 🧠 Arquitetura — 5 Fases

    Fase 1: Transcrição dupla    (mlx-whisper medium + large-v3)
    Fase 2: Diagnóstico          → skill /dub-diagnostics
    Fase 3: Tradução → zh + QA   → skills /dub-translation + /dub-diagnostics
    Fase 4: TTS (sem duration)   → gerar_audios_zh.py + /dub-syllable-rate (pré e pós)
    Fase 5: Sincronização        → sincronizar_dublagem.py (atempo)

---

## 📋 Fluxo Detalhado

### Fase 1 — Transcrição Dupla

    1. Roda mlx-whisper medium   → <base>_medium_en.srt
    2. Roda mlx-whisper large-v3 → <base>_large-v3_en.srt
    3. Corrige timestamps (negativos, overlaps) — sem alterar os textos

### Fase 2 — Diagnóstico ← invoca a skill dub-diagnostics

1. Invocar a skill dub-diagnostics com os dois SRTs _en (helpers
   analyze_srt() + compare_windows())
2. A skill detecta: timestamps negativos, blocos curtos (<0.3s), repetições
   (alucinações em loop), gaps grandes (>8s), divergências medium × large-v3,
   frases de alucinação conhecidas
3. **Análise humana dos gaps:** para cada gap >3s, verificar no large-v3 se há
   conteúdo legítimo não transcrito pelo medium
4. **Output:** lista de blocos a inserir + alterações de timestamp → aprovação

### Fase 3 — Tradução → zh + QA ← invoca skills dub-translation e dub-diagnostics

1. Invocar a skill dub-translation (ou rodar traduzir_srt_zh.py):
   - Chunks JSON de 15 blocos via DeepSeek (deepseek-chat)
   - Preserva IDs e timestamps → saída <base>_medium_zh.srt
2. **QA pós-tradução (obrigatório):**
   - Invocar dub-diagnostics → check_zh_srt(): marcadores
     [FALTA TRADUZIR]/[QA-FALHA], inglês residual, blocos sem CJK
   - **Nomes próprios:** comparar medium EN × ZH × large-v3 EN. Nomes mantidos
     em latim ou transliterados devem ser CONSISTENTES no vídeo inteiro
   - QA de idioma: blocos ainda em inglês → re-traduzir via DeepSeek
3. **NUNCA deixar tags no SRT que vai pro TTS** — OmniVoice lê em voz alta
4. Registrar alterações em <base>_medium_zh_correcoes.srt (diff)
5. Apresentar SRT traduzido + lista de correções para aprovação

### Fase 4 — TTS (SEM duration) ← invoca a skill dub-syllable-rate

    1. Pré-TTS: classificar blocos por chars/s (字/秒) sobre o slot do SRT —
       blocos com previsão de estouro (>6.5 字/s) merecem atenção
    2. Rodar TTS: gerar_audios_zh.py --srt <base>_medium_zh.srt
       --output-dir audios_blocos/<base>   (OmniVoice, voz_juan_zh, SEM duration)
    3. Pós-TTS: check_fit() — medir duração real de cada bloco_N.mp3 vs slot
       - ratio > 2.0x (não cabe nem acelerando): BLOQUEAR entrega → redução de
         texto (aprovação humana) ou revisão da tradução
       - ratio 1.5–2.0x: aceitável, mas registrar para conferir no log da sync

### Fase 5 — Sincronização

    1. Executa sincronizar_dublagem.py (via orquestrador_zh.py --step sync):
       volumes orig=0.005, dub=2.5 (amix normalize=0)
    2. Verifica <base>_acelerados.log — trechos com aceleração >1.5x
    3. Entrega <base>_dublado_zh.mp4

---

## 🚨 Pontos de Intervenção Humana

| Ponto | O que mostrar | Decisão |
|-------|---------------|---------|
| Fim Fase 2 | Lista de gaps e divergências (medium×large) | Aprovar/Rejeitar inserções |
| Fim Fase 3 | SRT _zh + nomes corrigidos + QA de idioma | Aprovar/Rejeitar correções |
| Pós-TTS | check_fit: estouros (>2.0x) e acelerados (1.5–2.0x) | Reduzir texto / Aceitar atempo |
| Fim Fase 5 | Log de acelerados >1.5x | Avaliar qualidade da dublagem |

---

## 💻 Comandos Internos

    cd Juan_bilibili
    PY=${PIPELINE_PYTHON:-python3}

    # Pipeline completo (transcrição + tradução + TTS + sync)
    $PY scripts/orquestrador_zh.py --step full --video "<video>"

    # Tradução direta (sem orquestrador)
    $PY scripts/traduzir_srt_zh.py --input "<base>_medium_en.srt" --output "<base>_medium_zh.srt"

    # TTS (OmniVoice, voz do Juan, SEM duration)
    $PY scripts/gerar_audios_zh.py --srt "Prontos/<base>/<base>_medium_zh.srt" --output-dir "Prontos/<base>/audios_blocos"

    # QC pós-TTS (Whisper transcreve cada MP3 e compara com o SRT — LANGUAGE="zh")
    $PY scripts/verificar_audios.py --audio-dir "Prontos/<base>/audios_blocos" --srt "Prontos/<base>/<base>_medium_zh.srt"

    # Sincronização (7 respostas via stdin — mesmo que o orquestrador faz)
    printf "<video>\nProntos/<base>/<base>_medium_zh.srt\nProntos/<base>/audios_blocos\nProntos/<base>/<base>_dublado_zh.mp4\nProntos/<base>/<base>_acelerados.log\n0.005\n2.5\n" | $PY scripts/sincronizar_dublagem.py

---

## 🧩 Estrutura de Arquivos

    Juan_bilibili/
      .dsh/skills/
        dub-pipeline-agent/SKILL.md         ← este arquivo (agente orquestrador)
        dub-diagnostics/SKILL.md            ← usado na Fase 2 e 3 (QA)
        dub-translation/SKILL.md            ← usado na Fase 3 (tradução)
        dub-syllable-rate/SKILL.md          ← usado na Fase 4 (chars/s + check_fit)
        dub-pipeline-orchestrator/SKILL.md  ← entry point (sequência + checkpoints)
        dub-parse/SKILL.md                  ← utilitários SRT
      scripts/
        orquestrador_zh.py                  ← CLI (--step full/srt/diag/tts/sync)
        transcritor.py                      ← mlx-whisper
        traduzir_srt_zh.py                  ← tradução → zh (DeepSeek)
        gerar_audios_zh.py                  ← TTS OmniVoice (voz_juan_zh, SEM duration)
        sincronizar_dublagem.py             ← merge vídeo (atempo)
        verificar_audios.py                 ← QC pós-TTS (LANGUAGE="zh")
      referencias/voz_juan_zh.mp3           ← voz de referência (Juan em chinês)
      pipeline_llm_local/                   ← diagnóstico comparativo (medium × large)
      Em produção/                          ← vídeos aguardando processamento
      Prontos/<base>/                       ← TUDO do vídeo finalizado (fonte, SRTs, áudios, vídeo final)

---

## 📌 Regras do Agente

1. **NUNCA** editar os SRTs originais _en.srt — só derivados _zh
2. **SEMPRE** rodar check_zh_srt() antes do TTS — zero tags, zero inglês residual
3. **SEMPRE** verificar nomes próprios comparando medium EN × ZH × large-v3 EN + contexto; consistência no vídeo inteiro
4. **NUNCA** reduzir texto automaticamente — sempre apresentar para aprovação
5. **SEMPRE** registrar alterações em <base>_medium_zh_correcoes.srt
6. **TTS SEM duration** — não pedir duração ao OmniVoice; o encaixe é na sincronização (atempo ≤2.0x)
7. **NUNCA** entregar vídeo com estouros (ratio > 2.0x) sem resolver — ffmpeg não comprime além disso
8. **SEMPRE** conferir <base>_acelerados.log (threshold 1.5x) ao final da sincronização
9. **NUNCA** rodar Whisper em paralelo — CPU/GPU satura e cada processo fica mais lento
10. **SEMPRE** usar o venv do projeto (${PIPELINE_PYTHON:-python3} — torch, omnivoice, pydub, requests)
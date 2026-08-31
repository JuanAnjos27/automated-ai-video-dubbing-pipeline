---
name: dub-syllable-rate
description: "Análise de ritmo de fala para o pipeline chinês (Juan_bilibili): taxa de caracteres chineses por segundo (字/秒) por bloco SRT, previsão de estouro do slot e verificação pós-TTS da razão atempo (duração real do áudio vs slot). pyphen é PT-BR e NÃO se aplica ao mandarim — aqui o mandarim é contado por hanzi (caracteres), e como o TTS OmniVoice não controla duração, o encaixe acontece na sincronização (atempo)."
compatibility: "Python 3.x — sem dependências externas (não usa pyphen)"
---

# Speech Rate Analysis (Mandarim — 字/秒)

## Por que não pyphen?

O canal PT-BR conta **sílabas** com pyphen `pt_BR`. Chinês não tem sílabas
contáveis desse jeito: cada hanzi (汉字) é uma sílaba. A métrica natural é
**caracteres por segundo (字/秒)**.

> ⚠️ **Multi-idioma:** esta métrica é específica do mandarim. Para outros
> idiomas alvo, adapte a contagem (ex.: sílabas com pyphen para pt/es,
> moras/fonemas para ja/ko) e recalibre HI/LO — a lógica de previsão e o
> `check_fit()` pós-TTS (razão atempo) continuam válidos em qualquer idioma.

Além disso, no pipeline chinês o TTS
(`gerar_audios_zh.py`) **não controla duração** — o áudio sai com a duração
natural que o modelo escolhe e o encaixe é feito na sincronização via `atempo`
(speedup, limite 2.0x do ffmpeg). Por isso esta skill opera em **duas fases**:

1. **Pré-TTS (previsão):** chars/s sobre o slot do SRT → estima se o áudio TTS
   provavelmente vai estourar o slot.
2. **Pós-TTS (verificação):** mede a duração real do `bloco_N.mp3` vs o slot →
   razão atempo efetiva. É esta a fonte da verdade.

## Parâmetros calibrados (mandarim)

```python
CHARS_HI  = 6.5   # previsor: acima disso o áudio TTS provavelmente estoura o slot
CHARS_LO  = 1.5   # abaixo disso é pausa/silêncio ou bloco de fala curta
MIN_DUR   = 1.0   # duração mínima que um bloco pode ter após ceder tempo
WINDOW    = 5     # vizinhos a considerar para cada lado no ajuste
ATEMPO_ALERT = 1.5  # aceleração que exige atenção (threshold do log de sincronização)
ATEMPO_MAX   = 2.0  # limite do ffmpeg — acima disso o áudio NÃO cabe no slot
```

## Contagem de caracteres (hanzi)

```python
import re

CJK = re.compile(r'[\u4e00-\u9fff]')  # caracteres chineses (hanzi)

def count_chars(text):
    """Número de hanzi no texto (≈ sílabas em mandarim). Ignora pontuação e latim."""
    return len(CJK.findall(text or ''))

def get_cps(text, duration):
    """Taxa de caracteres por segundo (字/秒)."""
    if duration <= 0.1: return 999
    return count_chars(text) / duration
```

**Fala mandarim natural:** ~3.5–5.5 字/秒. Um slot que exige >6.5 字/秒 é
sinal de que a fala TTS (ritmo natural ~4–5 字/秒) vai exceder o slot.

## Classificação pré-TTS (por slot do SRT)

```python
def classify_all(order, starts, ends, texts):
    result = {'ok': [], 'fast': [], 'slow': [], 'corrupt': [],
              'critical': [], 'moderate': [], 'marginal': []}
    for bid in order:
        dur = ends[bid] - starts[bid]
        if dur <= 0.3:
            result['corrupt'].append(bid)
            continue
        cps = get_cps(texts[bid], dur)
        if cps > CHARS_HI:
            result['fast'].append(bid)
            if cps >= 8.0:   result['critical'].append(bid)
            elif cps >= 7.0: result['moderate'].append(bid)
            else:            result['marginal'].append(bid)
        elif cps < CHARS_LO:
            result['slow'].append(bid)
        else:
            result['ok'].append(bid)
    return result

def print_stats(classification, total):
    c = classification
    ok = len(c['ok'])
    fast = len(c['fast'])
    print(f"✅ OK      : {ok} ({100*ok/total:.1f}%)")
    print(f"🔴 Rápidos : {fast} ({100*fast/total:.1f}%)")
    print(f"  Críticos : {len(c['critical'])} (≥8.0 字/s)")
    print(f"  Moderados: {len(c['moderate'])} (7.0–8.0 字/s)")
    print(f"  Marginais: {len(c['marginal'])} (<7.0 字/s — aceitáveis)")
    print(f"🔵 Lentos  : {len(c['slow'])} ({100*len(c['slow'])/total:.1f}%)")
    print(f"⚫ Corrompido: {len(c['corrupt'])}")
```

## Pós-TTS: verificação real de encaixe (razão atempo)

Depois do TTS, medir cada `Prontos/<base>/audios_blocos/bloco_N.mp3` contra o slot do
SRT — esta é a verificação que decide se a sincronização vai ficar boa:

```python
import os
from pydub import AudioSegment

def check_fit(blocks, audio_dir, prefix='bloco_', ext='.mp3'):
    """Mede duração real de cada áudio vs slot do SRT. Retorna razão atempo."""
    report = {'ok': [], 'alert': [], 'overflow': []}
    for b in blocks:
        path = os.path.join(audio_dir, f"{prefix}{b['id']}{ext}")
        if not os.path.exists(path): continue
        audio_ms = len(AudioSegment.from_file(path))       # duração real do TTS
        slot_ms  = (b['end'] - b['start']) * 1000
        ratio = audio_ms / slot_ms if slot_ms > 0 else 999
        entry = {'id': b['id'], 'audio_s': audio_ms/1000, 'slot_s': slot_ms/1000,
                 'ratio': round(ratio, 3)}
        if ratio > ATEMPO_MAX:        # não cabe nem a 2.0x → precisa redução de texto
            report['overflow'].append(entry)
        elif ratio > ATEMPO_ALERT:    # aceleração forte (log de sincronização)
            report['alert'].append(entry)
        else:
            report['ok'].append(entry)
    return report

def print_fit(report):
    print(f"\nENCAIXE PÓS-TTS (bloco_N.mp3 vs slot SRT)")
    print(f"  ✅ OK        : {len(report['ok'])} (atempo ≤ {ATEMPO_ALERT}x)")
    print(f"  ⚠️  Acelerados: {len(report['alert'])} (atempo {ATEMPO_ALERT}–{ATEMPO_MAX}x — no log da sync)")
    print(f"  ❌ Estouro   : {len(report['overflow'])} (precisa > {ATEMPO_MAX}x — não cabe!)")
    for e in sorted(report['overflow'], key=lambda x: -x['ratio'])[:15]:
        print(f"    #{e['id']}: áudio {e['audio_s']:.1f}s vs slot {e['slot_s']:.1f}s → {e['ratio']:.2f}x")
```

## Ajuste de timestamps por vizinhos (pré-TTS)

Mesmo algoritmo do canal PT-BR, trocando sílabas por hanzi:

```python
def can_give(bid, starts, ends, texts):
    """ATENCAO: starts/ends devem ser as copias de trabalho (mutaveis)."""
    dur = ends[bid] - starts[bid]
    min_needed = count_chars(texts[bid]) / CHARS_HI
    return max(0.0, dur - min_needed - MIN_DUR)

def resolve_by_timestamps(fast_ids, order, starts, ends, texts):
    unresolved = []
    for bid in sorted(fast_ids, key=lambda b: get_cps(texts[b], ends[b]-starts[b]), reverse=True):
        if get_cps(texts[bid], ends[bid]-starts[bid]) <= CHARS_HI: continue
        extra = count_chars(texts[bid]) / CHARS_HI - (ends[bid] - starts[bid])
        if extra <= 0.001: continue
        remaining = extra
        i = order.index(bid)
        for delta in range(1, WINDOW+1):
            if remaining <= 0.001: break
            if i-delta >= 0:
                did = order[i-delta]
                avail = can_give(did, starts, ends, texts)
                if avail > 0.001:
                    give = min(avail, remaining)
                    if ends[did] - give >= starts[did] + MIN_DUR:
                        ends[did] -= give; starts[bid] -= give; remaining -= give
            if remaining <= 0.001: break
            if i+delta < len(order):
                did = order[i+delta]
                avail = can_give(did, starts, ends, texts)
                if avail > 0.001:
                    give = min(avail, remaining)
                    if starts[did] + give <= ends[did] - MIN_DUR:
                        starts[did] += give; ends[bid] += give; remaining -= give
        if remaining > 0.001:
            unresolved.append(bid)
    return unresolved
```

## Verificação de redução de texto

```python
def verify_reduction(new_text, duration):
    cps = round(count_chars(new_text) / duration, 2)
    return {'ok': cps <= CHARS_HI, 'cps': cps, 'max_chars': int(duration * CHARS_HI)}
```

## Regras
- Análise SEMPRE no texto chinês traduzido (`_zh`), nunca no inglês original
- **Não usar pyphen** — contar hanzi (CJK) com `count_chars`
- Como o TTS não controla duração, o **pré-TTS é previsão**; o **pós-TTS (check_fit) é a fonte da verdade**
- Estouro (ratio > 2.0x): o ffmpeg NÃO consegue comprimir o suficiente → reduzir texto (aprovação humana) ou revisar tradução
- Acelerados (1.5–2.0x): aceitáveis, mas verificar no log `<base>_acelerados.log` após a sincronização
- Tentar ajuste de timestamp ANTES de propor redução de texto
- Reduções sempre com aprovação humana — nunca automáticas
- Se >40% rápidos: sinalizar anomalia antes de continuar

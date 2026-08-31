---
name: dub-diagnostics
description: "Análise completa de qualidade de um SRT do pipeline chinês (Juan_bilibili): timestamps negativos, blocos curtos, repetições/alucinações, gaps grandes, divergências medium × large-v3 e QA do SRT chinês (inglês residual, marcadores [FALTA TRADUZIR]/[QA-FALHA]). Use na Etapa 2 (diagnóstico) e no pós-tradução (QA do _zh)."
compatibility: "Python 3.x — sem dependências externas"
---

# SRT Diagnostics (Juan_bilibili — PT/EN → ZH)

## Análise de um SRT isolado

```python
import re, statistics, collections

def analyze_srt(blocks, label="SRT"):
    def parse_time(t):
        h,m,s = t.split(':'); s,ms = s.split(',')
        return int(h)*3600+int(m)*60+int(s)+int(ms)/1000

    durs = [parse_time(b['end'])-parse_time(b['start']) for b in blocks]
    valid = [d for d in durs if d > 0]

    # Timestamps negativos
    neg = [(b, parse_time(b['end'])-parse_time(b['start']))
           for b in blocks if parse_time(b['end'])-parse_time(b['start']) <= 0]

    # Blocos muito curtos
    short = [(b, parse_time(b['end'])-parse_time(b['start']))
             for b in blocks if 0 < parse_time(b['end'])-parse_time(b['start']) < 0.3]

    # Repetições consecutivas (alucinações em loop)
    reps = []
    for i in range(1, len(blocks)):
        a = blocks[i-1]['text'].strip().lower()
        b = blocks[i]['text'].strip().lower()
        if a == b or (len(a) > 20 and (a in b or b in a)):
            reps.append((blocks[i-1], blocks[i]))

    # Gaps grandes
    gaps = []
    for i in range(len(blocks)-1):
        gap = parse_time(blocks[i+1]['start']) - parse_time(blocks[i]['end'])
        if gap > 8:
            gaps.append((blocks[i], blocks[i+1], gap))

    # Blocos longos (possível loop)
    long_b = [(b, parse_time(b['end'])-parse_time(b['start']))
              for b in blocks if parse_time(b['end'])-parse_time(b['start']) > 20]

    # Frases de alucinação (idioma ORIGINAL do vídeo — geralmente inglês)
    hall_phrases = ['thank you for watching','please subscribe','like and subscribe',
                    'see you next','.com','♪','[music]','[applause]']
    hall = [(b,p) for b in blocks for p in hall_phrases if p in b['text'].lower()]

    return {
        'label': label,
        'total': len(blocks),
        'duration': blocks[-1]['end'] if blocks else '00:00:00,000',
        'dur_mean': round(statistics.mean(valid),2) if valid else 0,
        'dur_median': round(statistics.median(valid),2) if valid else 0,
        'neg_timestamps': neg,
        'short_blocks': short,
        'repetitions': reps,
        'gaps': gaps,
        'long_blocks': long_b,
        'hallucinations': hall,
    }

def print_report(report):
    r = report
    print(f"\n{'='*60}")
    print(f"DIAGNÓSTICO — {r['label']}")
    print(f"{'='*60}")
    print(f"Blocos: {r['total']} | Fim: {r['duration']}")
    print(f"Dur. média: {r['dur_mean']}s | mediana: {r['dur_median']}s")
    print(f"Negativos : {len(r['neg_timestamps'])} {'✅' if not r['neg_timestamps'] else '❌'}")
    print(f"Curtos<0.3: {len(r['short_blocks'])} {'✅' if not r['short_blocks'] else '⚠️'}")
    print(f"Repetições: {len(r['repetitions'])} {'✅' if not r['repetitions'] else '⚠️'}")
    print(f"Gaps >8s  : {len(r['gaps'])} {'✅' if not r['gaps'] else '⚠️'}")
    print(f"Longos>20s: {len(r['long_blocks'])} {'✅' if not r['long_blocks'] else '⚠️'}")
    print(f"Alucinação: {len(r['hallucinations'])} {'✅' if not r['hallucinations'] else '❌'}")
```

## Comparação medium vs large-v3

```python
def compare_windows(base_blocks, compare_blocks, window_secs=180):
    """Compara conteúdo por janelas de tempo. Divergências altas = alucinação no compare."""
    def parse_time(t):
        h,m,s = t.split(':'); s,ms = s.split(',')
        return int(h)*3600+int(m)*60+int(s)+int(ms)/1000

    def text_by_window(blocks, w):
        windows = {}
        for b in blocks:
            t = parse_time(b['start'])
            wk = int(t // w)
            windows.setdefault(wk, []).append(b['text'])
        return {k: ' '.join(v) for k,v in sorted(windows.items())}

    base_win = text_by_window(base_blocks, window_secs)
    comp_win = text_by_window(compare_blocks, window_secs)
    all_w = sorted(set(base_win) | set(comp_win))

    results = []
    for w in all_w:
        b = base_win.get(w, '')
        c = comp_win.get(w, '')
        diff = abs(len(b) - len(c))
        mins = w * (window_secs // 60)
        h = mins // 60; mn = mins % 60
        results.append({
            'label': f"{h:02d}h{mn:02d}m",
            'diff': diff,
            'base_len': len(b),
            'compare_len': len(c),
            'base_text': b[:200],
            'compare_text': c[:200],
            'severe': diff > 500
        })
    return results

def print_comparison(results):
    print(f"\nCOMPARAÇÃO MEDIUM vs LARGE-V3 (janelas de 3 min)")
    severe = [r for r in results if r['severe']]
    print(f"Janelas com divergência >500 chars: {len(severe)}")
    for r in results:
        flag = "⚠️ " if r['severe'] else "  "
        print(f"{flag}[{r['label']}] diff={r['diff']:>5} | MED={r['base_len']:>4} LRG={r['compare_len']:>4}")
```

## Verificação de nomes próprios

```python
def check_proper_nouns(blocks, suspects):
    """
    suspects = [('Pryam', 'Priam'), ('Axel', 'Alexander'), ...]
    Retorna quais variantes incorretas aparecem.
    """
    results = []
    for wrong, correct in suspects:
        count = sum(1 for b in blocks if wrong.lower() in b['text'].lower())
        if count > 0:
            examples = [b for b in blocks if wrong.lower() in b['text'].lower()][:2]
            results.append({'wrong': wrong, 'correct': correct, 'count': count, 'examples': examples})
    return results
```

## QA do SRT chinês (_zh) — pós-tradução

O TTS lê TUDO em voz alta: marcadores e texto em inglês vazam para o áudio. Checar SEMPRE no
`<base>_medium_zh.srt` antes da Etapa de TTS:

```python
import re as _re

ZH_RESIDUAL_MARKERS = ['[FALTA TRADUZIR]', '[QA-FALHA]', '[MUSIC]', '[APPLAUSE]']

def check_zh_srt(zh_blocks):
    """Detecta no SRT chinês: marcadores de falha, tags e texto em inglês residual."""
    issues = []
    latin_words = _re.compile(r'[A-Za-z]{2,}')

    for b in zh_blocks:
        text = b['text']
        for m in ZH_RESIDUAL_MARKERS:
            if m.lower() in text.lower():
                issues.append({'id': b['id'], 'type': 'marker', 'marker': m, 'text': text[:80]})
        # Palavras latinas fora de nomes próprios = inglês não traduzido
        for w in latin_words.findall(text):
            if w.lower() not in {'ok', 'tv', 'usa', 'cnn', 'bbc', 'srt'}:
                issues.append({'id': b['id'], 'type': 'latin', 'word': w, 'text': text[:80]})
                break

    # Blocos sem nenhum caractere CJK = provavelmente não traduzidos
    cjk = _re.compile(r'[\u4e00-\u9fff]')
    untranslated = [b['id'] for b in zh_blocks if not cjk.search(b['text']) and b['text'].strip()]
    return {'issues': issues, 'untranslated_ids': untranslated}
```

**Nomes próprios em chinês:** a tradução pode manter nomes em alfabeto latino (decisão do
`traduzir_srt_zh.py`: "Maintain all proper nouns exactly as-is") ou transliterar (音译).
O que NÃO pode acontecer: o mesmo nome aparecer de duas formas diferentes. Se o QA achar
divergências, comparar com o EN original (medium × large-v3 + contexto) e padronizar.

## Regras de interpretação
- **Repetições no large-v3**: sempre alucinação — usar medium como fallback nessa região
- **Repetições no medium**: geralmente fala real do professor (ênfase) — manter
- **Divergência >500 chars**: investigar se é alucinação do large-v3 ou conteúdo perdido no medium
- **Gaps >8s**: comparar com large-v3 para determinar se é pausa real ou conteúdo perdido
- **Medium como base padrão**: mais estável, menos alucinações, segmentação mais limpa
- **No SRT _zh**: zero marcadores, zero texto em inglês (exceto nomes próprios mantidos por decisão), zero blocos sem CJK

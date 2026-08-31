---
name: dub-srt-parse
description: "Funções utilitárias para ler, escrever e manipular arquivos SRT de legenda do pipeline chinês (Juan_bilibili). Use sempre que precisar carregar um SRT, converter timestamps, salvar o resultado ou verificar integridade (sobreposições, durações negativas, blocos curtos). Nomenclatura do projeto: _en = transcrição original (nunca editar), _zh = traduzido (fonte do TTS)."
compatibility: "Python 3.x — sem dependências externas"
---

# SRT Parse & Utils (Juan_bilibili — PT/EN → ZH)

Pipeline chinês: os nomes derivados seguem o padrão do orquestrador:

- `<base>_medium_en.srt` / `<base>_large-v3_en.srt` — transcrições originais (idioma do vídeo, NUNCA editar)
- `<base>_medium_zh.srt` — traduzido para chinês simplificado (fonte do TTS)
- `<base>_medium_zh_virgem.srt` — tradução crua antes de correções
- `<base>_medium_zh_correcoes.srt` — diff: apenas blocos alterados pós-QA

## Funções essenciais

```python
import re

def parse_srt(path):
    with open(path, encoding='utf-8') as f:
        content = f.read()
    blocks = re.split(r'\n\n+', content.strip())
    result = []
    for b in blocks:
        lines = b.strip().split('\n')
        if len(lines) < 3: continue
        try: idx = int(lines[0].strip())
        except: continue
        tm = re.match(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', lines[1])
        if not tm: continue
        result.append({
            'id': idx,
            'start': tm.group(1),
            'end': tm.group(2),
            'text': ' '.join(lines[2:]).strip()
        })
    return result

def parse_time(t):
    h, m, s = t.split(':')
    s, ms = s.split(',')
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

def format_time(secs):
    secs = max(0, secs)
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    ms = round((s % 1) * 1000)
    s = int(s)
    if ms >= 1000: ms -= 1000; s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def save_srt(order, starts, ends, texts, path):
    output = '\n\n'.join(
        f"{bid}\n{format_time(starts[bid])} --> {format_time(ends[bid])}\n{texts[bid]}"
        for bid in order
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(output)

def srt_to_dicts(blocks):
    """Converte lista de blocos em dicts mutáveis para ajuste de timestamps."""
    order  = [b['id'] for b in blocks]
    starts = {b['id']: parse_time(b['start']) for b in blocks}
    ends   = {b['id']: parse_time(b['end'])   for b in blocks}
    texts  = {b['id']: b['text'] for b in blocks}
    return order, starts, ends, texts
```

## Verificação de integridade

```python
def check_integrity(order, starts, ends, texts):
    overlaps = sum(1 for i in range(len(order)-1)
                   if starts[order[i+1]] < ends[order[i]] - 0.001)
    neg      = sum(1 for bid in order if ends[bid]-starts[bid] <= 0)
    short    = sum(1 for bid in order if 0 < ends[bid]-starts[bid] < 0.5)
    return {
        'overlaps': overlaps,   # deve ser 0
        'neg_dur': neg,         # deve ser 0
        'short_blocks': short,  # deve ser 0
        'total': len(order)
    }

def fix_overlaps(order, starts, ends):
    """Corrigir sobreposições após qualquer ajuste de timestamp."""
    for i in range(len(order)-1):
        if starts[order[i+1]] < ends[order[i]] - 0.001:
            starts[order[i+1]] = ends[order[i]]
```

## SRT de correções (diff) e split

```python
def gerar_srt_correcoes(original_blocks, final_texts, order, starts, ends, path):
    """Salva APENAS os blocos cujo texto foi alterado — para sobrescrever áudios já gerados.
    No pipeline chinês: comparar <base>_medium_zh_virgem.srt vs <base>_medium_zh.srt."""
    orig_map = {b['id']: b['text'].strip() for b in original_blocks}
    changed = [bid for bid in order if final_texts[bid].strip() != orig_map.get(bid, '')]
    if not changed:
        print("Nenhum texto foi alterado.")
        return 0
    output = '\n\n'.join(
        f"{bid}\n{format_time(starts[bid])} --> {format_time(ends[bid])}\n{final_texts[bid]}"
        for bid in changed
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(output)
    return len(changed)

def split_srt(order, starts, ends, texts, path_base, ratio=0.75):
    """Divide em parte1 (75%) e parte2 (25%) mantendo numeração original."""
    split = int(len(order) * ratio)
    for suffix, ids in [('parte1', order[:split]), ('parte2', order[split:])]:
        out = '\n\n'.join(
            f"{bid}\n{format_time(starts[bid])} --> {format_time(ends[bid])}\n{texts[bid]}"
            for bid in ids
        )
        with open(f"{path_base}_{suffix}.srt", 'w', encoding='utf-8') as f:
            f.write(out)
    print(f"Parte1: #{order[0]}–#{order[split-1]} ({split} blocos)")
    print(f"Parte2: #{order[split]}–#{order[-1]} ({len(order)-split} blocos)")
```

## Regras
- Sempre usar `fix_overlaps` após qualquer ajuste de timestamp
- `gerar_srt_correcoes` compara texto por texto — nunca salva todos os blocos
- `format_time` nunca gera valores negativos — usa `max(0, secs)`
- Nunca salvar por cima dos `_en.srt` originais — só `_zh` é gravável

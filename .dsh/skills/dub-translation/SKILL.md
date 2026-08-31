---
name: dub-translation
description: "Tradução de blocos SRT para chinês simplificado (mandarim) no pipeline Juan_bilibili. Provider prioritário: DeepSeek API (modelo deepseek-chat) com chunks JSON de 15 blocos preservando IDs e timestamps; fallback Ollama local (Qwen 2.5 14B). Inclui QA de fronteiras, retomada por checkpoint e verificação de inglês residual. Use na Etapa 3, após diagnóstico/correção do _en e antes do TTS."
compatibility: "Python 3.x — requer: requests (ou urllib padrão). Implementação de referência: scripts/traduzir_srt_zh.py"
---

# SRT Translation → 中文 (Simplified Chinese) — DeepSeek API

## Diferença-chave vs o canal PT-BR

O alvo DEFAULT é **chinês simplificado (mandarim)**, não PT-BR. O estilo preserva
o tom informal de aula do professor, mantém nomes próprios exatamente como estão
(ou transliterados de forma consistente) e usa mandarim natural.

**Multi-idioma:** o alvo é configurável via `--idioma` (ex.: `--idioma es`,
`--idioma pt-br`, `--idioma ja`) com `--style-hint` opcional. O orquestrador
passa `--lang` automaticamente.

## Cliente DeepSeek

```python
import os, requests, re, time, json

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")   # NUNCA commitar a chave — use variável de ambiente
DEEPSEEK_MODEL   = "deepseek-chat"   # modelo usado pelo traduzir_srt_zh.py
DEEPSEEK_URL     = "https://api.deepseek.com/chat/completions"
TEMPERATURE      = 0.3
MAX_TOKENS       = 4000

def call_llm(prompt, api_key=DEEPSEEK_API_KEY, model=DEEPSEEK_MODEL):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content":
                "You are a Chinese (Simplified) translator. Return ONLY a valid JSON array. "
                "No markdown, no explanations, no extra text."},
            {"role": "user", "content": prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    resp = requests.post(DEEPSEEK_URL, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
```

## Style hint (mandarim)

```python
STYLE_HINT = (
    "Academic lecture style. Maintain all proper nouns and names exactly as-is. "
    "Keep the informal classroom lecture tone. "
    "Use natural Simplified Chinese (Mandarin)."
)
```

## Tradução de chunk (formato JSON — o mais robusto)

```python
def build_chunk_prompt(chunk):
    items = [{"id": b["id"], "start": b["start"], "end": b["end"], "text": b["text"]} for b in chunk]
    payload_json = json.dumps(items, ensure_ascii=False, indent=2)
    return (
        "Translate the 'text' field of each block to Simplified Chinese (Mandarin).\n"
        f"Style: {STYLE_HINT}\n"
        "Rules:\n"
        "1) Preserve 'id', 'start', 'end' fields exactly as-is.\n"
        "2) Translate ONLY the 'text' field.\n"
        "3) Return ONLY a valid JSON array with the same blocks, same order.\n"
        "4) No markdown, no explanations, no extra text.\n\n"
        f"Input JSON:\n{payload_json}"
    )

def parse_json_response(raw, original_chunk):
    text = raw.strip()
    if text.startswith("```"):                      # remove code fences
        lines = text.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start_idx = text.find("["); end_idx = text.rfind("]")
        if start_idx != -1 and end_idx > start_idx:
            data = json.loads(text[start_idx:end_idx+1])
        else:
            raise RuntimeError(f"Resposta não contém JSON array:\n{text[:500]}")
    if isinstance(data, dict): data = [data]
    by_id = {b["id"]: b for b in original_chunk}
    result = []
    for item in data:
        if "id" not in item or "text" not in item: continue
        idx = int(item["id"])
        if idx not in by_id: continue
        result.append({"id": idx, "start": by_id[idx]["start"],
                       "end": by_id[idx]["end"], "text": str(item["text"]).strip()})
    return result
```

## Tradução completa com fallback progressivo

```python
CHUNK_SIZE = 15

def translate_chunk(chunk):
    """Traduz um chunk com fallback progressivo (divide ao meio se falhar)."""
    def _try(items):
        raw = call_llm(build_chunk_prompt(items))
        return parse_json_response(raw, items)
    try:
        result = _try(chunk)
    except Exception:
        if len(chunk) <= 1: raise
        mid = len(chunk) // 2
        return translate_chunk(chunk[:mid]) + translate_chunk(chunk[mid:])
    by_id = {b["id"]: b for b in chunk}
    out_map = {b["id"]: b for b in result}
    missing = sorted(set(by_id) - set(out_map))
    if missing:                                    # retry dos IDs faltantes
        try:
            for b in _try([by_id[i] for i in missing]):
                out_map[b["id"]] = b
        except Exception:
            pass
    for idx in sorted(set(by_id) - set(out_map)):  # fallback final: marcar
        out_map[idx] = {"id": idx, "start": by_id[idx]["start"], "end": by_id[idx]["end"],
                        "text": f"[FALTA TRADUZIR] {by_id[idx]['text']}"}
    return [out_map[idx] for idx in sorted(by_id)]

def translate_srt(blocks, chunk_size=CHUNK_SIZE):
    chunks = [blocks[i:i+chunk_size] for i in range(0, len(blocks), chunk_size)]
    print(f"Traduzindo {len(blocks)} blocos → zh | {len(chunks)} chunks | DeepSeek {DEEPSEEK_MODEL}")
    out_lines = []
    for ci, chunk in enumerate(chunks):
        try:
            for b in translate_chunk(chunk):
                out_lines += [str(b["id"]), f"{b['start']} --> {b['end']}", b["text"], ""]
            print(f"  ✓ {ci+1}/{len(chunks)} #{chunk[0]['id']}–#{chunk[-1]['id']}")
        except Exception as e:
            for b in chunk:                        # fallback: mantém original marcado
                out_lines += [str(b["id"]), f"{b['start']} --> {b['end']}",
                              f"[FALTA TRADUZIR] {b['text']}", ""]
            print(f"  ✗ {ci+1}/{len(chunks)} ERRO: {e}")
        time.sleep(0.3)
    return "\n\n".join(out_lines)
```

## Verificação pós-tradução (QA de idioma — chinês)

```python
def verify_translation_zh(zh_blocks, expected_count):
    import re as _re
    cjk  = _re.compile(r'[\u4e00-\u9fff]')
    latin = _re.compile(r'[A-Za-z]{2,}')
    # nomes próprios mantidos em latim são aceitos (decisão do STYLE_HINT);
    # inglês residual REAL = blocos com várias palavras latinas e zero CJK
    problems = []
    for b in zh_blocks:
        text = b['text']
        if '[FALTA TRADUZIR]' in text or '[QA-FALHA]' in text:
            problems.append({'id': b['id'], 'type': 'marker'})
        elif not cjk.search(text) and len(latin.findall(text)) >= 2:
            problems.append({'id': b['id'], 'type': 'untranslated', 'text': text[:60]})
    return {
        'valid_blocks': len(zh_blocks),
        'expected': expected_count,
        'complete': len(zh_blocks) == expected_count,
        'problems': problems,
        'ok': len(problems) == 0,
    }
```

## QA de fronteiras de chunk (revisão de contexto)

Ao final, revisar as fronteiras onde os chunks cortam: mostrar 2 blocos antes e
depois de cada corte (#15→#16, #30→#31, ...) e checar fluência/continuidade do
mandarim. O `traduzir_srt_zh.py` já implementa isso (`qa_boundary_check`).

## Retomada por checkpoint

O `traduzir_srt_zh.py` salva a cada 5 chunks e pula blocos já traduzidos ao
recomeçar (lê os IDs existentes do arquivo de saída). Nunca re-traduzir do zero
se já existe `<base>_medium_zh.srt` parcial.

## Custo estimado (DeepSeek deepseek-chat)

| Vídeo | Blocos | Custo aprox. |
|-------|--------|--------------|
| 30 min | ~500   | ~$0.02       |
| 1h     | ~1000  | ~$0.05       |
| 1h30   | ~1500  | ~$0.07       |

## Provider prioritário e fallback

**Sempre usar DeepSeek como provider principal.** Fallback para Ollama local
(Qwen 2.5 14B) apenas se a API estiver indisponível:

```python
# Provider: deepseek (prioridade) ou ollama (fallback)
LLM_PROVIDER = "deepseek"
LLM_MODEL    = "deepseek-chat"
# Fallback Ollama local:
# LLM_PROVIDER = "ollama"
# LLM_MODEL    = "qwen2.5:14b"
# LLM_URL      = "http://localhost:11434/api/generate"
```

## Regras
- Chunk size padrão: **15 blocos** — nunca menos (perde contexto)
- temperature: 0.3 — mais determinístico para tradução
- Fallback em erro: manter bloco original **marcado** com `[FALTA TRADUZIR]` — nunca travar o pipeline
- **Nunca deixar `[FALTA TRADUZIR]` / `[QA-FALHA]` no SRT que vai pro TTS** — o OmniVoice lê em voz alta
- Verificar inglês residual no `_zh` antes de avançar (QA de idioma)
- Nomes próprios: manter exatamente como estão OU transliterar — mas sempre de forma consistente no vídeo inteiro
- Verificar nomes históricos/pessoais após tradução (comparar medium × large-v3 EN + contexto)

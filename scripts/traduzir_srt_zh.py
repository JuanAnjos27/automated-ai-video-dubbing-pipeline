#!/usr/bin/env python3
"""Traduz SRT → idioma alvo via DeepSeek API (default: chinês simplificado zh).
Chunks de 15 blocos com JSON, preserva contexto. Faz QA automatico nas
fronteiras dos chunks ao final da traducao.

Multi-idioma: o alvo é definido por --idioma (default zh). Pré-configurado
para chinês mandarim (estilo do projeto Juan_bilibili), mas qualquer idioma
pode ser usado — basta passar o código (ex.: pt-br, es, en, ja...) e,
opcionalmente, um --style-hint próprio.

Adaptado de traduzir_srt_deepseek.py (canal-dublagem-jiang).
"""

import argparse, json, os, re, signal, sys, time, urllib.request
from urllib.error import HTTPError, URLError

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_URL = "https://api.deepseek.com/chat/completions"

if not API_KEY:
    sys.exit("[ERRO] Variável de ambiente DEEPSEEK_API_KEY não definida. "
             "Configure-a (ex.: export DEEPSEEK_API_KEY=sk-... ) antes de rodar a tradução.")
MODEL = "deepseek-chat"
TEMPERATURE = 0.3
MAX_TOKENS = 4000
CHUNK_SIZE = 15
SAVE_EVERY = 5  # checkpoint a cada N chunks

# Idioma alvo (default zh). Mapa código → nome em inglês para o prompt.
TARGET_LANGS = {
    "zh":    "Simplified Chinese (Mandarin)",
    "pt-br": "Brazilian Portuguese",
    "pt":    "Portuguese",
    "en":    "English",
    "es":    "Spanish (Latin America)",
    "fr":    "French",
    "de":    "German",
    "ja":    "Japanese",
    "ko":    "Korean",
}
TARGET_LANG = "zh"
TARGET_LANG_NAME = TARGET_LANGS[TARGET_LANG]

STYLE_HINT = (
    "Academic lecture style. Maintain all proper nouns and names exactly as-is. "
    "Keep the informal classroom lecture tone. Use natural Simplified Chinese (Mandarin)."
)

_state = {"out_lines": [], "output_path": "", "translated": 0, "total": 0}

def _on_interrupt(signum, frame):
    if _state["out_lines"] and _state["output_path"]:
        with open(_state["output_path"], "w", encoding="utf-8") as f:
            f.write("\n".join(_state["out_lines"]))
        print(f"\n\n⏸️ Salvo: {_state['translated']}/{_state['total']} blocos em {_state['output_path']}")
    sys.exit(0)

signal.signal(signal.SIGINT, _on_interrupt)


def call_llm(prompt, max_tokens=MAX_TOKENS, timeout=180):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": f"You are a {TARGET_LANG_NAME} translator. Return ONLY a valid JSON array. No markdown, no explanations, no extra text."},
            {"role": "user", "content": prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                API_URL,
                data=data,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = json.loads(res.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
        except (HTTPError, URLError) as e:
            if attempt < 2:
                time.sleep((attempt + 1) * 10)
            else:
                raise RuntimeError(f"Falha na API: {e}")


def parse_srt(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    blocks = []
    pattern = re.compile(
        r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n([\s\S]*?)(?=\n\d+\s*\n|\Z)",
        re.MULTILINE
    )
    for m in pattern.finditer(content):
        blocks.append({
            "id": int(m.group(1)),
            "start": m.group(2),
            "end": m.group(3),
            "text": m.group(4).strip(),
        })
    return blocks


def build_chunk_prompt(chunk):
    items = [{"id": b["id"], "start": b["start"], "end": b["end"], "text": b["text"]} for b in chunk]
    payload_json = json.dumps(items, ensure_ascii=False, indent=2)

    return (
        f"Translate the 'text' field of each block to {TARGET_LANG_NAME}.\n"
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

    # Remove markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    # Try direct parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: extract JSON array from text
        start_idx = text.find("[")
        end_idx = text.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            try:
                data = json.loads(text[start_idx:end_idx + 1])
            except json.JSONDecodeError:
                raise RuntimeError(f"Não foi possível extrair JSON da resposta:\n{text[:500]}")
        else:
            raise RuntimeError(f"Resposta não contém JSON array:\n{text[:500]}")

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError(f"Resposta não é lista JSON:\n{text[:500]}")

    by_id = {b["id"]: b for b in original_chunk}
    result = []
    for item in data:
        if "id" not in item or "text" not in item:
            continue
        idx = int(item["id"])
        if idx not in by_id:
            continue
        result.append({
            "id": idx,
            "start": by_id[idx]["start"],
            "end": by_id[idx]["end"],
            "text": str(item["text"]).strip(),
        })
    return result


def translate_chunk(chunk):
    """Traduz um chunk com fallback progressivo (divide ao meio se falhar)."""

    def _try(items):
        prompt = build_chunk_prompt(items)
        raw = call_llm(prompt)
        return parse_json_response(raw, items)

    # Primeira tentativa
    try:
        result = _try(chunk)
    except Exception:
        if len(chunk) <= 1:
            raise
        mid = len(chunk) // 2
        left = translate_chunk(chunk[:mid])
        right = translate_chunk(chunk[mid:])
        return left + right

    # Verificar IDs faltantes
    by_id = {b["id"]: b for b in chunk}
    out_map = {b["id"]: b for b in result}
    missing = sorted(set(by_id.keys()) - set(out_map.keys()))

    if missing:
        retry_chunk = [by_id[idx] for idx in missing]
        try:
            retry_result = _try(retry_chunk)
            for b in retry_result:
                out_map[b["id"]] = b
        except Exception:
            pass

    # Fallback final: marcar IDs ainda ausentes
    final_missing = sorted(set(by_id.keys()) - set(out_map.keys()))
    if final_missing:
        for idx in final_missing:
            src = by_id[idx]
            out_map[idx] = {
                "id": idx,
                "start": src["start"],
                "end": src["end"],
                "text": f"[FALTA TRADUZIR] {src['text']}",
            }
        print(f"  ⚠️ IDs ausentes após retry: {final_missing}")

    return [out_map[idx] for idx in sorted(by_id.keys())]


def qa_boundary_check(input_path, output_path, chunk_size=CHUNK_SIZE):
    """Verifica a qualidade da traducao nas fronteiras dos chunks.
    Mostra 2 blocos antes e depois de cada corte para revisao de contexto."""
    src_blocks = parse_srt(input_path)
    zh_blocks = parse_srt(output_path)

    if not src_blocks or not zh_blocks:
        print("  ⚠️  QA pulado: SRT vazio ou nao encontrado.")
        return

    total = len(zh_blocks)
    boundary_ids = [chunk_size * i for i in range(1, (total // chunk_size) + 1)]
    # So mostra algumas fronteiras (inicio, meio, fim) se forem muitas
    if len(boundary_ids) > 6:
        boundary_ids = boundary_ids[:3] + boundary_ids[len(boundary_ids)//2:len(boundary_ids)//2+1] + boundary_ids[-3:]

    src_map = {b["id"]: b["text"] for b in src_blocks}
    zh_map = {b["id"]: b["text"] for b in zh_blocks}

    print("\n" + "=" * 60)
    print("  QA — Fronteiras de Chunk")
    print("=" * 60)

    issues = 0
    for bid in boundary_ids:
        next_id = bid + 1
        if bid not in zh_map or next_id not in zh_map:
            continue

        print(f"\n  ── #{bid} → #{next_id} ──")
        for idx in [bid - 1, bid, next_id, next_id + 1]:
            if idx in zh_map and idx in src_map:
                print(f"  #{idx}")
                print(f"  SRC: {src_map[idx][:130]}")
                print(f"  ZH:  {zh_map[idx][:130]}")
                print()

        prev_text = zh_map[bid].rstrip("。，；：！？.,;:!? ")
        next_text = zh_map[next_id].lstrip("。，；：！？.,;:!? ")
        # Se ambos sao muito curtos (<10 chars), pode ser bloco fragmentado
        if len(zh_map[bid]) < 10 or len(zh_map[next_id]) < 10:
            print(f"  ⚠️  Bloco curto na fronteira #{bid}→#{next_id}")
            issues += 1

    # Resumo
    print(f"\n  Fronteiras verificadas: {len([b for b in boundary_ids if b in zh_map])}")
    if issues:
        print(f"  ⚠️  {issues} potenciais problemas encontrados.")
    else:
        print(f"  ✅ Nenhum problema evidente nas fronteiras.")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Traduzir SRT para idioma alvo via DeepSeek API (chunk JSON, preserva contexto)")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--idioma", default="zh", help="Idioma alvo (default: zh). Ex.: zh, pt-br, es, en, ja...")
    parser.add_argument("--style-hint", default=None, help="Instrução de estilo customizada (opcional)")
    parser.add_argument("--inicio", type=int, default=1)
    parser.add_argument("--no-qa", action="store_true", help="Pula verificacao de fronteiras ao final")
    args = parser.parse_args()

    global TARGET_LANG, TARGET_LANG_NAME, STYLE_HINT
    TARGET_LANG = args.idioma
    TARGET_LANG_NAME = TARGET_LANGS.get(args.idioma, args.idioma)
    if args.style_hint:
        STYLE_HINT = args.style_hint

    _state["output_path"] = args.output

    blocks = parse_srt(args.input)
    blocks = [b for b in blocks if b["id"] >= args.inicio]
    total = len(blocks)

    # Agrupar em chunks
    chunks = [blocks[i:i + CHUNK_SIZE] for i in range(0, len(blocks), CHUNK_SIZE)]

    print(f"DeepSeek (deepseek-chat) | {total} blocos | {len(chunks)} chunks de até {CHUNK_SIZE}")
    print(f"Entrada: {args.input}")
    print(f"Saída:   {args.output}")
    print(f"Início: bloco #{args.inicio}")
    print("=" * 60)

    # Resume
    existing_ids = set()
    out_lines = []
    try:
        existing = parse_srt(args.output)
        existing_ids = {b["id"] for b in existing}
        if existing_ids:
            with open(args.output, encoding="utf-8") as f:
                out_lines = [l.rstrip() for l in f.readlines()]
            print(f"Retomando: {len(existing_ids)} blocos já traduzidos, pulando...")
    except Exception:
        pass

    _state["out_lines"] = out_lines
    _state["total"] = total

    translated = len(existing_ids)
    chunks_done = 0

    for ci, chunk in enumerate(chunks):
        # Pular chunks já completamente traduzidos
        if all(b["id"] in existing_ids for b in chunk):
            chunks_done += 1
            continue

        # Filtrar blocos pendentes
        pending = [b for b in chunk if b["id"] not in existing_ids]
        if not pending:
            chunks_done += 1
            continue

        first_id = pending[0]["id"]
        last_id = pending[-1]["id"]
        sys.stdout.write(f"\r[chunk {ci+1}/{len(chunks)}] #{first_id}–#{last_id} ({len(pending)} blocos)...")
        sys.stdout.flush()

        try:
            result = translate_chunk(pending)
            for b in result:
                out_lines.append(str(b["id"]))
                out_lines.append(f"{b['start']} --> {b['end']}")
                out_lines.append(b["text"])
                out_lines.append("")
                existing_ids.add(b["id"])
                translated += 1
            _state["translated"] = translated
            chunks_done += 1
            sys.stdout.write(f"\r[chunk {ci+1}/{len(chunks)}] #{first_id}–#{last_id} ✓ ({len(result)} blocos) {' ' * 30}\n")
            sys.stdout.flush()
        except Exception as e:
            # Fallback: manter texto original
            for b in pending:
                out_lines.append(str(b["id"]))
                out_lines.append(f"{b['start']} --> {b['end']}")
                out_lines.append(f"[FALTA TRADUZIR] {b['text']}")
                out_lines.append("")
                existing_ids.add(b["id"])
                translated += 1
            _state["translated"] = translated
            chunks_done += 1
            sys.stdout.write(f"\r[chunk {ci+1}/{len(chunks)}] #{first_id}–#{last_id} ✗ ERRO: {e}\n")
            sys.stdout.flush()

        # Checkpoint
        if chunks_done % SAVE_EVERY == 0:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write("\n".join(out_lines))
            print(f"  💾 checkpoint salvo ({translated}/{total} blocos)")

        time.sleep(0.3)

    # Salvar final
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))

    final = parse_srt(args.output)
    faltantes = sum(1 for b in final if "[FALTA TRADUZIR]" in b["text"])
    print("=" * 60)
    print(f"✅ {len(final)}/{total} blocos → {args.output}")
    if faltantes > 0:
        print(f"⚠️ {faltantes} blocos marcados [FALTA TRADUZIR]")

    # QA: verificar fronteiras de chunk
    if not args.no_qa and args.input and os.path.exists(args.input):
        qa_boundary_check(args.input, args.output, CHUNK_SIZE)

if __name__ == "__main__":
    main()

import json
import os
import time
import urllib.request
from typing import List, Tuple
from urllib.error import HTTPError, URLError

from .models import SRTBlock
from .ollama_client import generate


def _build_prompt(chunk: List[SRTBlock], target_lang: str, style_hint: str) -> str:
    return (
        "You translate subtitle blocks strictly.\n"
        f"Target language code: {target_lang}.\n"
        f"Style requirement: {style_hint}.\n"
        "Rules:\n"
        "1) Preserve id/start/end exactly.\n"
        "2) Translate only text.\n"
        "3) Return ONLY valid JSON array with fields: id,start,end,text.\n"
        "4) Do not add commentary.\n\n"
        f"Input JSON:\n{_chunk_payload(chunk)}"
    )


def _parse_translated_data(raw: str) -> List[dict]:
    text = raw.strip()

    if text.startswith("```"):
        # Remove fenced code blocks if present
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    def _try_parse(candidate: str):
        return json.loads(candidate)

    try:
        data = _try_parse(text)
    except json.JSONDecodeError:
        data = None

    if data is None:
        # Fallback: extract the largest JSON-looking array/object span.
        starts = [i for i, ch in enumerate(text) if ch in "[{"]
        ends = [i for i, ch in enumerate(text) if ch in "]}"]
        parsed = None
        for s in starts:
            for e in reversed(ends):
                if e <= s:
                    continue
                candidate = text[s : e + 1].strip()
                try:
                    parsed = _try_parse(candidate)
                    break
                except json.JSONDecodeError:
                    continue
            if parsed is not None:
                break
        if parsed is None:
            raise json.JSONDecodeError("Unable to parse JSON payload", text, 0)
        data = parsed

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError("Resposta de traducao nao e lista JSON")
    return data


def _translate_once(
    chunk: List[SRTBlock],
    model: str,
    ollama_url: str,
    target_lang: str,
    style_hint: str,
    request_timeout: int = 240,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
) -> List[SRTBlock]:
    prompt = _build_prompt(chunk=chunk, target_lang=target_lang, style_hint=style_hint)
    raw = generate(
        prompt=prompt,
        model=model,
        ollama_url=ollama_url,
        timeout=request_timeout,
        provider=llm_provider,
        api_key=llm_api_key,
    )

    try:
        data = _parse_translated_data(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chunk de traducao retornou JSON invalido: {exc}") from exc

    by_id = {b.index: b for b in chunk}
    out: List[SRTBlock] = []

    for item in data:
        if "id" not in item or "text" not in item:
            continue
        idx = int(item["id"])
        if idx not in by_id:
            continue
        src = by_id[idx]
        out.append(
            SRTBlock(
                index=idx,
                start=src.start,
                end=src.end,
                start_ms=src.start_ms,
                end_ms=src.end_ms,
                text=str(item["text"]).strip(),
            )
        )

    out.sort(key=lambda x: x.index)
    return out


def chunk_blocks(blocks: List[SRTBlock], chunk_size: int) -> List[List[SRTBlock]]:
    return [blocks[i : i + chunk_size] for i in range(0, len(blocks), chunk_size)]


def _chunk_payload(chunk: List[SRTBlock]) -> str:
    payload = []
    for b in chunk:
        payload.append(
            {
                "id": b.index,
                "start": b.start,
                "end": b.end,
                "text": b.text,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def translate_chunk(
    chunk: List[SRTBlock],
    model: str,
    ollama_url: str,
    target_lang: str,
    style_hint: str,
    request_timeout: int = 240,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
) -> List[SRTBlock]:
    def _translate_resilient(items: List[SRTBlock]) -> List[SRTBlock]:
        try:
            return _translate_once(
                chunk=items,
                model=model,
                ollama_url=ollama_url,
                target_lang=target_lang,
                style_hint=style_hint,
                request_timeout=request_timeout,
                llm_provider=llm_provider,
                llm_api_key=llm_api_key,
            )
        except Exception:
            # Fallback progressivo: divide chunk grande em partes menores.
            if len(items) <= 1:
                raise
            mid = len(items) // 2
            left = _translate_resilient(items[:mid])
            right = _translate_resilient(items[mid:])
            return left + right

    first_pass = _translate_resilient(chunk)

    by_id = {b.index: b for b in chunk}
    out_map = {b.index: b for b in first_pass}
    missing = sorted(set(by_id.keys()) - set(out_map.keys()))

    if missing:
        retry_chunk = [by_id[idx] for idx in missing]
        retry_pass = _translate_resilient(retry_chunk)
        for b in retry_pass:
            out_map[b.index] = b

    final_missing = sorted(set(by_id.keys()) - set(out_map.keys()))
    if final_missing:
        # Fallback de robustez: preserva texto original para IDs ausentes
        # em vez de abortar toda a pipeline em arquivos longos.
        for idx in final_missing:
            out_map[idx] = by_id[idx]
        print(
            "[TRAD][WARN] IDs ausentes apos traducao, usando texto original: "
            f"{final_missing}"
        )

    return [out_map[idx] for idx in sorted(by_id.keys())]


def retranslate_failed_blocks(
    blocks: List[SRTBlock],
    failed_ids: List[int],
    model: str,
    ollama_url: str,
    target_lang: str,
    style_hint: str,
    request_timeout: int = 240,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
) -> List[SRTBlock]:
    if not failed_ids:
        return blocks

    lookup = {b.index: b for b in blocks}
    to_fix = [lookup[i] for i in failed_ids if i in lookup]

    fixed = translate_chunk(
        chunk=to_fix,
        model=model,
        ollama_url=ollama_url,
        target_lang=target_lang,
        style_hint=style_hint,
        request_timeout=request_timeout,
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
    )

    fixed_map = {b.index: b for b in fixed}
    merged: List[SRTBlock] = []
    for b in blocks:
        merged.append(fixed_map.get(b.index, b))

    return merged


# ── Traducao rapida (deepseek-chat, checkpoint, resume) ─────────────────────

FAST_MODEL = "deepseek-chat"
FAST_TEMPERATURE = 0.3
FAST_CHUNK_SIZE = 15
FAST_SAVE_EVERY = 5

FAST_SYSTEM_PROMPT = (
    "You are a PT-BR translator. "
    "Return ONLY a valid JSON array. No markdown, no explanations, no extra text."
)


def _fast_call_api(prompt: str, api_key: str, api_url: str, timeout: int = 180) -> str:
    payload = {
        "model": FAST_MODEL,
        "messages": [
            {"role": "system", "content": FAST_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": FAST_TEMPERATURE,
        "max_tokens": 4000,
    }
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                api_url,
                data=data,
                headers={
                    "Authorization": f"Bearer {api_key}",
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
                raise RuntimeError(f"Falha na API DeepSeek: {e}")


def _fast_parse_response(raw: str) -> List[dict]:
    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start_idx = text.find("[")
        end_idx = text.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            data = json.loads(text[start_idx:end_idx + 1])
        else:
            raise RuntimeError(f"Resposta nao contem JSON array:\n{text[:500]}")

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise RuntimeError(f"Resposta nao e lista JSON:\n{text[:500]}")
    return data


def _fast_translate_chunk(
    chunk: List[SRTBlock],
    target_lang: str,
    style_hint: str,
    api_key: str,
    api_url: str,
    timeout: int = 180,
) -> List[SRTBlock]:
    """Traduz um chunk com fallback progressivo (divide ao meio se falhar)."""

    def _build_prompt(items: List[SRTBlock]) -> str:
        payload = [
            {"id": b.index, "start": b.start, "end": b.end, "text": b.text}
            for b in items
        ]
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            f"Translate the 'text' field of each block to {target_lang.upper()}.\n"
            f"Style: {style_hint}\n"
            "Rules:\n"
            "1) Preserve 'id', 'start', 'end' fields exactly as-is.\n"
            "2) Translate ONLY the 'text' field.\n"
            "3) Return ONLY a valid JSON array with the same blocks, same order.\n"
            "4) No markdown, no explanations, no extra text.\n\n"
            f"Input JSON:\n{payload_json}"
        )

    def _try(items: List[SRTBlock]) -> List[SRTBlock]:
        prompt = _build_prompt(items)
        raw = _fast_call_api(prompt, api_key, api_url, timeout)
        data = _fast_parse_response(raw)

        by_id = {b.index: b for b in items}
        result: List[SRTBlock] = []
        for item in data:
            if "id" not in item or "text" not in item:
                continue
            idx = int(item["id"])
            if idx not in by_id:
                continue
            src = by_id[idx]
            result.append(SRTBlock(
                index=idx,
                start=src.start,
                end=src.end,
                start_ms=src.start_ms,
                end_ms=src.end_ms,
                text=str(item["text"]).strip(),
            ))
        return result

    try:
        result = _try(chunk)
    except Exception:
        if len(chunk) <= 1:
            raise
        mid = len(chunk) // 2
        left = _fast_translate_chunk(chunk[:mid], target_lang, style_hint, api_key, api_url, timeout)
        right = _fast_translate_chunk(chunk[mid:], target_lang, style_hint, api_key, api_url, timeout)
        return left + right

    # Verificar IDs faltantes
    by_id = {b.index: b for b in chunk}
    out_map = {b.index: b for b in result}
    missing = sorted(set(by_id.keys()) - set(out_map.keys()))

    if missing:
        retry_chunk = [by_id[idx] for idx in missing]
        try:
            retry_result = _try(retry_chunk)
            for b in retry_result:
                out_map[b.index] = b
        except Exception:
            pass

    final_missing = sorted(set(by_id.keys()) - set(out_map.keys()))
    if final_missing:
        for idx in final_missing:
            src = by_id[idx]
            out_map[idx] = SRTBlock(
                index=idx,
                start=src.start,
                end=src.end,
                start_ms=src.start_ms,
                end_ms=src.end_ms,
                text=f"[FALTA TRADUZIR] {src.text}",
            )
        print(f"  [FAST-TRAD] ⚠️ IDs ausentes apos retry: {final_missing}")

    return [out_map[idx] for idx in sorted(by_id.keys())]


def translate_all_fast(
    blocks: List[SRTBlock],
    output_srt_path: str,
    target_lang: str,
    style_hint: str,
    llm_api_key: str,
    api_url: str = "https://api.deepseek.com/chat/completions",
    chunk_size: int = FAST_CHUNK_SIZE,
    request_timeout: int = 180,
    resume: bool = True,
    verbose: bool = True,
) -> List[SRTBlock]:
    """Traducao rapida via deepseek-chat com checkpoints incrementais.

    Chama a API diretamente (sem passar pelo ollama_client) para usar:
    - modelo deepseek-chat (mais rapido/barato que v4-pro)
    - system prompt que reduz alucinacoes
    - temperatura 0.3 (mais deterministico)
    - 3 retries em erros HTTP
    - checkpoint a cada 5 chunks no arquivo de saida
    - resume automatico se o arquivo de saida ja existir

    Args:
        blocks: Lista de SRTBlock a traduzir.
        output_srt_path: Caminho do SRT de saida (usado p/ checkpoint).
        target_lang: Idioma alvo (ex: "pt-br").
        style_hint: Guia de estilo para o tradutor.
        llm_api_key: Chave da API DeepSeek.
        api_url: URL da API (default DeepSeek).
        chunk_size: Blocos por chunk.
        request_timeout: Timeout por chamada.
        resume: Se True, retoma traducao de arquivo de saida existente.
        verbose: Se True, imprime progresso.

    Returns:
        Lista de SRTBlock traduzidos.
    """
    from .srt_utils import parse_srt as _parse_srt

    total = len(blocks)
    chunks = [blocks[i:i + chunk_size] for i in range(0, total, chunk_size)]

    # Resume
    existing_ids: set = set()
    if resume and os.path.exists(output_srt_path):
        try:
            existing = _parse_srt(output_srt_path)
            existing_ids = {b.index for b in existing}
            if existing_ids and verbose:
                print(f"[FAST-TRAD] Retomando: {len(existing_ids)} blocos ja traduzidos")
        except Exception:
            pass

    translated_map: dict[int, SRTBlock] = {}
    for b in blocks:
        if b.index in existing_ids:
            # Buscar no arquivo existente
            pass

    # Carregar traducoes existentes do arquivo
    if existing_ids:
        try:
            existing = _parse_srt(output_srt_path)
            for b in existing:
                translated_map[b.index] = b
        except Exception:
            pass

    chunks_done = 0
    total_pending = total - len(existing_ids)

    if verbose and total_pending > 0:
        print(f"[FAST-TRAD] deepseek-chat | {total} blocos | {len(chunks)} chunks de ate {chunk_size}")
        print(f"[FAST-TRAD] Pendentes: {total_pending} | Idioma: {target_lang}")

    for ci, chunk in enumerate(chunks):
        # Pular chunks ja completamente traduzidos
        if all(b.index in existing_ids for b in chunk):
            for b in chunk:
                if b.index in translated_map:
                    pass  # ja temos
            chunks_done += 1
            continue

        pending = [b for b in chunk if b.index not in existing_ids]
        if not pending:
            chunks_done += 1
            continue

        first_id = pending[0].index
        last_id = pending[-1].index

        if verbose:
            print(f"[FAST-TRAD] Chunk {ci+1}/{len(chunks)} (#{first_id}-#{last_id}, {len(pending)} blocos)...", end=" ", flush=True)

        try:
            result = _fast_translate_chunk(
                pending, target_lang, style_hint,
                llm_api_key, api_url, request_timeout,
            )
            for b in result:
                translated_map[b.index] = b
                existing_ids.add(b.index)
            chunks_done += 1
            if verbose:
                print(f"OK ({len(result)} blocos)")
        except Exception as e:
            # Fallback: preservar texto original
            for b in pending:
                translated_map[b.index] = SRTBlock(
                    index=b.index, start=b.start, end=b.end,
                    start_ms=b.start_ms, end_ms=b.end_ms,
                    text=f"[FALTA TRADUZIR] {b.text}",
                )
                existing_ids.add(b.index)
            chunks_done += 1
            if verbose:
                print(f"ERRO: {e}")

        # Checkpoint a cada FAST_SAVE_EVERY chunks
        if chunks_done % FAST_SAVE_EVERY == 0:
            _save_checkpoint(translated_map, output_srt_path, total, verbose)

        time.sleep(0.3)

    # Salvar final
    _save_checkpoint(translated_map, output_srt_path, total, verbose)

    result = [translated_map[idx] for idx in sorted(translated_map.keys())]

    faltantes = sum(1 for b in result if "[FALTA TRADUZIR]" in b.text)
    if faltantes > 0 and verbose:
        print(f"[FAST-TRAD] ⚠️ {faltantes} blocos marcados [FALTA TRADUZIR]")

    return result


def _save_checkpoint(
    translated_map: dict,
    path: str,
    total: int,
    verbose: bool,
) -> None:
    """Salva SRT parcial no disco."""
    from .srt_utils import save_srt as _save_srt

    ordered = [translated_map[idx] for idx in sorted(translated_map.keys())]
    _save_srt(path, ordered)
    if verbose:
        print(f"  [FAST-TRAD] checkpoint: {len(ordered)}/{total} blocos salvos")


def fix_failed_blocks(
    blocks: List[SRTBlock],
    failed_ids: List[int],
    en_blocks: List[SRTBlock],
    target_lang: str,
    style_hint: str,
    llm_api_key: str,
    api_url: str = "https://api.deepseek.com/chat/completions",
    model: str = "deepseek-v4-pro",
    max_retries: int = 3,
    verbose: bool = True,
) -> Tuple[List[SRTBlock], List[int]]:
    """Corrige blocos reprovados no QA re-traduzindo com EN original + vizinhos.

    Casa blocos PT ↔ EN por timestamp (start_ms/end_ms), nunca por indice.

    Returns:
        (blocos corrigidos, ids que ainda falharam apos todas as tentativas)
    """
    if not failed_ids:
        return blocks, []

    # Ordenar EN por timestamp
    en_sorted = sorted(en_blocks, key=lambda b: b.start_ms)
    ordered = sorted(blocks, key=lambda b: b.index)
    still_failed: List[int] = []
    fixed_count = 0

    def _find_en_block(pt_block: SRTBlock) -> SRTBlock | None:
        """Encontra o bloco EN que mais sobrepoe com o PT no tempo."""
        best, best_overlap = None, 0
        for en in en_sorted:
            overlap = min(pt_block.end_ms, en.end_ms) - max(pt_block.start_ms, en.start_ms)
            if overlap > best_overlap:
                best_overlap = overlap
                best = en
        return best if best_overlap > 0 else None

    def _nearby_en(bid: int, radius: int = 2) -> List[SRTBlock]:
        """Pega EN blocks que sobrepoem no tempo com os vizinhos do bloco PT."""
        pt_block = next((b for b in ordered if b.index == bid), None)
        if not pt_block:
            return []
        pos = next((i for i, b in enumerate(ordered) if b.index == bid), -1)
        nearby = []
        for i in range(max(0, pos - radius), min(len(ordered), pos + radius + 1)):
            if i == pos:
                continue
            en_match = _find_en_block(ordered[i])
            if en_match and en_match not in nearby:
                nearby.append(en_match)
        return nearby

    for bid in failed_ids:
        pt_block = next((b for b in ordered if b.index == bid), None)
        if not pt_block:
            still_failed.append(bid)
            continue

        # Encontrar EN original correspondente por timestamp
        en_source = _find_en_block(pt_block)
        en_text = en_source.text if en_source else "(texto original nao encontrado)"

        # Coletar contexto EN dos vizinhos
        en_neighbors = _nearby_en(bid, radius=2)
        context_json = json.dumps(
            [{"id": nb.index, "en": nb.text} for nb in en_neighbors],
            ensure_ascii=False, indent=2,
        )

        prompt = (
            f"Re-translate this subtitle block to {target_lang.upper()}.\n"
            f"Style: {style_hint}\n\n"
            f"EN source (what the speaker actually said):\n"
            f"\"{en_text}\"\n\n"
            f"Current PT translation (WRONG — must be replaced):\n"
            f"\"{pt_block.text}\"\n\n"
            f"Nearby EN blocks for context:\n"
            f"{context_json}\n\n"
            "Rules:\n"
            "1. Return ONLY the corrected PT-BR translation.\n"
            "2. No JSON, no markdown, no explanations.\n"
            "3. Translate the EN source faithfully, using context for coherence.\n"
            "4. Preserve proper names exactly as in the EN source."
        )

        fixed = False
        for attempt in range(1, max_retries + 1):
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": f"You are a {target_lang.upper()} translator. Return ONLY the corrected translation text, nothing else."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                }
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    api_url,
                    data=data,
                    headers={
                        "Authorization": f"Bearer {llm_api_key}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=120) as res:
                    body = json.loads(res.read().decode("utf-8"))
                new_text = body["choices"][0]["message"]["content"].strip()

                if new_text.startswith('"') and new_text.endswith('"'):
                    new_text = new_text[1:-1]
                new_text = new_text.replace("```", "").strip()

                if new_text and len(new_text) > 3:
                    pt_block.text = new_text
                    fixed = True
                    fixed_count += 1
                    break
            except Exception as e:
                if verbose and attempt == max_retries:
                    print(f"  [FIX] Bloco #{bid}: falha na tentativa {attempt}/{max_retries}: {e}")
                time.sleep(1)

        if not fixed:
            pt_block.text = f"[QA-FALHA] {pt_block.text}"
            still_failed.append(bid)
            if verbose:
                print(f"  [FIX] Bloco #{bid}: NAO RESOLVIDO apos {max_retries} tentativas — marcado [QA-FALHA]")

    if verbose:
        print(f"[FIX] Corrigidos: {fixed_count}/{len(failed_ids)} | Pendentes: {len(still_failed)}")

    return blocks, still_failed

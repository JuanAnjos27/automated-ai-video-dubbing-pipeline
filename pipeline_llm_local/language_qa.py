from typing import Dict, List, Tuple

from .models import SRTBlock
from .ollama_client import generate

_STOPWORDS: Dict[str, set[str]] = {
    "pt": {"de", "que", "não", "para", "com", "uma", "em", "por", "como", "mais"},
    "es": {"de", "que", "no", "para", "con", "una", "en", "por", "como", "mas"},
    "en": {"the", "and", "to", "of", "in", "for", "with", "that", "is", "on"},
    "pl": {"i", "w", "na", "z", "do", "to", "nie", "jest", "się", "że"},
}


def _tokenize(text: str) -> List[str]:
    cleaned = []
    token = []
    for ch in text.lower():
        if ch.isalpha() or ch in "àáâãéêíóôõúüçñłżźćąęś":
            token.append(ch)
        elif token:
            cleaned.append("".join(token))
            token = []
    if token:
        cleaned.append("".join(token))
    return cleaned


def heuristic_language_score(text: str, target_lang: str) -> float:
    words = _tokenize(text)
    if not words:
        return 0.0

    bag = _STOPWORDS.get(target_lang, set())
    if not bag:
        return 0.0

    hits = sum(1 for w in words if w in bag)
    return hits / max(1, len(words))


def llm_language_check(
    text: str,
    target_lang: str,
    model: str,
    ollama_url: str,
    request_timeout: int = 90,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
) -> bool:
    prompt = (
        "You are a strict language validator for subtitles.\n"
        f"Target language code: {target_lang}.\n"
        "Task: decide if the text is predominantly in the target language.\n"
        "Return ONLY one token: OK or NOT_OK.\n"
        f"Text: {text}"
    )
    result = generate(
        prompt=prompt,
        model=model,
        ollama_url=ollama_url,
        timeout=request_timeout,
        provider=llm_provider,
        api_key=llm_api_key,
    )
    if result is None:
        return False
    return result.strip().upper().startswith("OK")


def full_document_language_qa(
    blocks: List[SRTBlock],
    target_lang: str,
    model: str,
    ollama_url: str,
    heuristic_threshold: float = 0.04,
    progress_every: int = 100,
    verbose: bool = True,
    request_timeout: int = 90,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
) -> Tuple[int, int, List[int]]:
    passed = 0
    failed = 0
    failed_ids: List[int] = []
    total = len(blocks)

    if verbose:
        print(f"[QA] Iniciando validacao de idioma em {total} blocos...")

    for i, b in enumerate(blocks, start=1):
        score = heuristic_language_score(b.text, target_lang)
        # Heuristica aprova os casos obvios; casos duvidosos passam por LLM.
        if score >= heuristic_threshold:
            passed += 1
        else:
            is_ok = llm_language_check(
                text=b.text,
                target_lang=target_lang,
                model=model,
                ollama_url=ollama_url,
                request_timeout=request_timeout,
                llm_provider=llm_provider,
                llm_api_key=llm_api_key,
            )
            if is_ok:
                passed += 1
            else:
                failed += 1
                failed_ids.append(b.index)

        if verbose and (i % max(1, progress_every) == 0 or i == total):
            print(
                f"[QA] {i}/{total} blocos | aprovados={passed} | reprovados={failed}"
            )

    if verbose:
        print(f"[QA] Finalizado: aprovados={passed}, reprovados={failed}")

    return passed, failed, failed_ids


def auto_verify_failed_blocks(
    blocks: List[SRTBlock],
    failed_ids: List[int],
    en_source_blocks: List[SRTBlock],
    target_lang: str,
    model: str,
    ollama_url: str,
    request_timeout: int = 120,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
    verbose: bool = True,
) -> Tuple[List[SRTBlock], List[int], int]:
    """Auto-verifica blocos [QA-FALHA] comparando EN original com PT traduzido.

    Para cada bloco reprovado na QA:
      - Busca o EN original correspondente (por timestamp)
      - Envia EN + PT + contexto pro LLM perguntando se a traducao esta correta
      - Se LLM disser OK → era falso positivo, remove tag [QA-FALHA]
      - Se LLM disser NOT_OK → mantem a tag (realmente precisa de correcao)

    Args:
        blocks: Lista de blocos PT (alguns com [QA-FALHA]).
        failed_ids: IDs dos blocos reprovados na QA.
        en_source_blocks: Blocos EN originais (medium).
        target_lang: Idioma alvo (ex: pt-br).
        model: Modelo LLM a usar.
        ollama_url: URL da API.
        request_timeout: Timeout por chamada.
        llm_provider: Backend ("deepseek" ou "ollama").
        llm_api_key: Chave da API.

    Returns:
        (blocos atualizados, ids ainda falhos, falsos_positivos_resolvidos)
    """
    if not failed_ids:
        return blocks, [], 0

    # Ordenar EN por timestamp para busca
    en_sorted = sorted(en_source_blocks, key=lambda b: b.start_ms)
    ordered = sorted(blocks, key=lambda b: b.index)
    by_id = {b.index: b for b in ordered}

    resolved = 0
    still_failed: List[int] = []

    if verbose:
        print(f"\n[QA-VERIFY] Auto-verificando {len(failed_ids)} blocos [QA-FALHA]...")
        print(f"[QA-VERIFY] Provider: {llm_provider} | Model: {model}")

    for bid in failed_ids:
        pt_block = by_id.get(bid)
        if not pt_block:
            still_failed.append(bid)
            continue

        # Encontrar EN original por sobreposicao de timestamp
        best, best_overlap = None, 0
        for en in en_sorted:
            overlap = min(pt_block.end_ms, en.end_ms) - max(pt_block.start_ms, en.start_ms)
            if overlap > best_overlap:
                best_overlap = overlap
                best = en

        en_text = best.text if best and best_overlap > 0 else "(original indisponivel)"

        # Coletar vizinhos EN para contexto
        context_parts = []
        pos = next((i for i, b in enumerate(ordered) if b.index == bid), -1)
        if pos >= 0:
            for offset in [-1, 1]:
                if 0 <= pos + offset < len(ordered):
                    nb = ordered[pos + offset]
                    nb_en_match = None
                    nb_best_overlap = 0
                    for en in en_sorted:
                        overlap = min(nb.end_ms, en.end_ms) - max(nb.start_ms, en.start_ms)
                        if overlap > nb_best_overlap:
                            nb_best_overlap = overlap
                            nb_en_match = en
                    if nb_en_match:
                        side_label = "antes" if offset == -1 else "depois"
                        context_parts.append(
                            f"Bloco {side_label} (#{nb.index}):\n"
                            f"  EN: \"{nb_en_match.text}\"\n"
                            f"  PT: \"{nb.text}\""
                        )

        context_str = "\n".join(context_parts) if context_parts else "(sem contexto)"

        prompt = (
            f"You are a bilingual {target_lang.upper()}-EN translation quality verifier.\n"
            "Task: check if the PT translation faithfully conveys the EN meaning.\n"
            "Consider context from neighboring blocks for coherence.\n\n"
            f"EN source text:\n\"{en_text}\"\n\n"
            f"PT translation:\n\"{pt_block.text.replace('[QA-FALHA] ', '').replace('[QA-FALHA]', '')}\"\n\n"
            f"Context (neighbor blocks with their translations):\n{context_str}\n\n"
            "Evaluation criteria:\n"
            "1. Is the meaning preserved? (not word-for-word, but faithful)\n"
            "2. Is the PT-BR natural and grammatically correct?\n"
            "3. Are proper names preserved correctly?\n"
            "4. Does it flow naturally with the neighboring blocks?\n\n"
            "If the translation is GOOD (meaning preserved, natural PT-BR): reply OK\n"
            "If the translation is BAD (meaning distorted, not PT-BR, or nonsensical): reply NOT_OK\n"
            "Reply ONLY with OK or NOT_OK."
        )

        is_ok = False
        try:
            result = generate(
                prompt=prompt,
                model=model,
                ollama_url=ollama_url,
                timeout=request_timeout,
                provider=llm_provider,
                api_key=llm_api_key,
            )
            is_ok = result is not None and result.strip().upper().startswith("OK")
        except Exception as e:
            if verbose:
                print(f"  [QA-VERIFY] #{bid}: erro na verificacao — {e}")
            # Em caso de erro, assumir falso positivo (nao travar o pipeline)
            is_ok = True

        if is_ok:
            # Falso positivo — remover tag
            pt_block.text = pt_block.text.replace("[QA-FALHA] ", "").replace("[QA-FALHA]", "")
            resolved += 1
            if verbose:
                print(f"  [QA-VERIFY] #{bid}: ✅ FALSO POSITIVO — tag removida")
        else:
            still_failed.append(bid)
            if verbose:
                print(f"  [QA-VERIFY] #{bid}: ❌ CONFIRMADO — precisa de correcao")

    if verbose:
        print(f"[QA-VERIFY] Resolvidos: {resolved}/{len(failed_ids)} | Pendentes: {len(still_failed)}")

    return blocks, still_failed, resolved

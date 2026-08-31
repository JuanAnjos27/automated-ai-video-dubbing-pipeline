import json
import re
from typing import Dict, List, Tuple

from .models import SRTBlock
from .ollama_client import generate


def _build_pairs(base_blocks: List[SRTBlock], compare_blocks: List[SRTBlock], limit: int = 120) -> List[Dict[str, str]]:
    """Casa blocos medium vs large-v3 por sobreposicao de tempo, nao por indice."""
    pairs: List[Dict[str, str]] = []

    for b in base_blocks:
        # Encontrar blocos do large-v3 que sobrepoem no tempo
        overlapping = [
            c for c in compare_blocks
            if c.start_ms < b.end_ms and c.end_ms > b.start_ms
        ]
        if overlapping:
            # Pegar o que tem maior sobreposicao
            best = max(overlapping, key=lambda c: (
                min(c.end_ms, b.end_ms) - max(c.start_ms, b.start_ms)
            ))
            if b.text.strip() != best.text.strip():
                pairs.append({"index": b.index, "base": b.text, "compare": best.text})
                if len(pairs) >= limit:
                    break

    return pairs


def suggest_corrections_with_llm(
    base_blocks: List[SRTBlock],
    compare_blocks: List[SRTBlock],
    model: str,
    ollama_url: str,
    limit_pairs: int = 120,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
) -> List[Dict[str, str]]:
    pairs = _build_pairs(base_blocks, compare_blocks, limit=limit_pairs)
    if not pairs:
        return []

    prompt = (
        "You are reviewing two subtitle transcriptions of the same content.\n"
        "Goal: identify likely spelling/name mistakes in BASE and suggest replacements using COMPARE context.\n"
        "Return ONLY valid JSON array of objects with keys: wrong,right,reason.\n"
        "Rules:\n"
        "1) Only include high-confidence lexical corrections (names/terms), no paraphrasing.\n"
        "2) wrong and right must be short phrases (1-3 words).\n"
        "3) Keep original language.\n"
        "4) Max 20 suggestions.\n\n"
        f"Pairs JSON:\n{json.dumps(pairs, ensure_ascii=False)}"
    )

    raw = generate(
        prompt=prompt,
        model=model,
        ollama_url=ollama_url,
        timeout=180,
        provider=llm_provider,
        api_key=llm_api_key,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    out: List[Dict[str, str]] = []
    for item in data:
        wrong = str(item.get("wrong", "")).strip()
        right = str(item.get("right", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not wrong or not right or wrong.lower() == right.lower():
            continue
        out.append({"wrong": wrong, "right": right, "reason": reason})

    return out[:20]


def apply_corrections(
    blocks: List[SRTBlock],
    corrections: List[Dict[str, str]],
    min_occurrences: int = 2,
) -> Tuple[List[SRTBlock], Dict[str, int]]:
    if not corrections:
        return blocks, {}

    corrected = [
        SRTBlock(
            index=b.index,
            start=b.start,
            end=b.end,
            start_ms=b.start_ms,
            end_ms=b.end_ms,
            text=b.text,
            meta=dict(b.meta),
        )
        for b in blocks
    ]

    replacement_counts: Dict[str, int] = {}

    for corr in corrections:
        wrong = corr["wrong"]
        right = corr["right"]
        pattern = re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE)

        possible = sum(len(pattern.findall(b.text)) for b in corrected)
        if possible < min_occurrences:
            continue

        total = 0
        for b in corrected:
            new_text, n = pattern.subn(right, b.text)
            if n:
                b.text = new_text
                total += n

        if total:
            replacement_counts[f"{wrong} -> {right}"] = total

    return corrected, replacement_counts

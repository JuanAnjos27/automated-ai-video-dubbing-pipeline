import difflib
import re
from typing import Dict, List, Tuple

from .models import SRTBlock


def _normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def detect_repetition_runs(blocks: List[SRTBlock], min_run: int = 3) -> List[Dict[str, object]]:
    issues: List[Dict[str, object]] = []
    if not blocks:
        return issues

    ordered = sorted(blocks, key=lambda b: b.index)
    run_start = 0

    for i in range(1, len(ordered) + 1):
        changed = i == len(ordered) or _normalize_text(ordered[i].text) != _normalize_text(ordered[i - 1].text)
        if not changed:
            continue

        run_len = i - run_start
        if run_len >= min_run:
            issues.append(
                {
                    "start_index": ordered[run_start].index,
                    "end_index": ordered[i - 1].index,
                    "run_length": run_len,
                    "text": ordered[run_start].text,
                }
            )
        run_start = i

    return issues


def _window_text(blocks: List[SRTBlock], start_ms: int, end_ms: int) -> str:
    parts = [b.text for b in blocks if b.start_ms >= start_ms and b.start_ms < end_ms]
    return " ".join(parts)


def compare_by_windows(
    base_blocks: List[SRTBlock],
    compare_blocks: List[SRTBlock],
    window_ms: int = 180000,
) -> List[Dict[str, object]]:
    if not base_blocks or not compare_blocks:
        return []

    max_ms = max(base_blocks[-1].end_ms, compare_blocks[-1].end_ms)
    out: List[Dict[str, object]] = []

    for start in range(0, max_ms + 1, window_ms):
        end = start + window_ms
        base_text = _window_text(base_blocks, start, end)
        cmp_text = _window_text(compare_blocks, start, end)
        if not base_text and not cmp_text:
            continue

        ratio = difflib.SequenceMatcher(None, base_text, cmp_text).ratio()
        out.append(
            {
                "start_ms": start,
                "end_ms": end,
                "similarity": round(ratio, 4),
                "divergence": round(1.0 - ratio, 4),
            }
        )

    return out


def detect_coverage_gaps(
    base_blocks: List[SRTBlock],
    compare_blocks: List[SRTBlock],
    tolerance_ms: int = 200,
    min_duration_s: float = 2.0,
) -> List[Dict[str, object]]:
    """
    Encontra intervalos onde o compare (large-v3) transcreveu mas o
    base (medium) nao — ou seja, conteudo que o medium perdeu.

    tolerance_ms  = folga nas bordas dos blocos (200ms)
    min_duration_s = ignora gaps menores que isso (ruido)
    """
    # Filtra blocos corrompidos do compare (end <= start)
    cmp_validos = [b for b in compare_blocks if b.end_ms > b.start_ms]
    corrupted = len(compare_blocks) - len(cmp_validos)

    # Blocos do compare que nao tem sobreposicao com nenhum bloco base
    sem_cobertura = []
    for cb in cmp_validos:
        coberto = any(
            bb.start_ms < cb.end_ms + tolerance_ms
            and bb.end_ms > cb.start_ms - tolerance_ms
            for bb in base_blocks
        )
        if not coberto:
            sem_cobertura.append(cb)

    if not sem_cobertura:
        return []

    # Agrupar blocos consecutivos (gap <= 2s entre eles = mesmo intervalo)
    gaps = []
    grupo = [sem_cobertura[0]]

    for i in range(1, len(sem_cobertura)):
        intervalo = sem_cobertura[i].start_ms - sem_cobertura[i - 1].end_ms
        if intervalo <= 2000:
            grupo.append(sem_cobertura[i])
        else:
            gap = _build_gap(grupo)
            if gap["duration_s"] >= min_duration_s:
                gaps.append(gap)
            grupo = [sem_cobertura[i]]

    gap = _build_gap(grupo)
    if gap["duration_s"] >= min_duration_s:
        gaps.append(gap)

    for g in gaps:
        g["corrupted_compare_blocks_skipped"] = corrupted

    return gaps


def _build_gap(blocks: List[SRTBlock]) -> Dict[str, object]:
    texto = " ".join(b.text for b in blocks)
    return {
        "start": blocks[0].start,
        "end": blocks[-1].end,
        "start_ms": blocks[0].start_ms,
        "end_ms": blocks[-1].end_ms,
        "duration_s": round((blocks[-1].end_ms - blocks[0].start_ms) / 1000, 1),
        "compare_block_count": len(blocks),
        "compare_block_ids": [b.index for b in blocks],
        "text_en": texto,
    }


def build_diagnostic_summary(base_blocks: List[SRTBlock], compare_blocks: List[SRTBlock]) -> Dict[str, object]:
    base_rep = detect_repetition_runs(base_blocks)
    cmp_rep = detect_repetition_runs(compare_blocks)
    windows = compare_by_windows(base_blocks, compare_blocks)
    gaps = detect_coverage_gaps(base_blocks, compare_blocks)

    severe_windows = [w for w in windows if w["divergence"] >= 0.65]

    return {
        "base_repetition_issues": base_rep,
        "compare_repetition_issues": cmp_rep,
        "window_divergence": windows,
        "severe_windows": severe_windows,
        "base_repetition_count": len(base_rep),
        "compare_repetition_count": len(cmp_rep),
        "severe_windows_count": len(severe_windows),
        "coverage_gaps": gaps,
        "coverage_gaps_count": len(gaps),
        "coverage_gaps_total_s": round(sum(g["duration_s"] for g in gaps), 1),
    }


HALLUCINATION_PHRASES = [
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "see you next",
    "don't forget to",
    "subscribe to",
    "thanks for watching",
    "[music]",
    "[applause]",
    "[inaudible]",
    "♪",
    "www.",
    ".com",
    "patreon.com",
    "subscribestar",
]


def generate_corrected_en_srt(
    medium_blocks: List[SRTBlock],
    large_v3_blocks: List[SRTBlock],
    corrections_llm_model: str = "deepseek-chat",
    corrections_llm_url: str = "https://api.deepseek.com/chat/completions",
    corrections_api_key: str | None = None,
    verbose: bool = True,
) -> Tuple[List[SRTBlock], dict]:
    """Gera medium EN corrigido usando large-v3 como referencia.

    Correcoes aplicadas:
    1. Gaps de cobertura — insere blocos do large-v3 que o medium perdeu
    2. Repeticoes/alucinacoes — remove blocos repetidos 3+ vezes
    3. Correcoes lexicais — usa LLM pra detectar erros de nome/termo
    4. Frases de alucinacao — remove thank you for watching, subscribe, etc.

    Returns:
        (blocos corrigidos, resumo das correcoes)
    """
    from copy import deepcopy

    from .models import SRTBlock as _SRTBlock

    corrected = [
        _SRTBlock(
            index=b.index, start=b.start, end=b.end,
            start_ms=b.start_ms, end_ms=b.end_ms,
            text=b.text, meta=dict(b.meta),
        )
        for b in medium_blocks
    ]

    summary = {
        "gaps_inserted": 0,
        "gaps_inserted_ids": [],
        "repetitions_removed": 0,
        "repetitions_removed_ids": [],
        "hallucinations_removed": 0,
        "hallucinations_removed_ids": [],
        "lexical_corrections": 0,
        "total_before": len(medium_blocks),
        "total_after": len(medium_blocks),
    }

    # ── 1. Inserir gaps de cobertura (large-v3 tem conteudo que medium perdeu) ──
    gaps = detect_coverage_gaps(medium_blocks, large_v3_blocks)
    if gaps:
        # Coletar blocos do large-v3 que estao nos gaps
        gap_ids: set[int] = set()
        for g in gaps:
            for bid in g.get("compare_block_ids", []):
                gap_ids.add(int(bid))

        cmp_map = {b.index: b for b in large_v3_blocks}
        inserted = []
        for bid in sorted(gap_ids):
            if bid in cmp_map:
                src = cmp_map[bid]
                inserted.append(_SRTBlock(
                    index=-1,  # placeholder, renumera depois
                    start=src.start, end=src.end,
                    start_ms=src.start_ms, end_ms=src.end_ms,
                    text=src.text,
                ))

        if inserted:
            corrected.extend(inserted)
            corrected.sort(key=lambda b: b.start_ms)
            # Renumerar
            for i, b in enumerate(corrected, 1):
                b.index = i
            summary["gaps_inserted"] = len(inserted)
            summary["gaps_inserted_ids"] = sorted(gap_ids)
            if verbose:
                print(f"[DIAG] {len(inserted)} blocos inseridos de gaps de cobertura (large-v3): {sorted(gap_ids)[:20]}{'...' if len(gap_ids) > 20 else ''}")

    # ── 2. Remover repeticoes/alucinacoes ──
    rep_runs = detect_repetition_runs(corrected, min_run=3)
    if rep_runs:
        remove_ids: set[int] = set()
        for run in rep_runs:
            run_ids = list(range(int(run["start_index"]), int(run["end_index"]) + 1))
            # Mantem o primeiro, remove o resto da sequencia repetida
            for rid in run_ids[1:]:
                remove_ids.add(rid)

        corrected = [b for b in corrected if b.index not in remove_ids]
        summary["repetitions_removed"] = len(remove_ids)
        summary["repetitions_removed_ids"] = sorted(remove_ids)
        if verbose:
            print(f"[DIAG] {len(remove_ids)} blocos removidos por repeticao: {sorted(remove_ids)[:20]}{'...' if len(remove_ids) > 20 else ''}")

    # ── 3. Remover frases de alucinacao conhecidas ──
    hall_removed = 0
    hall_removed_ids: List[int] = []
    filtered = []
    for b in corrected:
        text_lower = b.text.lower()
        is_hall = any(h in text_lower for h in HALLUCINATION_PHRASES)
        if is_hall:
            hall_removed += 1
            hall_removed_ids.append(b.index)
        else:
            filtered.append(b)

    if hall_removed:
        hall_ids_sorted = sorted(hall_removed_ids)
        corrected = filtered
        summary["hallucinations_removed"] = hall_removed
        summary["hallucinations_removed_ids"] = hall_ids_sorted
        if verbose:
            print(f"[DIAG] {hall_removed} blocos removidos por frases de alucinacao: {hall_ids_sorted[:20]}{'...' if len(hall_ids_sorted) > 20 else ''}")

    # Renumerar apos remocoes
    for i, b in enumerate(corrected, 1):
        b.index = i

    # ── 3.5. Preencher nomes proprios ausentes (medium perdeu foneticamente) ──
    # Compara cada bloco medium com large-v3 por timestamp.
    # Se large-v3 tem nomes proprios (capitalizados) que o medium nao tem
    # e os textos sao similares (>40%), usa o texto do large-v3.
    proper_noun_fixes = 0
    for cb in corrected:
        best = None
        best_overlap = 0
        for lb in large_v3_blocks:
            overlap = min(cb.end_ms, lb.end_ms) - max(cb.start_ms, lb.start_ms)
            if overlap > best_overlap:
                best_overlap = overlap
                best = lb
        if not best:
            continue

        med_words = set(cb.text.lower().split())
        large_words = set(best.text.lower().split())
        # Nomes proprios no large-v3 que o medium nao tem
        missing = [w for w in large_words if w[0].isupper() if w not in med_words] if large_words else []

        if missing and len(cb.text) > 20:
            # Verificar se os textos sao similares (nao sao blocos completamente diferentes)
            from difflib import SequenceMatcher
            sim = SequenceMatcher(None, cb.text.lower(), best.text.lower()).ratio()
            if sim > 0.40:
                cb.text = best.text
                proper_noun_fixes += 1

    if proper_noun_fixes:
        summary["proper_noun_fixes"] = proper_noun_fixes
        if verbose:
            print(f"[DIAG] {proper_noun_fixes} blocos corrigidos com nomes proprios ausentes (large-v3)")

    # ── 4. Correcoes lexicais (usa large-v3 como referencia) ──
    if corrections_api_key:
        try:
            from .contextual_corrections import apply_corrections, suggest_corrections_with_llm

            suggestions = suggest_corrections_with_llm(
                base_blocks=corrected,
                compare_blocks=large_v3_blocks,
                model=corrections_llm_model,
                ollama_url=corrections_llm_url,
                llm_provider="deepseek",
                llm_api_key=corrections_api_key,
            )
            if suggestions:
                corrected, applied = apply_corrections(corrected, suggestions, min_occurrences=1)
                summary["lexical_corrections"] = sum(applied.values())
                if verbose:
                    print(f"[DIAG] {len(applied)} correcoes lexicais aplicadas: {list(applied.keys())}")
        except Exception as e:
            if verbose:
                print(f"[DIAG] Correcoes lexicais puladas (erro LLM): {e}")

    summary["total_after"] = len(corrected)
    return corrected, summary

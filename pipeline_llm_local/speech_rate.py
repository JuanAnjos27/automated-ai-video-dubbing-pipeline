import math
import re
from typing import Dict, List, Tuple

from .models import SRTBlock

VOWELS = set("aeiouyáéíóúâêîôûãõàäëïöüąćęłńóśźż")
WINDOW = 5  # vizinhos em cada direcao no time borrowing


def _syllable_groups(word: str) -> int:
    count = 0
    prev_vowel = False
    for ch in word.lower():
        is_vowel = ch in VOWELS
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    return max(1, count)


def count_syllables(text: str) -> int:
    words = re.findall(r"[a-zA-ZÀ-ÿ]+", text)
    if not words:
        return 0
    return sum(_syllable_groups(w) for w in words)


def speech_rate(block: SRTBlock) -> float:
    dur_s = max(0.001, (block.end_ms - block.start_ms) / 1000.0)
    syl = count_syllables(block.text)
    return syl / dur_s


def classify_by_rate(
    blocks: List[SRTBlock],
    hi: float = 6.2,
    lo: float = 2.99,
    critical: float = 8.0,
) -> Dict[str, List[int]]:
    fast: List[int] = []
    slow: List[int] = []
    ok: List[int] = []
    corrupt: List[int] = []
    critical_ids: List[int] = []
    moderate_ids: List[int] = []
    marginal_ids: List[int] = []

    for b in blocks:
        dur = (b.end_ms - b.start_ms) / 1000.0
        if dur <= 0.3:
            corrupt.append(b.index)
            continue
        rate = speech_rate(b)
        if rate > hi:
            fast.append(b.index)
            if rate >= critical:
                critical_ids.append(b.index)
            elif rate >= 7.0:
                moderate_ids.append(b.index)
            else:
                marginal_ids.append(b.index)
        elif rate < lo:
            slow.append(b.index)
        else:
            ok.append(b.index)

    return {
        "fast": fast,
        "slow": slow,
        "ok": ok,
        "corrupt": corrupt,
        "critical": critical_ids,
        "moderate": moderate_ids,
        "marginal": marginal_ids,
    }


def _ideal_duration_ms(block: SRTBlock, hi: float) -> float:
    """Duracao ideal minima para o texto caber sem aceleracao (em ms)."""
    syl = count_syllables(block.text)
    return (syl / max(0.1, hi)) * 1000.0


def can_give(block: SRTBlock, hi: float, min_dur: float) -> float:
    """Quanto tempo (em ms) um bloco pode ceder sem ficar rapido (>hi sil/s)."""
    dur_ms = block.end_ms - block.start_ms
    needed = _ideal_duration_ms(block, hi)
    spare = dur_ms - needed
    return max(0.0, spare)


def analyze_neighbor_solvability(
    bid: int,
    ordered: List[SRTBlock],
    hi: float,
    min_dur: float,
) -> dict:
    """Preve se os vizinhos conseguem resolver um bloco especifico."""
    by_id = {b.index: b for b in ordered}
    block = by_id.get(bid)
    if not block:
        return {"solvable": False, "extra_needed": 0, "total_available": 0, "donors": []}

    pos = next((i for i, x in enumerate(ordered) if x.index == bid), -1)
    if pos == -1:
        return {"solvable": False, "extra_needed": 0, "total_available": 0, "donors": []}

    needed_ms = _ideal_duration_ms(block, hi) - (block.end_ms - block.start_ms)
    if needed_ms <= 0:
        return {"solvable": True, "extra_needed": 0, "total_available": 0, "donors": []}

    total_avail = 0.0
    donors = []

    for delta in range(1, WINDOW + 1):
        for side, idx in [("◀", pos - delta), ("▶", pos + delta)]:
            if 0 <= idx < len(ordered):
                nb = ordered[idx]
                avail = round(can_give(nb, hi, min_dur), 3)
                total_avail += avail
                if avail > 0.01:
                    donors.append(f"#{nb.index}({side}{delta}) +{avail:.2f}s")

    return {
        "solvable": total_avail >= needed_ms,
        "extra_needed": round(needed_ms / 1000, 3),
        "total_available": round(total_avail / 1000, 3),
        "donors": donors,
    }


def _fix_overlaps(ordered: List[SRTBlock], min_dur: float, max_passes: int = 10) -> int:
    """Corrige sobreposicoes entre blocos adjacentes. Roda ate nao ter mais overlaps."""
    fixed = 0
    for _ in range(max_passes):
        had_overlap = False
        for i in range(len(ordered) - 1):
            cur = ordered[i]
            nxt = ordered[i + 1]
            if cur.end_ms > nxt.start_ms:
                had_overlap = True
                overlap = cur.end_ms - nxt.start_ms
                half = overlap / 2.0
                new_cur_end = cur.end_ms - half
                new_nxt_start = nxt.start_ms + half
                if new_cur_end - cur.start_ms >= min_dur * 1000:
                    cur.end_ms = int(new_cur_end)
                else:
                    # Cur nao pode ceder, joga tudo pro nxt
                    nxt.start_ms = int(cur.end_ms)
                    continue
                if nxt.end_ms - new_nxt_start >= min_dur * 1000:
                    nxt.start_ms = int(new_nxt_start)
                else:
                    # Nxt nao pode ceder, joga tudo pro cur
                    cur.end_ms = int(nxt.start_ms)
                fixed += 1
        if not had_overlap:
            break
    return fixed


def adjust_fast_blocks(
    blocks: List[SRTBlock],
    hi: float = 6.2,
    min_dur: float = 1.0,
) -> Tuple[List[SRTBlock], List[int]]:
    """Time borrowing expandido: ate 5 vizinhos em cada direcao.

    Prioriza os blocos mais urgentes (maior sil/s primeiro).
    Cada vizinho so cede o que sobra alem da sua duracao ideal.
    Corrige overlaps ao final.
    """
    ordered = sorted(blocks, key=lambda x: x.index)
    by_id = {b.index: b for b in ordered}

    cls = classify_by_rate(ordered, hi=hi)
    fast_ids = sorted(cls["fast"], key=lambda i: speech_rate(by_id[i]), reverse=True)

    unresolved: List[int] = []

    for bid in fast_ids:
        b = by_id[bid]
        current_ms = b.end_ms - b.start_ms
        needed_ms = _ideal_duration_ms(b, hi) - current_ms
        if needed_ms <= 0.001:
            continue

        pos = next((i for i, x in enumerate(ordered) if x.index == bid), -1)
        if pos == -1:
            unresolved.append(bid)
            continue

        remaining = needed_ms

        for delta in range(1, WINDOW + 1):
            if remaining <= 0.001:
                break

            # ── Vizinho antes ──
            if pos - delta >= 0:
                donor = ordered[pos - delta]
                avail = can_give(donor, hi, min_dur)

                # Se nao pode doar, tenta empurrar ele pra dentro do gap antes dele
                if avail <= 0.001 and pos - delta - 1 >= 0:
                    gap_before_donor = donor.start_ms - ordered[pos - delta - 1].end_ms
                    if gap_before_donor > 0.001:
                        # So desliza o necessario, max 2s pra nao desincronizar
                        slide = min(gap_before_donor, remaining, 2000)
                        donor.start_ms -= int(slide)
                        donor.end_ms -= int(slide)
                        avail = slide  # o slide liberou esse espaco

                if avail > 0.001:
                    give = min(avail, remaining)
                    donor.end_ms -= int(give)
                    b.start_ms -= int(give)
                    remaining -= give

            if remaining <= 0.001:
                break

            # ── Vizinho depois ──
            if pos + delta < len(ordered):
                donor = ordered[pos + delta]
                avail = can_give(donor, hi, min_dur)

                # Se nao pode doar, tenta empurrar ele pra dentro do gap depois dele
                if avail <= 0.001 and pos + delta + 1 < len(ordered):
                    gap_after_donor = ordered[pos + delta + 1].start_ms - donor.end_ms
                    if gap_after_donor > 0.001:
                        # So desliza o necessario, max 2s pra nao desincronizar
                        slide = min(gap_after_donor, remaining, 2000)
                        donor.start_ms += int(slide)
                        donor.end_ms += int(slide)
                        avail = slide

                if avail > 0.001:
                    give = min(avail, remaining)
                    donor.start_ms += int(give)
                    b.end_ms += int(give)
                    remaining -= give

        if remaining > 0.001:
            unresolved.append(bid)

    _fix_overlaps(ordered, min_dur)

    return ordered, sorted(set(unresolved))

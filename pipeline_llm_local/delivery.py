from typing import Dict, List, Tuple

from .models import SRTBlock
from .srt_utils import save_srt


def split_srt(blocks: List[SRTBlock], ratio: float = 0.75) -> Tuple[List[SRTBlock], List[SRTBlock]]:
    ordered = sorted(blocks, key=lambda b: b.index)
    split_idx = int(len(ordered) * ratio)
    return ordered[:split_idx], ordered[split_idx:]


def save_split_outputs(blocks: List[SRTBlock], output_prefix: str, ratio: float = 0.75) -> Dict[str, str]:
    part1, part2 = split_srt(blocks, ratio=ratio)
    p1 = f"{output_prefix}_parte1.srt"
    p2 = f"{output_prefix}_parte2.srt"
    save_srt(p1, part1)
    save_srt(p2, part2)
    return {"part1": p1, "part2": p2}


def save_corrections_srt(
    original_blocks: List[SRTBlock],
    final_blocks: List[SRTBlock],
    output_path: str,
) -> int:
    original_map = {b.index: b.text for b in original_blocks}
    changed = [b for b in final_blocks if b.text != original_map.get(b.index, "")]
    if not changed:
        save_srt(output_path, [])
        return 0
    save_srt(output_path, changed)
    return len(changed)

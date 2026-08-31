import re
from typing import List, Tuple

from .models import SRTBlock


def srt_time_to_ms(time_str: str) -> int:
    norm = time_str.strip().replace(",", ".")
    h, m, rest = norm.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def ms_to_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(path: str) -> List[SRTBlock]:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n"
        r"([\s\S]*?)(?=\n\d+\s*\n|\Z)",
        re.MULTILINE,
    )

    blocks: List[SRTBlock] = []
    for m in pattern.finditer(content):
        text = m.group(4).strip()
        if not text:
            continue
        start = m.group(2).replace(".", ",")
        end = m.group(3).replace(".", ",")
        blocks.append(
            SRTBlock(
                index=int(m.group(1)),
                start=start,
                end=end,
                start_ms=srt_time_to_ms(start),
                end_ms=srt_time_to_ms(end),
                text=text,
            )
        )
    return blocks


def save_srt(path: str, blocks: List[SRTBlock]) -> None:
    lines = []
    for b in blocks:
        start = ms_to_srt_time(b.start_ms)
        end = ms_to_srt_time(b.end_ms)
        lines.append(f"{b.index}\n{start} --> {end}\n{b.text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(lines) + "\n")


def validate_timing(blocks: List[SRTBlock]) -> Tuple[int, int, int]:
    overlaps = 0
    negative = 0
    short = 0
    ordered = sorted(blocks, key=lambda x: x.index)

    for i, b in enumerate(ordered):
        dur = b.end_ms - b.start_ms
        if dur <= 0:
            negative += 1
        elif dur < 500:
            short += 1

        if i < len(ordered) - 1:
            nxt = ordered[i + 1]
            if nxt.start_ms < b.end_ms:
                overlaps += 1

    return overlaps, negative, short

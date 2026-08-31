from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class SRTBlock:
    index: int
    start: str
    end: str
    start_ms: int
    end_ms: int
    text: str
    meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class PipelineReport:
    total_blocks: int = 0
    translated_blocks: int = 0
    language_qa_passed: int = 0
    language_qa_failed: int = 0
    fast_blocks_before: int = 0
    slow_blocks_before: int = 0
    fast_blocks_after: int = 0
    slow_blocks_after: int = 0
    critical_fast_before: int = 0
    critical_fast_after: int = 0
    unresolved_fast_after_adjust: int = 0
    reductions_pending_count: int = 0
    corrections_blocks_count: int = 0
    split_enabled: bool = False
    split_part1_path: str = ""
    split_part2_path: str = ""
    corrections_srt_path: str = ""
    compare_srt_path: str = ""
    diagnostic_report_path: str = ""
    diagnostic_compare_repetition_count: int = 0
    diagnostic_base_repetition_count: int = 0
    diagnostic_severe_windows_count: int = 0
    diagnostic_coverage_gaps_count: int = 0
    diagnostic_coverage_gaps_total_s: float = 0.0
    contextual_suggestions_count: int = 0
    contextual_applied_count: int = 0
    overlap_errors: int = 0
    negative_duration_errors: int = 0
    short_block_errors: int = 0
    corrected_en_srt_path: str = ""
    pt_virgem_srt_path: str = ""
    steps: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    approved_srt_path: str = ""
    rejected_srt_path: str = ""

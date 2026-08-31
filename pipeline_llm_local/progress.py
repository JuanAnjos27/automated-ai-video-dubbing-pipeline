"""
Salvar/retomar progresso da etapa de traducao.

Grava um JSON ao lado do SRT de saida (`<output>.progress.json`) contendo:
- assinatura da configuracao (modelo, idioma, chunk_size, hash do SRT de entrada)
- lista de blocos ja traduzidos

Se a assinatura nao bater na retomada (ex.: usuario mudou o SRT de entrada
ou o idioma alvo), o progresso e descartado para evitar mistura.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from typing import List, Optional

from .models import SRTBlock


def progress_path_for(output_srt: str) -> str:
    base = output_srt[:-4] if output_srt.lower().endswith(".srt") else output_srt
    return f"{base}.progress.json"


def file_signature(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_config_key(
    input_srt: str,
    target_lang: str,
    model: str,
    chunk_size: int,
    style_hint: str,
) -> dict:
    return {
        "input_srt": os.path.abspath(input_srt),
        "input_sha256": file_signature(input_srt),
        "target_lang": target_lang,
        "model": model,
        "chunk_size": chunk_size,
        "style_hint": style_hint,
    }


def save(
    path: str,
    config_key: dict,
    completed_chunks: int,
    total_chunks: int,
    translated: List[SRTBlock],
) -> None:
    payload = {
        "config": config_key,
        "completed_chunks": int(completed_chunks),
        "total_chunks": int(total_chunks),
        "translated": [asdict(b) for b in translated],
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def load(path: str, expected_config_key: dict) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    saved_config = payload.get("config") or {}
    for k, v in expected_config_key.items():
        if saved_config.get(k) != v:
            return None

    translated_raw = payload.get("translated") or []
    blocks: List[SRTBlock] = []
    for item in translated_raw:
        try:
            blocks.append(
                SRTBlock(
                    index=int(item["index"]),
                    start=str(item["start"]),
                    end=str(item["end"]),
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    text=str(item["text"]),
                    meta=dict(item.get("meta") or {}),
                )
            )
        except (KeyError, ValueError, TypeError):
            return None

    return {
        "completed_chunks": int(payload.get("completed_chunks") or 0),
        "total_chunks": int(payload.get("total_chunks") or 0),
        "translated": blocks,
    }


def clear(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass

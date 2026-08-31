import json
import os
import time
from dataclasses import asdict
from typing import List

from . import progress as progress_store
from .contextual_corrections import apply_corrections, suggest_corrections_with_llm
from .delivery import save_corrections_srt, save_split_outputs
from .diagnostics import build_diagnostic_summary, generate_corrected_en_srt
from .language_qa import auto_verify_failed_blocks, full_document_language_qa
from .models import PipelineReport, SRTBlock
from .speech_rate import _fix_overlaps, adjust_fast_blocks, classify_by_rate
from .srt_utils import parse_srt, save_srt, validate_timing
from .translator import chunk_blocks, fix_failed_blocks, translate_all_fast, translate_chunk


def _confirm(checkpoints: bool, message: str) -> None:
    if not checkpoints:
        return
    answer = input(f"{message} [s/N]: ").strip().lower()
    if answer not in {"s", "sim", "y", "yes"}:
        raise RuntimeError("Execucao interrompida no checkpoint")


def _strip_qa_falha(text: str) -> str:
    """Remove prefixo [QA-FALHA] do texto, se presente."""
    if text.startswith("[QA-FALHA] "):
        return text[11:]
    if text.startswith("[QA-FALHA]"):
        return text[10:]
    return text


def _assisted_correction(
    translated_blocks: List[SRTBlock],
    still_failed: List[int],
    en_source_blocks: List[SRTBlock],
    output_srt_path: str,
    report: "PipelineReport",
    verbose: bool = True,
) -> None:
    """Correcao assistida: mostra tabela dos blocos [QA-FALHA] e decide o que fazer.

    Carrega o EN corrigido (se existir) para mostrar o texto que foi realmente traduzido.
    Oferece opcoes: remover todas as tags, manter todas, ou corrigir um a um.
    Em modo nao-interativo (EOFError), mantem os marcadores [QA-FALHA] e segue.
    """
    total = len(still_failed)

    # Carregar EN (corrigido ou original)
    from .srt_utils import parse_srt as _parse_srt
    en_path = output_srt_path.replace("_medium_pt.srt", "_medium_en_corrigido.srt")
    if os.path.exists(en_path):
        en_blocks = _parse_srt(en_path)
        en_fonte = "CORRIGIDO"
    else:
        en_blocks = en_source_blocks
        en_fonte = "ORIGINAL"

    en_by_index = {b.index: b.text for b in en_blocks}

    # Montar dados dos blocos falhos
    falhas = []
    for bid in still_failed:
        pt_block = next((b for b in translated_blocks if b.index == bid), None)
        if not pt_block:
            continue
        falhas.append({
            "id": bid,
            "start": pt_block.start,
            "end": pt_block.end,
            "pt": _strip_qa_falha(pt_block.text),
            "en": en_by_index.get(bid, "(EN nao encontrado)"),
            "block": pt_block,
        })

    # Mostrar tabela
    print(f"\n{'=' * 70}")
    print(f"  CORRECAO ASSISTIDA — {len(falhas)} bloco(s) com [QA-FALHA]")
    print(f"  Fonte EN: {en_fonte}")
    print(f"{'=' * 70}")
    print(f"  {'#':>4}  {'EN (texto original)':<35}  {'PT (traducao)':<35}")
    print(f"  {'-'*4}  {'-'*35}  {'-'*35}")
    for f in falhas:
        en_short = f["en"][:35] + "..." if len(f["en"]) > 38 else f["en"]
        pt_short = f["pt"][:35] + "..." if len(f["pt"]) > 38 else f["pt"]
        print(f"  {f['id']:>4}  {en_short:<38}  {pt_short:<38}")

    # Decidir o que fazer
    print(f"\n  Opcoes:")
    print(f"    1 — Remover todas as tags [QA-FALHA] e seguir (falsos positivos)")
    print(f"    2 — Manter tags e seguir (corrigir depois)")
    print(f"    3 — Corrigir bloco a bloco manualmente")
    resolved: List[int] = []
    kept: List[int] = []

    try:
        opcao = input(f"\n  Escolha [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        opcao = "2"  # modo nao-interativo: mantem tags

    if opcao == "3":
        # Corrigir um a um
        for f in falhas:
            print(f"\n  Bloco #{f['id']}  [{f['start']} -> {f['end']}]")
            print(f"    EN: {f['en']}")
            print(f"    PT: {f['pt']}")
            try:
                correction = input("    Correcao (Enter = manter [QA-FALHA]): ").strip()
                if correction:
                    f["block"].text = correction
                    resolved.append(f["id"])
                else:
                    kept.append(f["id"])
            except (EOFError, KeyboardInterrupt):
                kept.append(f["id"])
                break
    elif opcao == "1" or opcao == "":
        # Remover todas as tags
        for f in falhas:
            f["block"].text = f["pt"]
            resolved.append(f["id"])
        print(f"\n  ✅ {len(resolved)} tags [QA-FALHA] removidas")
    else:
        # Manter tags
        kept = [f["id"] for f in falhas]
        print(f"\n  ⚠️  {len(kept)} tags [QA-FALHA] mantidas para correcao posterior")

    if resolved:
        from .srt_utils import save_srt as _save_srt
        _save_srt(output_srt_path, translated_blocks)

    # Atualizar relatorio
    report.language_qa_passed = len(translated_blocks) - len(kept)
    report.language_qa_failed = len(kept)

    if resolved:
        print(f"  ✅ {len(resolved)} bloco(s) resolvido(s) — SRT re-salvo")
    if kept:
        report.failures.append(
            f"QA: {len(kept)} blocos mantidos [QA-FALHA] "
            f"(ids: {kept[:20]}{'...' if len(kept) > 20 else ''})"
        )
        report.steps.append(
            f"QA final: {report.language_qa_passed} aprovados, {len(kept)} marcados [QA-FALHA]"
        )


def run_pipeline(
    input_srt: str,
    output_srt: str,
    report_path: str,
    target_lang: str,
    model: str,
    ollama_url: str,
    style_hint: str,
    chunk_size: int = 15,
    checkpoints: bool = True,
    auto_retry_failed_language: bool = True,
    hi: float = 6.2,
    lo: float = 2.99,
    min_dur: float = 1.0,
    adjust_timestamps: bool = True,
    generate_split: bool = False,
    split_ratio: float = 0.75,
    generate_corrections_srt: bool = True,
    corrections_srt_path: str | None = None,
    compare_srt: str | None = None,
    diagnostics_path: str | None = None,
    suggest_contextual_corrections: bool = False,
    min_correction_occurrences: int = 2,
    skip_translation: bool = False,
    skip_language_qa: bool = False,
    progress_every: int = 100,
    verbose: bool = True,
    llm_timeout: int = 240,
    resume_translation: bool = True,
    llm_provider: str = "ollama",
    llm_api_key: str | None = None,
    fast_translate: bool = False,
) -> PipelineReport:
    report = PipelineReport()
    started_at = time.time()

    if verbose:
        print("[PIPELINE] Iniciando execucao...")
        print(f"[PIPELINE] Entrada: {input_srt}")
        if compare_srt:
            print(f"[PIPELINE] Compare: {compare_srt}")

    blocks = parse_srt(input_srt)
    if not blocks:
        raise RuntimeError("Nenhum bloco encontrado no SRT de entrada")

    # Guardar EN original ANTES de qualquer correcao (usado pelo fix_failed_blocks)
    en_source_blocks = [
        SRTBlock(
            index=b.index, start=b.start, end=b.end,
            start_ms=b.start_ms, end_ms=b.end_ms,
            text=b.text, meta=dict(b.meta),
        )
        for b in blocks
    ]

    original_blocks = [
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
    report.total_blocks = len(blocks)
    report.steps.append(f"Entrada carregada: {len(blocks)} blocos")
    if verbose:
        print(f"[PIPELINE] Blocos carregados: {len(blocks)}")

    if compare_srt:
        cmp_blocks = parse_srt(compare_srt)
        if verbose:
            print(f"[DIAG] Blocos compare carregados: {len(cmp_blocks)}")
        diag = build_diagnostic_summary(blocks, cmp_blocks)
        report.compare_srt_path = os.path.abspath(compare_srt)
        report.diagnostic_base_repetition_count = int(diag["base_repetition_count"])
        report.diagnostic_compare_repetition_count = int(diag["compare_repetition_count"])
        report.diagnostic_severe_windows_count = int(diag["severe_windows_count"])
        report.diagnostic_coverage_gaps_count = int(diag["coverage_gaps_count"])
        report.diagnostic_coverage_gaps_total_s = float(diag["coverage_gaps_total_s"])
        report.steps.append(
            "Diagnostico medium/large concluido "
            f"(rep_base={report.diagnostic_base_repetition_count}, "
            f"rep_compare={report.diagnostic_compare_repetition_count}, "
            f"janelas_severas={report.diagnostic_severe_windows_count}, "
            f"gaps_cobertura={report.diagnostic_coverage_gaps_count} "
            f"({report.diagnostic_coverage_gaps_total_s}s))"
        )

        if diagnostics_path:
            os.makedirs(os.path.dirname(diagnostics_path), exist_ok=True)
            with open(diagnostics_path, "w", encoding="utf-8") as f:
                json.dump(diag, f, ensure_ascii=False, indent=2)
            report.diagnostic_report_path = os.path.abspath(diagnostics_path)

        if suggest_contextual_corrections:
            if verbose:
                print("[DIAG] Gerando sugestoes contextuais...")
            suggestions = suggest_corrections_with_llm(
                base_blocks=blocks,
                compare_blocks=cmp_blocks,
                model=model,
                ollama_url=ollama_url,
                llm_provider=llm_provider,
                llm_api_key=llm_api_key,
            )
            report.contextual_suggestions_count = len(suggestions)
            if suggestions:
                blocks, applied = apply_corrections(
                    blocks=blocks,
                    corrections=suggestions,
                    min_occurrences=min_correction_occurrences,
                )
                report.contextual_applied_count = len(applied)
                report.steps.append(
                    "Correcoes contextuais aplicadas "
                    f"(sugestoes={report.contextual_suggestions_count}, "
                    f"aplicadas={report.contextual_applied_count})"
                )
                if verbose:
                    print(
                        "[DIAG] Correcoes contextuais aplicadas: "
                        f"{report.contextual_applied_count}/{report.contextual_suggestions_count}"
                    )

        # ── Etapa 2: Gerar EN corrigido ──
        if compare_srt and llm_api_key:
            if verbose:
                print("[DIAG] Gerando medium EN corrigido...")
            corrected_en, corr_summary = generate_corrected_en_srt(
                medium_blocks=blocks,
                large_v3_blocks=cmp_blocks,
                corrections_api_key=llm_api_key,
                verbose=verbose,
            )
            # Salvar _medium_en_corrigido.srt
            base_name = input_srt.replace("_medium_en.srt", "").replace(".srt", "")
            if not base_name.endswith("_en_corrigido"):
                base_name = input_srt[:-4] if input_srt.endswith(".srt") else input_srt
                corrected_en_path = base_name.replace("_medium_en", "_medium_en_corrigido")
                if not corrected_en_path.endswith(".srt"):
                    corrected_en_path += ".srt"
            else:
                corrected_en_path = input_srt

            save_srt(corrected_en_path, corrected_en)
            report.corrected_en_srt_path = os.path.abspath(corrected_en_path)
            report.steps.append(
                f"EN corrigido salvo: {corr_summary['total_after']} blocos "
                f"(gaps={corr_summary['gaps_inserted']}, "
                f"rep_remov={corr_summary['repetitions_removed']}, "
                f"hall_remov={corr_summary['hallucinations_removed']})"
            )
            if verbose:
                print(f"[DIAG] EN corrigido: {corr_summary['total_before']} → {corr_summary['total_after']} blocos")
                print(f"        gaps inseridos={corr_summary['gaps_inserted']}, "
                      f"repeticoes removidas={corr_summary['repetitions_removed']}, "
                      f"alucinacoes removidas={corr_summary['hallucinations_removed']}")
            # Usar corrigido como entrada da traducao
            blocks = corrected_en
            report.total_blocks = len(blocks)

            # Corrigir blocos com duracao zero (originais do Whisper) antes de validar
            fix_neg = 0
            for b in blocks:
                if b.end_ms <= b.start_ms:
                    b.end_ms = b.start_ms + 1000
                    h = b.end_ms // 3600000
                    m = (b.end_ms % 3600000) // 60000
                    s = (b.end_ms % 60000) // 1000
                    ms = b.end_ms % 1000
                    b.end = f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
                    fix_neg += 1
            if fix_neg:
                if verbose:
                    print(f"[DIAG] {fix_neg} blocos com duracao zero corrigidos (1s)")
                from .srt_utils import save_srt as _save_srt
                _save_srt(corrected_en_path, corrected_en)

        elif compare_srt and not llm_api_key:
            if verbose:
                print("[DIAG] EN corrigido pulado: sem API key para correcoes lexicais")

    overlaps, negative, short = validate_timing(blocks)
    report.overlap_errors = overlaps
    report.negative_duration_errors = negative
    report.short_block_errors = short

    if overlaps > 0 or negative > 0:
        report.failures.append(
            f"SRT de entrada invalido (overlaps={overlaps}, duracoes_negativas={negative})"
        )
        _write_report(report_path, report)
        raise RuntimeError("SRT de entrada possui erros criticos de timestamp")

    _confirm(checkpoints, "Etapa 1 concluida (ingestao + validacao). Avancar?")

    translated: List[SRTBlock] = []
    if skip_translation:
        translated = [
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
        report.steps.append("Traducao pulada por configuracao (--skip-translation)")
        if verbose:
            print("[TRAD] Etapa pulada (--skip-translation)")
    else:
        if fast_translate:
            if verbose:
                print("[TRAD] Modo rapido: deepseek-chat com checkpoints incrementais")
            translated = translate_all_fast(
                blocks=blocks,
                output_srt_path=output_srt,
                target_lang=target_lang,
                style_hint=style_hint,
                llm_api_key=llm_api_key,
                chunk_size=chunk_size,
                request_timeout=llm_timeout,
                resume=resume_translation,
                verbose=verbose,
            )
        else:
            chunks = chunk_blocks(blocks, chunk_size=chunk_size)
            total_chunks = len(chunks)

            progress_file = progress_store.progress_path_for(output_srt)
            config_key = progress_store.build_config_key(
                input_srt=input_srt,
                target_lang=target_lang,
                model=model,
                chunk_size=chunk_size,
                style_hint=style_hint,
            )

            start_from_chunk = 0
            if resume_translation:
                saved = progress_store.load(progress_file, config_key)
                if saved and saved["completed_chunks"] > 0:
                    translated = saved["translated"]
                    start_from_chunk = saved["completed_chunks"]
                    report.steps.append(
                        f"Retomada de traducao: {start_from_chunk}/{total_chunks} chunks ja feitos"
                    )
                    if verbose:
                        print(
                            f"[TRAD] Retomando do chunk {start_from_chunk + 1}/{total_chunks} "
                            f"({len(translated)} blocos ja traduzidos em {progress_file})"
                        )
                elif saved is None and os.path.exists(progress_file):
                    if verbose:
                        print(
                            "[TRAD] Arquivo de progresso encontrado mas incompativel "
                            "(input/idioma/modelo mudaram). Comecando do zero."
                        )
                    progress_store.clear(progress_file)
            else:
                progress_store.clear(progress_file)

            if verbose:
                if start_from_chunk == 0:
                    print(f"[TRAD] Iniciando traducao em {total_chunks} chunks...")
                else:
                    print(f"[TRAD] Continuando traducao ate o chunk {total_chunks}...")

            for i, chunk in enumerate(chunks, start=1):
                if i <= start_from_chunk:
                    continue
                if verbose:
                    first_id = chunk[0].index
                    last_id = chunk[-1].index
                    print(f"[TRAD] Chunk {i}/{total_chunks} (blocos {first_id}-{last_id})")
                out = translate_chunk(
                    chunk=chunk,
                    model=model,
                    ollama_url=ollama_url,
                    target_lang=target_lang,
                    style_hint=style_hint,
                    request_timeout=llm_timeout,
                    llm_provider=llm_provider,
                    llm_api_key=llm_api_key,
                )
                translated.extend(out)
                try:
                    progress_store.save(
                        path=progress_file,
                        config_key=config_key,
                        completed_chunks=i,
                        total_chunks=total_chunks,
                        translated=translated,
                    )
                except OSError as exc:
                    if verbose:
                        print(f"[TRAD][WARN] Falha ao salvar progresso: {exc}")

            progress_store.clear(progress_file)

        translated.sort(key=lambda x: x.index)
        report.steps.append(f"Traducao concluida em chunks de {chunk_size}")
        if verbose:
            print("[TRAD] Traducao concluida")

    # ── Salvar PT virgem (ANTES de QA e ajuste de timestamps) ──
    virgem_path = output_srt.replace("_pt.srt", "_pt_virgem.srt")
    if virgem_path == output_srt:
        virgem_path = output_srt[:-4] + "_virgem.srt"
    save_srt(virgem_path, translated)
    report.pt_virgem_srt_path = os.path.abspath(virgem_path)
    report.steps.append(f"PT virgem salvo: {len(translated)} blocos (sem ajustes)")
    if verbose:
        print(f"[OUT] PT virgem salvo: {os.path.abspath(virgem_path)}")

    report.translated_blocks = len(translated)

    _confirm(checkpoints, "Etapa 3 concluida (traducao). Avancar para QA de idioma total?")

    if skip_language_qa:
        report.language_qa_passed = len(translated)
        report.language_qa_failed = 0
        report.steps.append("QA de idioma pulada por configuracao (--skip-language-qa)")
        if verbose:
            print("[QA] Etapa pulada (--skip-language-qa)")
    else:
        # ── QA Passo 1 ──
        passed, failed, failed_ids = full_document_language_qa(
            blocks=translated,
            target_lang=target_lang,
            model=model,
            ollama_url=ollama_url,
            progress_every=progress_every,
            verbose=verbose,
            request_timeout=llm_timeout,
            llm_provider=llm_provider,
            llm_api_key=llm_api_key,
        )
        report.language_qa_passed = passed
        report.language_qa_failed = failed

        # ── Auto-verificacao: compara EN original com PT para detectar falsos positivos ──
        if failed > 0 and llm_provider == "deepseek":
            if verbose:
                print(f"\n[QA-VERIFY] Verificando {failed} blocos reprovados contra EN original...")
            translated, failed_ids, auto_resolved = auto_verify_failed_blocks(
                blocks=translated,
                failed_ids=failed_ids,
                en_source_blocks=en_source_blocks,
                target_lang=target_lang,
                model=model,
                ollama_url=ollama_url,
                request_timeout=llm_timeout,
                llm_provider=llm_provider,
                llm_api_key=llm_api_key,
                verbose=verbose,
            )
            report.language_qa_passed += auto_resolved
            report.language_qa_failed = len(failed_ids)
            report.steps.append(
                f"QA-VERIFY: {auto_resolved}/{failed - report.language_qa_failed + auto_resolved} "
                f"falsos positivos removidos automaticamente"
            )

        # ── Corrigir blocos reprovados (NUNCA remover) ──
        if failed_ids and auto_retry_failed_language:
            if verbose:
                print(f"[QA] Corrigindo {len(failed_ids)} blocos reprovados...")
            translated, still_failed = fix_failed_blocks(
                blocks=translated,
                failed_ids=failed_ids,
                en_blocks=en_source_blocks,
                target_lang=target_lang,
                style_hint=style_hint,
                llm_api_key=llm_api_key,
                max_retries=3,
                verbose=verbose,
            )

            # ── Correcao assistida dos blocos [QA-FALHA] ──
            if still_failed:
                _assisted_correction(
                    translated_blocks=translated,
                    still_failed=still_failed,
                    en_source_blocks=en_source_blocks,
                    output_srt_path=output_srt,
                    report=report,
                    verbose=verbose,
                )
            else:
                report.steps.append("QA final: todos os blocos aprovados apos correcao")
                if verbose:
                    print("[QA] Todos os blocos aprovados apos correcao")
        elif failed_ids and not auto_retry_failed_language:
            report.failures.append(
                f"QA: {len(failed_ids)} blocos reprovados (auto-retry desabilitado)"
            )

    cls_before = classify_by_rate(translated, hi=hi, lo=lo)
    report.fast_blocks_before = len(cls_before["fast"])
    report.slow_blocks_before = len(cls_before["slow"])
    report.critical_fast_before = len(cls_before["critical"])
    report.steps.append(
        "Analise de taxa de fala concluida "
        f"(fast={report.fast_blocks_before}, slow={report.slow_blocks_before})"
    )
    if verbose:
        print(
            "[RITMO] Antes do ajuste: "
            f"fast={report.fast_blocks_before}, slow={report.slow_blocks_before}, "
            f"criticos={report.critical_fast_before}"
        )

    unresolved_after_adjust: List[int] = []
    if adjust_timestamps:
        translated, unresolved_after_adjust = adjust_fast_blocks(
            translated,
            hi=hi,
            min_dur=min_dur,
        )
        report.unresolved_fast_after_adjust = len(unresolved_after_adjust)
        report.steps.append(
            "Ajuste de timestamps concluido "
            f"(nao resolvidos={report.unresolved_fast_after_adjust})"
        )
        if verbose:
            print(f"[RITMO] Ajuste concluido. Nao resolvidos={report.unresolved_fast_after_adjust}")

    overlaps2, negative2, short2 = validate_timing(translated)
    if overlaps2 > 0 or negative2 > 0:
        if verbose:
            print(f"[RITMO] {overlaps2} overlaps, {negative2} duracoes negativas — tentando corrigir...")
        _fix_overlaps(translated, min_dur)
        # Re-ordenar e re-validar
        translated.sort(key=lambda b: b.index)
        overlaps2, negative2, short2 = validate_timing(translated)
        if overlaps2 > 0 or negative2 > 0:
            report.failures.append(
                "Ajustes criaram inconsistencias de timing que nao puderam ser corrigidas "
                f"(overlaps={overlaps2}, duracoes_negativas={negative2})"
            )
            if verbose:
                print(f"[RITMO] ⚠️ Ainda ha {overlaps2} overlaps, {negative2} duracoes negativas — salvando assim mesmo")
            # Nao crasha — salva com warning

    cls_after = classify_by_rate(translated, hi=hi, lo=lo)
    report.fast_blocks_after = len(cls_after["fast"])
    report.slow_blocks_after = len(cls_after["slow"])
    report.critical_fast_after = len(cls_after["critical"])
    report.reductions_pending_count = len(cls_after["critical"]) + len(cls_after["moderate"])
    if verbose:
        print(
            "[RITMO] Depois do ajuste: "
            f"fast={report.fast_blocks_after}, slow={report.slow_blocks_after}, "
            f"criticos={report.critical_fast_after}, pendentes={report.reductions_pending_count}"
        )

    _confirm(checkpoints, "Etapa 7 concluida (QA idioma + timing). Avancar para salvar?")

    save_srt(output_srt, translated)
    report.steps.append(f"SRT final salvo em: {os.path.abspath(output_srt)}")
    if verbose:
        print(f"[OUT] SRT final salvo: {os.path.abspath(output_srt)}")

    if generate_corrections_srt:
        corr_path = corrections_srt_path
        if not corr_path:
            base = output_srt[:-4] if output_srt.lower().endswith(".srt") else output_srt
            corr_path = f"{base}_correcoes.srt"
        changed = save_corrections_srt(
            original_blocks=original_blocks,
            final_blocks=translated,
            output_path=corr_path,
        )
        report.corrections_blocks_count = changed
        report.corrections_srt_path = os.path.abspath(corr_path)
        report.steps.append(f"SRT de correcoes salvo ({changed} blocos)")
        if verbose:
            print(f"[OUT] SRT de correcoes salvo ({changed} blocos): {report.corrections_srt_path}")

    if generate_split:
        base = output_srt[:-4] if output_srt.lower().endswith(".srt") else output_srt
        split_paths = save_split_outputs(translated, output_prefix=base, ratio=split_ratio)
        report.split_enabled = True
        report.split_part1_path = os.path.abspath(split_paths["part1"])
        report.split_part2_path = os.path.abspath(split_paths["part2"])
        report.steps.append("Split 75/25 gerado")
        if verbose:
            print(f"[OUT] Split gerado: {report.split_part1_path} | {report.split_part2_path}")

    _write_report(report_path, report)
    if verbose:
        elapsed = time.time() - started_at
        print(f"[PIPELINE] Concluida em {elapsed:.1f}s")
    return report


def _write_report(path: str, report: PipelineReport) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, ensure_ascii=False, indent=2)

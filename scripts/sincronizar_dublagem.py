"""
Script de sincronização de dublagem com vídeo original (VERSÃO ORIGINAL)
=========================================================================

O QUE ESTE SCRIPT FAZ:
  - Monta uma trilha de dublagem juntando arquivos MP3 nos encaixes de tempo do SRT
  - Acelera o áudio (speedup) quando o MP3 é mais longo que o slot do SRT (até 2.0x, limite do ffmpeg)
  - Combina a trilha com o vídeo original (áudio original a 2% de volume)
  - Gera um log dos trechos com aceleração acima de 1.5x

O QUE ESTE SCRIPT NÃO FAZ:
  - NÃO desacelera áudio curto (ficam silêncios)
  - NÃO reduz gaps de silêncio entre trechos
  - NÃO mexe na velocidade do vídeo (vídeo é copiado sem alterações)
  - NÃO re-encoda o vídeo (usa -c:v copy)

Use quando: o áudio da dublagem já está bem ajustado e só precisa de correções
simples de áudio sem mexer no vídeo.

REQUISITOS:
    pip install pydub
    ffmpeg instalado no sistema (https://ffmpeg.org/)

ESTRUTURA DE ARQUIVOS ESPERADA:
    - video original:   video.mp4         (ou .mkv, .avi, etc.)
    - arquivo SRT:      legendas.srt
    - áudios dublagem:  trecho1.mp3, trecho2.mp3, ... (um por trecho/bloco gerado)

    Coloque todos os arquivos na mesma pasta que este script, ou ajuste os
    caminhos nas variáveis de configuração abaixo.

RESULTADO:
    video_dublado.mp4  — vídeo com dublagem + áudio original baixo (20%)
"""

import os
import re
import subprocess
from pathlib import Path
from pydub import AudioSegment

# =============================================================================
# CONFIGURAÇÃO — ajuste aqui
# =============================================================================

DEFAULT_VIDEO_ORIGINAL = str(Path(__file__).resolve().parent.parent / "video.mp4")
DEFAULT_SRT_FILE       = "legendas.srt"        # arquivo SRT original (com timestamps)
AUDIO_PREFIX     = "bloco_"              # prefixo dos arquivos de áudio (bloco_1.mp3, bloco_2.mp3...)
AUDIO_EXT        = ".mp3"               # extensão dos arquivos de áudio
DEFAULT_VOLUME_ORIGINAL = 0.02           # volume do áudio original do vídeo (0.0 a 1.0)
DEFAULT_VOLUME_DUB      = 1.2            # volume da dublagem (1.0 = 100%)
DEFAULT_OUTPUT_VIDEO   = "video_dublado.mp4"   # nome do vídeo final
DEFAULT_ACCEL_LOG_FILE = "trechos_acelerados.log"  # log dos trechos que exigiram aceleração

# Pasta onde estão os arquivos de áudio (mesma pasta do script por padrão)
DEFAULT_AUDIO_DIR      = str(Path(__file__).resolve().parent.parent / "audios_blocos")

# Binário do ffmpeg: prioriza o do sistema, depois o embutido do imageio-ffmpeg
import shutil as _shutil

def _encontrar_ffmpeg():
    exe = _shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _venv = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             ".venv311/lib/python3.11/site-packages/imageio_ffmpeg/binaries")
        fallback = os.path.join(_venv, "ffmpeg-macos-aarch64-v7.1")
        return fallback if os.path.exists(fallback) else "ffmpeg"

FFMPEG  = _encontrar_ffmpeg()
FFPROBE = FFMPEG  # imageio-ffmpeg não inclui ffprobe; usamos ffmpeg com -show_entries via ffprobe emulado abaixo

# Configura pydub para usar o ffmpeg embutido (evita "ffprobe not found")
AudioSegment.converter = FFMPEG
AudioSegment.ffmpeg    = FFMPEG
AudioSegment.ffprobe   = FFMPEG

# Monkey-patch: pydub.utils.get_prober_name ignora AudioSegment.ffprobe — corrigimos aqui
import pydub.utils as _pydub_utils
_pydub_utils.get_prober_name = lambda: FFMPEG


def _mediainfo_json_via_ffmpeg(filepath, read_ahead_limit=-1):
    """Substitui pydub.utils.mediainfo_json usando ffmpeg -i (sem precisar de ffprobe)."""
    import json as _json, re as _re, subprocess as _sp, os as _os
    try:
        fp = _pydub_utils.fsdecode(filepath)
    except TypeError:
        fp = None

    cmd = [FFMPEG, "-i", fp] if fp else [FFMPEG, "-i", "pipe:0"]
    res = _sp.run(cmd, stdin=_sp.DEVNULL if fp else None,
                  stdout=_sp.PIPE, stderr=_sp.PIPE)
    stderr = res.stderr.decode("utf-8", "ignore")

    # Extrai Duration
    m_dur = _re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr)
    duration = 0.0
    if m_dur:
        duration = int(m_dur.group(1))*3600 + int(m_dur.group(2))*60 + float(m_dur.group(3))

    # Extrai bitrate global
    m_br = _re.search(r"bitrate:\s*(\d+)\s*kb/s", stderr)
    bit_rate = int(m_br.group(1)) * 1000 if m_br else 0

    # Extrai stream de áudio
    m_stream = _re.search(
        r"Stream #(\d+:\d+)[^:]*: Audio:\s*(\w+).*?(\d+)\s*Hz.*?(\w+),\s*([\d.]+)\s*kb/s",
        stderr)
    if not m_stream:
        m_stream = _re.search(
            r"Stream #(\d+:\d+)[^:]*: Audio:\s*(\w+).*?(\d+)\s*Hz",
            stderr)

    codec_name  = m_stream.group(2) if m_stream else "mp3"
    sample_rate = int(m_stream.group(3)) if m_stream else 44100

    # Canais
    channels = 1
    if "stereo" in stderr:
        channels = 2
    elif "mono" in stderr:
        channels = 1
    m_ch = _re.search(r"(\d+)\s*channels?", stderr)
    if m_ch:
        channels = int(m_ch.group(1))

    stream_index = 0
    size = _os.path.getsize(fp) if fp and _os.path.exists(fp) else 0

    info = {
        "streams": [{
            "index": stream_index,
            "codec_type": "audio",
            "codec_name": codec_name,
            "sample_rate": str(sample_rate),
            "channels": channels,
            "duration": str(duration),
            "bits_per_sample": 0,
            "sample_fmt": "fltp",  # aciona workaround pydub → usa pcm_s16le
        }],
        "format": {
            "duration": str(duration),
            "bit_rate": str(bit_rate),
            "size": str(size),
        }
    }
    return info


_pydub_utils.mediainfo_json = _mediainfo_json_via_ffmpeg

# audio_segment.py importa mediainfo_json por valor — precisamos patchear lá também
import pydub.audio_segment as _pydub_audio_segment
_pydub_audio_segment.mediainfo_json = _mediainfo_json_via_ffmpeg

# =============================================================================
# FUNÇÕES
# =============================================================================

def srt_time_to_ms(time_str):
    """Converte timestamp SRT (HH:MM:SS,mmm) para milissegundos."""
    time_str = time_str.strip().replace(",", ".")
    h, m, rest = time_str.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def parse_srt(srt_path):
    """
    Lê o SRT e retorna uma lista de blocos:
    [{"index": 1, "start_ms": ..., "end_ms": ..., "text": "..."}, ...]
    """
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = []
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n"
        r"([\s\S]*?)(?=\n\d+\s*\n|\Z)",
        re.MULTILINE
    )
    for m in pattern.finditer(content):
        blocks.append({
            "index":    int(m.group(1)),
            "start_ms": srt_time_to_ms(m.group(2)),
            "end_ms":   srt_time_to_ms(m.group(3)),
            "text":     m.group(4).strip(),
        })
    return blocks


def group_blocks_by_trecho(blocks, audio_dir, prefix, ext):
    """
    Agrupa os blocos do SRT nos mesmos trechos usados para gerar os áudios.
    Detecta automaticamente quantos arquivos de áudio existem e quais blocos
    pertencem a cada trecho (baseado no marcador FIM do arquivo de blocos).
    """
    trechos = []
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+){re.escape(ext)}$")
    audio_files = []
    for nome in os.listdir(audio_dir):
        match = pattern.match(nome)
        if match:
            audio_files.append((int(match.group(1)), nome))

    for trecho_num, nome in sorted(audio_files):
        path = os.path.join(audio_dir, nome)
        trechos.append({"num": trecho_num, "audio_path": path, "blocks": []})

    if not trechos:
        raise FileNotFoundError(
            f"Nenhum arquivo de áudio encontrado com padrão '{prefix}N{ext}' em '{audio_dir}'"
        )

    print(f"Encontrados {len(trechos)} arquivos de áudio.")
    if trechos[0]["num"] != 1 or len(trechos) != (trechos[-1]["num"] - trechos[0]["num"] + 1):
        print(
            f"Numeração detectada: {trechos[0]['num']} até {trechos[-1]['num']} "
            f"(lacunas toleradas)."
        )

    # Lê o arquivo de blocos para saber quais índices SRT pertencem a cada trecho
    blocos_txt = os.path.join(audio_dir, "transcricao_blocos.txt")
    if not os.path.exists(blocos_txt):
        # Fallback: mapeamento 1:1 por índice (bloco_N.mp3 ↔ bloco SRT de índice N)
        print("Arquivo transcricao_blocos.txt não encontrado — mapeando 1:1 por índice.")
        block_by_index = {b["index"]: b for b in blocks}
        for t in trechos:
            b = block_by_index.get(t["num"])
            if b:
                t["blocks"] = [b]
        trechos = [t for t in trechos if t["blocks"]]
        print(f"Mapeados {len(trechos)} trechos com bloco SRT correspondente.")
        return trechos

    with open(blocos_txt, "r", encoding="utf-8") as f:
        raw = f.read()

    # Divide pelo marcador FIM
    partes = [p.strip() for p in raw.split("FIM") if p.strip()]
    block_idx = 0
    for i, parte in enumerate(partes):
        linhas = [l for l in parte.split("\n") if l.strip()]
        count = len(linhas)
        if i < len(trechos):
            trechos[i]["blocks"] = blocks[block_idx: block_idx + count]
        block_idx += count

    return trechos


def build_dub_track(trechos, total_duration_ms):
    """
    Monta a trilha de dublagem completa:
    - Para cada trecho, pega o áudio e o encaixa no intervalo de tempo do SRT
    - Se o áudio for mais longo que o intervalo, comprime (speedup)
    - Se for mais curto, deixa o silêncio no final do bloco
    """
    dub_track = AudioSegment.silent(duration=total_duration_ms)
    accelerated_segments = []

    def load_audio_segment(path):
        """Carrega áudio detectando o formato real pelo header."""
        with open(path, "rb") as f:
            header = f.read(12)

        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            return AudioSegment.from_file(path, format="wav")

        if header.startswith(b"ID3") or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            return AudioSegment.from_file(path, format="mp3")

        # Fallback: deixa o ffmpeg detectar automaticamente.
        return AudioSegment.from_file(path)

    for trecho in trechos:
        if not trecho["blocks"]:
            continue

        start_ms = trecho["blocks"][0]["start_ms"]
        end_ms   = trecho["blocks"][-1]["end_ms"]
        slot_ms  = end_ms - start_ms

        print(f"  Trecho {trecho['num']}: {start_ms/1000:.1f}s → {end_ms/1000:.1f}s "
              f"(slot={slot_ms/1000:.1f}s) | áudio: {trecho['audio_path']}")

        audio = load_audio_segment(trecho["audio_path"])
        audio_ms = len(audio)

        # Se o áudio ultrapassar o slot, acelera proporcionalmente
        if audio_ms > slot_ms:
            ratio = audio_ms / slot_ms
            print(f"    ⚡ Áudio longo ({audio_ms/1000:.1f}s), acelerando {ratio:.2f}x")
            accelerated_segments.append({
                "trecho_num": trecho["num"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "slot_ms": slot_ms,
                "original_audio_ms": audio_ms,
                "ratio": ratio,
            })
            # Usa ffmpeg para fazer o speedup sem alterar pitch
            tmp_in  = f"/tmp/trecho_{trecho['num']}_orig.mp3"
            tmp_out = f"/tmp/trecho_{trecho['num']}_fast.mp3"
            audio.export(tmp_in, format="mp3")
            subprocess.run([
                FFMPEG, "-y", "-i", tmp_in,
                "-filter:a", f"atempo={min(ratio, 2.0):.4f}",
                tmp_out
            ], check=True, capture_output=True)
            audio = AudioSegment.from_file(tmp_out, format="mp3")

        # Recorta ao tamanho do slot caso ainda seja maior
        audio = audio[:slot_ms]

        dub_track = dub_track.overlay(audio, position=start_ms)

    return dub_track, accelerated_segments

ACCEL_LOG_THRESHOLD = 1.5  # só registra no log trechos com aceleração acima deste valor


def write_acceleration_log(log_path, accelerated_segments):
    """Gera arquivo de log com os trechos com aceleração > ACCEL_LOG_THRESHOLD."""
    relevant = [s for s in accelerated_segments if s["ratio"] > ACCEL_LOG_THRESHOLD]
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Trechos acelerados (aceleração > {ACCEL_LOG_THRESHOLD}x) - sincronizar_dublagem.py\n")
        f.write("=" * 60 + "\n")

        if not relevant:
            f.write(f"Nenhum trecho com aceleração acima de {ACCEL_LOG_THRESHOLD}x.\n")
            return

        for seg in relevant:
            f.write(
                f"Trecho {seg['trecho_num']}: "
                f"{seg['start_ms']/1000:.3f}s -> {seg['end_ms']/1000:.3f}s | "
                f"slot={seg['slot_ms']/1000:.3f}s | "
                f"audio_original={seg['original_audio_ms']/1000:.3f}s | "
                f"aceleracao={seg['ratio']:.4f}x\n"
            )


def ask_value(prompt_text, default_value):
    """Lê um valor do terminal com opção de manter padrão ao pressionar Enter."""
    raw = input(f"{prompt_text} [{default_value}]: ").strip()
    return raw if raw else default_value


def get_video_duration_ms(video_path):
    """Obtém duração do vídeo em milissegundos via ffmpeg."""
    result = subprocess.run([
        FFMPEG, "-i", video_path
    ], capture_output=True, text=True)
    # ffmpeg retorna metadados no stderr quando não há output; extraímos duração
    import re as _re
    match = _re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
    if not match:
        raise RuntimeError(f"Não foi possível obter a duração de: {video_path}")
    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return int((h * 3600 + m * 60 + s) * 1000)


# =============================================================================
# EXECUÇÃO PRINCIPAL
# =============================================================================

def main():
    print("=" * 60)
    print("  Sincronizador de Dublagem")
    print("=" * 60)

    print("\nInforme os arquivos/pastas de entrada (Enter mantém o padrão):")
    video_original = ask_value("Vídeo de entrada", DEFAULT_VIDEO_ORIGINAL)
    srt_file = ask_value("Arquivo SRT", DEFAULT_SRT_FILE)
    audio_dir = ask_value("Pasta dos áudios de dublagem", DEFAULT_AUDIO_DIR)

    print("\nInforme os nomes de saída (Enter mantém o padrão):")
    output_video = ask_value("Arquivo de vídeo gerado", DEFAULT_OUTPUT_VIDEO)
    accel_log_file = ask_value("Arquivo de log de aceleração", DEFAULT_ACCEL_LOG_FILE)

    print("\nInforme os volumes (Enter mantém o padrão):")
    vol_orig_raw = ask_value("Volume do áudio original do vídeo (0.0 a 1.0)", str(DEFAULT_VOLUME_ORIGINAL))
    vol_dub_raw  = ask_value("Volume da dublagem (0.0 a 1.0, padrão 1.0 = 100%)", str(DEFAULT_VOLUME_DUB))
    volume_original = float(vol_orig_raw)
    volume_dub      = float(vol_dub_raw)

    # 1. Verifica arquivos
    for f in [video_original, srt_file]:
        if not os.path.exists(f):
            raise FileNotFoundError(f"Arquivo não encontrado: {f}")

    if not os.path.isdir(audio_dir):
        raise FileNotFoundError(f"Pasta de áudios não encontrada: {audio_dir}")

    # 2. Lê SRT e duração do vídeo
    print("\n[1/5] Lendo SRT...")
    blocks = parse_srt(srt_file)
    print(f"      {len(blocks)} blocos encontrados.")

    print("[2/5] Obtendo duração do vídeo...")
    total_ms = get_video_duration_ms(video_original)
    print(f"      Duração: {total_ms/1000:.1f}s")

    # 3. Agrupa blocos por trecho
    print("[3/5] Agrupando blocos por trecho de áudio...")
    trechos = group_blocks_by_trecho(blocks, audio_dir, AUDIO_PREFIX, AUDIO_EXT)

    # 4. Monta trilha de dublagem
    print("[4/5] Montando trilha de dublagem...")
    dub_track, accelerated_segments = build_dub_track(trechos, total_ms)

    tmp_dub = "/tmp/dublagem_sincronizada.mp3"
    print(f"      Exportando trilha para {tmp_dub}...")
    dub_track.export(tmp_dub, format="mp3")

    # 5. Combina com vídeo original
    print("[5/5] Combinando com vídeo original...")
    import math as _math
    vol_orig_db = 20 * _math.log10(max(volume_original, 1e-9))
    vol_dub_db  = 20 * _math.log10(max(volume_dub, 1e-9))
    print(f"      Volume original: {volume_original} ({vol_orig_db:.1f} dB) | Dublagem: {volume_dub} ({vol_dub_db:.1f} dB)")

    # Detecta se o arquivo de entrada tem vídeo
    probe = subprocess.run(
        [FFMPEG, "-i", video_original],
        capture_output=True, text=True,
    )
    tem_video = "Stream #" in probe.stderr and "Video:" in probe.stderr

    ext = os.path.splitext(output_video)[1].lower()
    if ext == ".mp3":
        codec_audio = "libmp3lame"
    elif ext in (".m4a", ".mp4", ".mov", ".mkv"):
        codec_audio = "aac"
    else:
        codec_audio = "aac"

    cmd = [
        FFMPEG, "-y",
        "-i", video_original,
        "-i", tmp_dub,
        "-filter_complex",
            f"[0:a]volume={vol_orig_db:.2f}dB[orig];"
            f"[1:a]volume={vol_dub_db:.2f}dB[dub];"
            # normalize=0: os gains já foram aplicados acima — sem o amix rebaixar o nível (÷2)
            f"[orig][dub]amix=inputs=2:duration=first:normalize=0[aout]",
    ]

    if tem_video:
        cmd += ["-map", "0:v", "-map", "[aout]", "-c:v", "copy"]
    else:
        cmd += ["-map", "[aout]"]

    cmd += ["-c:a", codec_audio, "-b:a", "192k", output_video]

    subprocess.run(cmd, check=True)

    # 6. Gera log de trechos acelerados
    write_acceleration_log(accel_log_file, accelerated_segments)

    print(f"\n✅ Concluído! Vídeo salvo em: {output_video}")
    print(f"📝 Log salvo em: {accel_log_file}")


if __name__ == "__main__":
    main()

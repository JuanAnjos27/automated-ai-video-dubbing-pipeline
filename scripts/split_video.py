import argparse
import logging
import os
import re
import shutil
import subprocess
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Config:
    base_dir: Path
    input_dir: Path
    output_dir: Path
    chunk_seconds: int
    min_segment_seconds: float
    silence_threshold_db: int
    min_silence_len: float
    video_codec: str
    audio_codec: str


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def require_binaries() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Dependencias ausentes: {joined}. Instale e tente novamente.")


def null_sink() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def run_ffmpeg(cmd: List[str]) -> subprocess.CompletedProcess:
    logging.debug("Comando ffmpeg: %s", " ".join(cmd))
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def get_duration_seconds(video_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError("ffprobe falhou ao obter duracao")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe retornou duracao invalida") from exc


def verify_segment(output_path: Path) -> None:
    attempts = 3
    for attempt in range(attempts):
        if not output_path.exists():
            if attempt < attempts - 1:
                time.sleep(0.5)
                continue
            raise RuntimeError("Segmento nao foi criado")

        duration = get_duration_seconds(output_path)
        if duration > 0.1:
            return
        if attempt < attempts - 1:
            time.sleep(0.5)
            continue
        raise RuntimeError("Segmento corrompido ou vazio")


def build_split_points(
    silence_times: List[float],
    chunk_seconds: int,
    total_duration: float,
    min_segment_seconds: float,
) -> List[Tuple[float, Optional[float]]]:
    if not silence_times:
        points: List[Tuple[float, Optional[float]]] = []
        start = 0.0
        while start < total_duration:
            end = min(start + chunk_seconds, total_duration)
            points.append((start, end))
            start = end
        if points and (points[-1][1] - points[-1][0]) < min_segment_seconds and len(points) > 1:
            prev_start = points[-2][0]
            points[-2] = (prev_start, points[-1][1])
            points.pop()
        return points

    split_points = [0.0]
    for start in silence_times:
        if start - split_points[-1] >= chunk_seconds:
            split_points.append(start)
    split_points.append(total_duration)

    segments = [(split_points[i], split_points[i + 1]) for i in range(len(split_points) - 1)]
    if segments and (segments[-1][1] - segments[-1][0]) < min_segment_seconds and len(segments) > 1:
        merged = (segments[-2][0], segments[-1][1])
        segments[-2] = merged
        segments.pop()
    return segments


def try_split_with_codec(
    video_path: Path,
    output_path: Path,
    start: float,
    end: Optional[float],
    video_codec: str,
    audio_codec: str,
) -> subprocess.CompletedProcess:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-err_detect",
        "ignore_err",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(video_path),
    ]
    if end is not None:
        duration = max(0.0, end - start)
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-c:v",
        video_codec,
        "-c:a",
        audio_codec,
        "-movflags",
        "+faststart",
        "-y",
        str(output_path),
    ]
    return run_ffmpeg(cmd)


def split_video(video_path: Path, cfg: Config, manual_points: List[Tuple[float, float]]) -> None:
    output_dir = cfg.output_dir / video_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    logging.info("Usando pontos de divisão manuais para %s: %s", video_path, manual_points)

    for index, (start, end) in enumerate(manual_points, start=1):
        output_path = output_dir / f"{video_path.stem}_part{index:03d}.mp4"
        result = try_split_with_codec(
            video_path,
            output_path,
            start,
            end,
            cfg.video_codec,
            cfg.audio_codec,
        )
        if result.returncode != 0 and cfg.video_codec == "h264_nvenc":
            logging.warning("NVENC falhou, tentando libx264 para %s", output_path)
            result = try_split_with_codec(
                video_path,
                output_path,
                start,
                end,
                "libx264",
                cfg.audio_codec,
            )
        if result.returncode != 0:
            logging.error("Falha ao dividir %s", video_path)
            logging.error("stderr ffmpeg (split): %s", result.stderr)
            raise RuntimeError("ffmpeg falhou ao dividir o video")

        try:
            verify_segment(output_path)
        except RuntimeError as exc:
            logging.error("Segmento invalido %s: %s", output_path, exc)
            raise

    logging.info("Divisao concluida: %s", video_path)


def load_split_points(file_path: Path) -> dict:
    """Carrega os pontos de divisão de um arquivo JSON."""
    if not file_path.exists():
        raise FileNotFoundError(f"Arquivo de pontos de divisão não encontrado: {file_path}")
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_time_input(time_str: str) -> float:
    """Converte uma string de tempo no formato mm:ss ou ss para segundos."""
    match = re.match(r"^(?:(\d+):)?(\d+)$", time_str)
    if not match:
        raise ValueError(f"Formato de tempo inválido: {time_str}")
    minutes = int(match.group(1)) if match.group(1) else 0
    seconds = int(match.group(2))
    return minutes * 60 + seconds


def get_manual_split_points(video_files: List[Path]) -> dict:
    """Solicita os pontos de divisão manualmente no terminal para cada vídeo."""
    split_points = {}
    for video in video_files:
        print(f"Digite os pontos de corte para o vídeo '{video.name}' (formato: início,fim início,fim ...):")
        user_input = input("Pontos: ").strip()
        try:
            points = [
                tuple(
                    parse_time_input(segment) for segment in segment.split(",")
                )
                for segment in user_input.split()
            ]
            split_points[video.name] = points
        except ValueError as e:
            print(f"Erro: {e}. Tente novamente.")
            return get_manual_split_points(video_files)
    return split_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Divide videos por silencio ou por tempo fixo.")
    parser.add_argument("--input", default="downloads", help="Pasta de entrada com MP4s")
    parser.add_argument("--output", default="splits", help="Pasta de saida")
    parser.add_argument("--chunk-minutes", type=int, default=28, help="Minutos por parte")
    parser.add_argument("--min-segment-seconds", type=float, default=5.0, help="Duracao minima do segmento")
    parser.add_argument("--silence-db", type=int, default=-10, help="Threshold de silencio em dB")
    parser.add_argument("--silence-len", type=float, default=0.5, help="Duracao minima de silencio")
    parser.add_argument("--video-codec", default="h264_nvenc", help="Codec de video")
    parser.add_argument("--audio-codec", default="aac", help="Codec de audio")
    parser.add_argument("--verbose", action="store_true", help="Log detalhado")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    require_binaries()

    base_dir = Path(__file__).resolve().parent.parent  # canal-dublagem-jiang/
    input_dir = (base_dir / args.input).resolve()
    output_dir = (base_dir / args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        base_dir=base_dir,
        input_dir=input_dir,
        output_dir=output_dir,
        chunk_seconds=args.chunk_minutes * 60,
        min_segment_seconds=args.min_segment_seconds,
        silence_threshold_db=args.silence_db,
        min_silence_len=args.silence_len,
        video_codec=args.video_codec,
        audio_codec=args.audio_codec,
    )

    input_files = sorted(cfg.input_dir.glob("*.mp4"))
    if not input_files:
        logging.error("Nenhum MP4 encontrado em: %s", cfg.input_dir)
        return

    logging.info("Arquivos encontrados: %s", len(input_files))
    manual_split_points = get_manual_split_points(input_files)

    for video_path in input_files:
        split_video(video_path, cfg, manual_split_points[video_path.name])


if __name__ == "__main__":
    main()
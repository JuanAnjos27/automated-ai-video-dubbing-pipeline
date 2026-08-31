import os, subprocess
from pathlib import Path

CANAL_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = CANAL_DIR.parent

PASTA_ENTRADA = str(CANAL_DIR / "audios_blocos")
PASTA_SAIDA   = str(CANAL_DIR / "audios_blocos")
BITRATE       = "192k"

# ffmpeg: sistema → imageio-ffmpeg → fallback do venv
import shutil as _shutil

def _encontrar_ffmpeg():
    exe = _shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return str(BASE_DIR / ".venv311/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1")

FFMPEG = _encontrar_ffmpeg()

MP3_MAGIC = b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\x49\x44\x33"  # MP3 / ID3


def ja_e_mp3(caminho: str) -> bool:
    """Verifica se o arquivo já é MP3 pelos primeiros bytes (magic bytes)."""
    with open(caminho, "rb") as f:
        header = f.read(4)
    return any(header.startswith(m) for m in MP3_MAGIC)


# ── Conversão / renomeação ─────────────────────────────────────
os.makedirs(PASTA_SAIDA, exist_ok=True)

wavs = [f for f in os.listdir(PASTA_ENTRADA) if f.lower().endswith(".wav")]

if not wavs:
    print("Nenhum arquivo .wav encontrado em:", os.path.abspath(PASTA_ENTRADA))
else:
    renomeados = convertidos = 0
    for nome in sorted(wavs):
        caminho_wav = os.path.join(PASTA_ENTRADA, nome)
        nome_mp3    = os.path.splitext(nome)[0] + ".mp3"
        caminho_mp3 = os.path.join(PASTA_SAIDA, nome_mp3)

        if ja_e_mp3(caminho_wav):
            # Arquivo já é MP3 — só renomeia (sem re-codificar)
            os.rename(caminho_wav, caminho_mp3)
            print(f"Renomeado:  {nome} → {nome_mp3}")
            renomeados += 1
        else:
            # WAV de verdade — converte com ffmpeg
            print(f"Convertendo: {nome} → {nome_mp3}")
            subprocess.run(
                [FFMPEG, "-y", "-i", caminho_wav, "-b:a", BITRATE, caminho_mp3],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            convertidos += 1

    print(f"\nConcluído! {renomeados} renomeado(s), {convertidos} convertido(s).")

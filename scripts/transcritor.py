"""
Transcritor de áudio/vídeo com mlx-whisper
===========================================
Otimizado para Mac com Apple Silicon (M1/M2/M3/M4).
Gera arquivo .srt com timestamps em milissegundos.

INSTALAÇÃO:
    pip install mlx-whisper

USO:
    python transcrever.py video.mp4
    python transcrever.py audio.mp3
    python transcrever.py video.mp4 --idioma pt       # forçar idioma
    python transcrever.py video.mp4 --modelo large-v3 # modelo maior = mais preciso

MODELOS DISPONÍVEIS (do mais rápido ao mais preciso):
    tiny, base, small, medium, large-v2, large-v3 (padrão)

RESULTADO:
    Um arquivo .srt com o mesmo nome do arquivo de entrada.
    Ex: video.mp4 → video.srt
"""

import sys
import os
import re
import shutil
from pathlib import Path

# =============================================================================
# CONFIGURAÇÃO
# =============================================================================

MODELO_PADRAO = "mlx-community/whisper-large-v3-mlx"  # melhor qualidade no M4
IDIOMA_PADRAO = None  # None = detecção automática; use "pt", "en", etc.

# Mapa de atalhos de modelo para o nome completo no mlx-community
MODELOS = {
    "tiny":     "mlx-community/whisper-tiny-mlx",
    "base":     "mlx-community/whisper-base-mlx",
    "small":    "mlx-community/whisper-small-mlx",
    "medium":   "mlx-community/whisper-medium-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
}

# =============================================================================
# FUNÇÕES
# =============================================================================

def segundos_para_srt(segundos):
    """Converte segundos (float) para formato SRT: HH:MM:SS,mmm"""
    ms = int(round(segundos * 1000))
    h  = ms // 3_600_000
    ms %= 3_600_000
    m  = ms // 60_000
    ms %= 60_000
    s  = ms // 1_000
    ms %= 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def gerar_srt(segmentos, output_path):
    """Gera o arquivo .srt a partir dos segmentos do Whisper."""
    linhas = []
    for i, seg in enumerate(segmentos, start=1):
        inicio = segundos_para_srt(seg["start"])
        fim    = segundos_para_srt(seg["end"])
        texto  = seg["text"].strip()
        if texto:
            linhas.append(f"{i}\n{inicio} --> {fim}\n{texto}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas))

    return len(linhas)


def transcrever(arquivo, modelo_nome=MODELO_PADRAO, idioma=IDIOMA_PADRAO):
    """Transcreve o arquivo e retorna os segmentos."""
    try:
        import mlx_whisper
    except ImportError:
        print("ERRO: mlx-whisper não encontrado.")
        print("Instale com: pip install mlx-whisper")
        sys.exit(1)

    print(f"Modelo:  {modelo_nome}")
    print(f"Idioma:  {idioma or 'detecção automática'}")
    print(f"Arquivo: {arquivo}")
    print("\nTranscrevendo... (pode levar alguns minutos na primeira vez — baixando o modelo)\n")

    # mlx-whisper chama o binário "ffmpeg" diretamente.
    # Se não existir no sistema, tenta disponibilizar via imageio-ffmpeg.
    if not shutil.which("ffmpeg"):
        try:
            import imageio_ffmpeg

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            ffmpeg_link_dir = str(Path.home() / ".cache" / "automacoes_tools")
            os.makedirs(ffmpeg_link_dir, exist_ok=True)
            ffmpeg_link = os.path.join(ffmpeg_link_dir, "ffmpeg")

            if os.path.islink(ffmpeg_link) or os.path.exists(ffmpeg_link):
                try:
                    if os.path.islink(ffmpeg_link):
                        atual = os.readlink(ffmpeg_link)
                        if atual != ffmpeg_exe:
                            os.remove(ffmpeg_link)
                            os.symlink(ffmpeg_exe, ffmpeg_link)
                    elif os.path.isfile(ffmpeg_link):
                        os.remove(ffmpeg_link)
                        os.symlink(ffmpeg_exe, ffmpeg_link)
                except OSError:
                    pass
            else:
                os.symlink(ffmpeg_exe, ffmpeg_link)

            os.environ["PATH"] = ffmpeg_link_dir + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            print("ERRO: ffmpeg não encontrado no sistema.")
            print("Instale com: pip install imageio-ffmpeg")
            sys.exit(1)

    kwargs = {
        "path_or_hf_repo": modelo_nome,
        "verbose": False,
        "word_timestamps": False,
    }
    if idioma:
        kwargs["language"] = idioma

    resultado = mlx_whisper.transcribe(arquivo, **kwargs)

    idioma_detectado = resultado.get("language", "desconhecido")
    print(f"Idioma detectado: {idioma_detectado}")

    return resultado.get("segments", [])


# =============================================================================
# MODO INTERATIVO
# =============================================================================

from pathlib import Path as _Path
PASTA_AUTOMACOES = str(_Path(__file__).resolve().parent.parent.parent)  # raiz do projeto (pasta de mídias)
EXTENSOES_MIDIA  = {".mp4", ".mp3", ".wav", ".m4a", ".mov", ".mkv", ".aac", ".ogg", ".flac", ".webm"}


def escolher_interativo():
    """Pergunta arquivo, idioma e modelo interativamente no terminal."""

    # ── Arquivo ──────────────────────────────────────────────────────────────
    arquivos = sorted(
        f for f in os.listdir(PASTA_AUTOMACOES)
        if Path(os.path.join(PASTA_AUTOMACOES, f)).suffix.lower() in EXTENSOES_MIDIA
    )

    if not arquivos:
        print(f"Nenhum arquivo de mídia encontrado em {PASTA_AUTOMACOES}")
        sys.exit(1)

    print("\nArquivos de mídia disponíveis:")
    for i, nome in enumerate(arquivos, 1):
        print(f"  {i}. {nome}")
    print()

    while True:
        resp = input("Escolha o número do arquivo (ou digite o nome completo): ").strip()
        if resp.isdigit():
            idx = int(resp) - 1
            if 0 <= idx < len(arquivos):
                arquivo = os.path.join(PASTA_AUTOMACOES, arquivos[idx])
                break
            print(f"  Número inválido. Digite entre 1 e {len(arquivos)}.")
        elif resp:
            # Pode ser só o nome do arquivo ou caminho completo
            candidato = resp if os.path.isabs(resp) else os.path.join(PASTA_AUTOMACOES, resp)
            if os.path.exists(candidato):
                arquivo = candidato
                break
            print(f"  Arquivo não encontrado: {candidato}")
        else:
            print("  Digite um número ou nome de arquivo.")

    # ── Idioma ───────────────────────────────────────────────────────────────
    print()
    print("Idioma da transcrição:")
    print("  1. Detecção automática (padrão)")
    print("  2. Português (pt)")
    print("  3. Inglês (en)")
    print("  4. Outro (digitar código)")
    print()

    resp = input("Escolha (Enter = automático): ").strip()
    if resp == "1" or resp == "":
        idioma = None
    elif resp == "2":
        idioma = "pt"
    elif resp == "3":
        idioma = "en"
    elif resp == "4":
        idioma = input("  Código do idioma (ex: es, fr, de): ").strip() or None
    else:
        idioma = resp if resp else None  # aceita código direto também

    # ── Modelo ───────────────────────────────────────────────────────────────
    nomes_modelo = list(MODELOS.keys())
    print()
    print("Modelo Whisper (mais acima = mais rápido | mais abaixo = mais preciso):")
    for i, chave in enumerate(nomes_modelo, 1):
        padrao = "  ← padrão" if MODELOS[chave] == MODELO_PADRAO else ""
        print(f"  {i}. {chave}{padrao}")
    print()

    resp = input("Escolha o número do modelo (Enter = large-v3): ").strip()
    if resp.isdigit() and 1 <= int(resp) <= len(nomes_modelo):
        modelo = MODELOS[nomes_modelo[int(resp) - 1]]
    elif resp in MODELOS:
        modelo = MODELOS[resp]
    else:
        modelo = MODELO_PADRAO  # padrão se Enter ou inválido

    return arquivo, idioma, modelo


# =============================================================================
# EXECUÇÃO PRINCIPAL
# =============================================================================

def main():
    # Parse de argumentos via linha de comando (modo não-interativo)
    args = sys.argv[1:]

    arquivo = None
    modelo  = MODELO_PADRAO
    idioma  = IDIOMA_PADRAO

    if args:
        i = 0
        while i < len(args):
            if args[i] == "--idioma" and i + 1 < len(args):
                idioma = args[i + 1]
                i += 2
            elif args[i] == "--modelo" and i + 1 < len(args):
                chave = args[i + 1]
                modelo = MODELOS.get(chave, chave)
                i += 2
            else:
                arquivo = args[i]
                i += 1

    # Se não veio arquivo pela linha de comando, entra no modo interativo
    if not arquivo:
        arquivo, idioma, modelo = escolher_interativo()

    if not Path(arquivo).exists():
        print(f"ERRO: arquivo não encontrado: {arquivo}")
        sys.exit(1)

    # Define nome do arquivo de saída
    output_srt = str(Path(arquivo).with_suffix(".srt"))

    print("=" * 60)
    print("  Transcritor mlx-whisper — Apple Silicon")
    print("=" * 60 + "\n")

    segmentos = transcrever(arquivo, modelo_nome=modelo, idioma=idioma)

    if not segmentos:
        print("ERRO: nenhum segmento gerado. Verifique o arquivo de entrada.")
        sys.exit(1)

    total = gerar_srt(segmentos, output_srt)

    print(f"\n✅ Concluído!")
    print(f"   Blocos gerados: {total}")
    print(f"   Arquivo salvo:  {Path(output_srt).resolve()}")


if __name__ == "__main__":
    main()
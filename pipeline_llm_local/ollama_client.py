import json
import time
import urllib.request
from urllib.error import HTTPError
from typing import Dict


class OllamaError(RuntimeError):
    pass


class LLMClientError(RuntimeError):
    pass


def generate(
    prompt: str,
    model: str,
    ollama_url: str = "http://localhost:11434/api/generate",
    timeout: int = 180,
    options: Dict[str, int] | None = None,
    provider: str = "ollama",
    api_key: str | None = None,
) -> str:
    if provider == "deepseek":
        return _generate_deepseek(
            prompt=prompt,
            model=model,
            base_url=ollama_url,
            timeout=timeout,
            api_key=api_key,
        )

    return _generate_ollama(
        prompt=prompt,
        model=model,
        ollama_url=ollama_url,
        timeout=timeout,
        options=options,
    )


def _generate_ollama(
    prompt: str,
    model: str,
    ollama_url: str,
    timeout: int,
    options: Dict[str, int] | None = None,
) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if options:
        payload["options"] = options

    req = urllib.request.Request(
        url=ollama_url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = json.loads(res.read().decode("utf-8"))
    except Exception as exc:
        raise OllamaError(f"Falha ao chamar Ollama: {exc}") from exc

    text = body.get("response", "").strip()
    if not text:
        raise OllamaError("Ollama retornou resposta vazia")
    return text


def _generate_deepseek(
    prompt: str,
    model: str,
    base_url: str,
    timeout: int,
    api_key: str | None,
) -> str:
    if not api_key:
        raise LLMClientError(
            "DeepSeek API key ausente. Defina DEEPSEEK_API_KEY ou use --llm-api-key."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "stream": False,
    }

    last_error = None
    max_retries = 5  # aumentado pra cobrir respostas vazias

    for attempt in range(max_retries):
        req = urllib.request.Request(
            url=base_url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = json.loads(res.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise LLMClientError(f"Falha ao chamar DeepSeek API: {detail}") from exc
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                print(
                    f"[LLM] Erro de rede, retentando em {wait}s "
                    f"({attempt + 1}/{max_retries}): {exc}"
                )
                time.sleep(wait)
                continue
            raise LLMClientError(f"Falha ao chamar DeepSeek API: {exc}") from exc

        try:
            text = body["choices"][0]["message"]["content"].strip()
        except Exception:
            text = ""

        if not text:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 3
                print(f"[LLM] Resposta vazia, retentando em {wait}s ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            raise LLMClientError("DeepSeek retornou resposta vazia")
        break

    return text

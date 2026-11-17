"""Clientes HTTP para Groq y Google Gemini (niveles gratuitos)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from examforge.errors import (
    AuthenticationError,
    ConfigurationError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from examforge.models import AppSettings, ProviderName

logger = logging.getLogger(__name__)

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REQUEST_TIMEOUT = (15, 120)
MAX_ATTEMPTS = 3


class ChatProvider(Protocol):
    name: str
    model: str

    def complete_json(self, system_prompt: str, user_prompt: str) -> str: ...


@dataclass(slots=True)
class ProviderStats:
    calls: int = 0
    retries: int = 0
    total_latency_ms: int = 0


class BaseHTTPProvider:
    name = "base"
    model = ""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key or not api_key.strip():
            raise AuthenticationError(
                "Falta la API key. Añádela en Ajustes para usar el proveedor seleccionado."
            )
        self.api_key = api_key.strip()
        self.model = model.strip()
        if not self.model:
            raise ConfigurationError("El identificador del modelo no puede estar vacío.")
        self.session = requests.Session()
        self.stats = ProviderStats()

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError

    def _post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                response = self.session.post(
                    url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
                )
            except requests.Timeout as exc:
                last_error = ProviderTimeoutError(
                    "El proveedor tardó demasiado en responder. "
                    "Se reintentará con un bloque más pequeño si vuelve a ocurrir."
                )
                logger.warning("Timeout en intento %s/%s", attempt, MAX_ATTEMPTS)
                self._sleep(attempt)
                continue
            except requests.RequestException as exc:
                last_error = ProviderError(
                    "No se pudo conectar con el proveedor de IA. Revisa tu conexión."
                )
                logger.warning("Error de red en intento %s: %s", attempt, exc)
                self._sleep(attempt)
                continue
            finally:
                self.stats.total_latency_ms += int((time.monotonic() - started) * 1000)

            self.stats.calls += 1
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    raise ProviderError("El proveedor devolvió una respuesta ilegible.") from exc
                if not isinstance(data, dict):
                    raise ProviderError("El proveedor devolvió un formato inesperado.")
                return data

            body = response.text[:600]
            logger.warning("HTTP %s del proveedor: %s", response.status_code, body)
            if response.status_code in (401, 403):
                raise AuthenticationError(
                    "La API key no es válida o no tiene permisos para este modelo. "
                    "Verifícala en Ajustes."
                )
            if response.status_code == 429:
                retry_after = self._retry_after(response)
                if attempt < MAX_ATTEMPTS:
                    self.stats.retries += 1
                    self._sleep(attempt, minimum=retry_after)
                    last_error = RateLimitError(
                        "El nivel gratuito alcanzó su límite temporal. Espera un momento."
                    )
                    continue
                raise RateLimitError(
                    "Se agotó el límite gratuito del proveedor. Espera uno o dos minutos "
                    "o cambia de proveedor en Ajustes."
                )
            if response.status_code >= 500:
                last_error = ProviderError(
                    "El proveedor de IA está teniendo problemas temporales. "
                    "Vuelve a intentarlo en unos minutos."
                )
                self._sleep(attempt)
                continue
            message = _error_message(body)
            if response.status_code == 404 or "model" in body.casefold():
                raise ProviderError(
                    f"El modelo «{self.model}» no está disponible para esta cuenta. "
                    "Revisa el identificador en Ajustes."
                )
            raise ProviderError(message or f"Error del proveedor (HTTP {response.status_code}).")

        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError("No se pudo completar la petición al proveedor.")

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            return max(0.0, min(float(raw), 30.0))
        except ValueError:
            return 0.0

    @staticmethod
    def _sleep(attempt: int, minimum: float = 0.0) -> None:
        time.sleep(max(minimum, 1.5 * (2 ** (attempt - 1))))

    def _error_message(self, body: str) -> str:
        return ""


class GroqProvider(BaseHTTPProvider):
    name = "groq"

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        }
        data = self._post(
            GROQ_ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Groq devolvió una respuesta sin contenido.") from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Groq devolvió una respuesta vacía.")
        return content

    def _error_message(self, body: str) -> str:
        try:
            detail = json.loads(body)
            message = detail.get("error", {}).get("message", "")
            if "rate limit" in message.casefold():
                return "Se alcanzó el límite de peticiones de Groq. Espera un momento."
            return str(message)[:300]
        except json.JSONDecodeError:
            return ""


class GeminiProvider(BaseHTTPProvider):
    name = "gemini"

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }
        data = self._post(
            GEMINI_ENDPOINT.format(model=self.model),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ProviderError(
                "Gemini bloqueó el contenido por sus políticas de seguridad. "
                "Prueba con otro fragmento del temario."
            )
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError("Gemini no devolvió candidatos de respuesta.")
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason", "")
        if finish_reason == "MAX_TOKENS":
            raise ProviderError(
                "La respuesta del modelo superó el límite de tokens. "
                "Reduce el número de preguntas por lote."
            )
        if finish_reason in {"SAFETY", "RECITATION"}:
            raise ProviderError(
                "Gemini bloqueó la respuesta generada. Prueba con otro fragmento."
            )
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if not text.strip():
            raise ProviderError("Gemini devolvió una respuesta vacía.")
        return text


def build_provider(settings: AppSettings, api_key: str) -> BaseHTTPProvider:
    if settings.provider is ProviderName.GROQ:
        return GroqProvider(api_key, settings.groq_model)
    if settings.provider is ProviderName.GEMINI:
        return GeminiProvider(api_key, settings.gemini_model)
    raise ConfigurationError(
        "El modo demostración no usa proveedor externo; elige Groq o Gemini."
    )


def provider_for(settings: AppSettings, api_key: str) -> ChatProvider:
    return build_provider(settings, api_key)

"""Errores de dominio con mensajes aptos para la interfaz."""

from __future__ import annotations


class ExamForgeError(Exception):
    """Base de los errores controlados de la aplicación."""

    code = "internal_error"

    def __init__(self, message: str, *, details: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class DocumentError(ExamForgeError):
    """El archivo no se puede leer, está dañado o no tiene texto útil."""

    code = "document_error"


class UnsupportedDocumentError(DocumentError):
    code = "unsupported_document"


class EmptyDocumentError(DocumentError):
    code = "empty_document"


class ConfigurationError(ExamForgeError):
    code = "configuration_error"


class ProviderError(ExamForgeError):
    """Fallo genérico del proveedor de IA."""

    code = "provider_error"


class AuthenticationError(ProviderError):
    code = "authentication_error"


class RateLimitError(ProviderError):
    code = "rate_limit"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"


class ResponseFormatError(ProviderError):
    """La respuesta del modelo no respeta el contrato JSON."""

    code = "invalid_model_response"


class GenerationError(ExamForgeError):
    code = "generation_error"

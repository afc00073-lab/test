"""Puente entre PyWebView y el núcleo de ExamForge.

Todos los métodos públicos devuelven un sobre `{"ok": bool, "data"|"error"}`
para que la interfaz pueda mostrar errores de dominio sin perder el stack.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

import webview
from pydantic import ValidationError

from examforge import __version__
from examforge.ai.demo import generate_demo_exam
from examforge.ai.generator import generate_exam
from examforge.ai.providers import build_provider
from examforge.config import ConfigStore, public_settings
from examforge.errors import ConfigurationError, ExamForgeError, GenerationError
from examforge.export import safe_filename, write_markdown, write_pdf
from examforge.models import (
    AppSettings,
    DocumentSummary,
    ExamConfig,
    GeneratedExam,
    ProviderName,
    QuestionType,
)
from examforge.parser import extract_document, sanitize_document
from examforge.parser.chunking import plan_chunks
from examforge.parser.exclusions import ExcludedBlock, ExclusionFilter
from examforge.parser.extract import ExtractedDocument
from examforge.parser.exclusions import SanitizedDocument

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 30
MAX_BATCH = 20


def _ok(data: Any = None) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def api_method(method: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    """Captura errores y mantiene la firma para la introspección de PyWebView."""

    @functools.wraps(method)
    def wrapper(self: "ExamForgeApi", *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return _ok(method(self, *args, **kwargs))
        except ExamForgeError as exc:
            logger.info("%s: %s", exc.code, exc.message)
            return _fail(exc.code, exc.message)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            message = first.get("msg", "Datos no válidos.")
            logger.info("Validación rechazada: %s", message)
            return _fail("validation_error", str(message))
        except Exception:  # pragma: no cover - red de seguridad de la interfaz
            logger.exception("Error no controlado en %s", method.__name__)
            return _fail(
                "internal_error",
                "Se produjo un error interno. Revisa el registro de la aplicación.",
            )

    return wrapper


class _DocumentRecord:
    __slots__ = ("summary", "extracted", "sanitized")

    def __init__(
        self,
        summary: DocumentSummary,
        extracted: ExtractedDocument,
        sanitized: SanitizedDocument,
    ) -> None:
        self.summary = summary
        self.extracted = extracted
        self.sanitized = sanitized


class ExamForgeApi:
    def __init__(self, store: ConfigStore | None = None) -> None:
        self._store = store or ConfigStore()
        self._settings = self._store.load_settings()
        self._documents: dict[str, _DocumentRecord] = {}
        self._current_exam: GeneratedExam | None = None
        self._native_window: webview.Window | None = None
        self._lock = threading.RLock()
        self._generation_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    @api_method
    def bootstrap(self) -> dict[str, Any]:
        self._settings = self._store.load_settings()
        return {
            "settings": public_settings(self._store, self._settings, __version__),
            "version": __version__,
        }

    @api_method
    def pick_documents(self) -> dict[str, Any]:
        window = self._window()
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Documentos (*.pdf;*.md;*.markdown)", "Todos los archivos (*.*)"),
        )
        paths = self._normalize_dialog_paths(result)
        return {"paths": paths}

    @api_method
    def add_documents(self, paths: Sequence[str]) -> dict[str, Any]:
        if not isinstance(paths, (list, tuple)):
            raise ConfigurationError("La lista de archivos no es válida.")
        cleaned = [str(Path(path)) for path in paths if str(path).strip()][:MAX_BATCH]
        if not cleaned:
            return {"documents": [], "errors": []}

        errors: list[dict[str, str]] = []
        added: list[DocumentSummary] = []
        exclusion_filter = ExclusionFilter(self._settings.exclusion_keywords)

        with self._lock:
            existing_paths = {
                str(Path(record.extracted.path).expanduser().resolve())
                for record in self._documents.values()
            }
            for raw_path in cleaned:
                if len(self._documents) >= MAX_DOCUMENTS:
                    errors.append(
                        {
                            "name": Path(raw_path).name,
                            "message": f"Límite de {MAX_DOCUMENTS} documentos alcanzado.",
                        }
                    )
                    break
                resolved = str(Path(raw_path).expanduser().resolve())
                if resolved in existing_paths:
                    continue
                try:
                    extracted = extract_document(resolved)
                    sanitized = sanitize_document(extracted.text, exclusion_filter)
                    if len(sanitized.text) < 100:
                        raise ConfigurationError(
                            "Todo el contenido quedó excluido o es demasiado breve para evaluarlo."
                        )
                    summary = self._make_summary(extracted, sanitized)
                except ExamForgeError as exc:
                    errors.append({"name": Path(raw_path).name, "message": exc.message})
                    continue
                self._documents[summary.id] = _DocumentRecord(summary, extracted, sanitized)
                existing_paths.add(resolved)
                added.append(summary)

        return {
            "documents": [summary.model_dump(mode="json") for summary in added],
            "errors": errors,
        }

    @api_method
    def get_documents(self) -> dict[str, Any]:
        with self._lock:
            return {
                "documents": [
                    record.summary.model_dump(mode="json")
                    for record in self._documents.values()
                ]
            }

    @api_method
    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._record(document_id)
            content = record.sanitized.text[:24000]
            if len(record.sanitized.text) > 24000:
                content += "\n\n[Vista previa limitada a 24 000 caracteres]"
            return {"summary": record.summary.model_dump(mode="json"), "content": content}

    @api_method
    def remove_document(self, document_id: str) -> dict[str, Any]:
        with self._lock:
            if document_id not in self._documents:
                raise ConfigurationError("El documento ya no está en la sesión.")
            del self._documents[document_id]
        return {"removed": document_id}

    @api_method
    def clear_session(self) -> dict[str, Any]:
        with self._lock:
            self._documents.clear()
            self._current_exam = None
        return {"cleared": True}

    # ------------------------------------------------------------------
    # Coverage and generation
    # ------------------------------------------------------------------

    @api_method
    def preview_coverage(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        config = self._config_from_payload(payload)
        text = self._combined_text()
        if not text.strip():
            return {
                "chunks": [],
                "question_count": config.question_count,
                "valid_chars": 0,
            }
        try:
            plans = plan_chunks(
                text,
                total_questions=config.question_count,
                weights=config.weights,
                max_chars=config.chunk_chars,
            )
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        return {
            "chunks": [plan.public_dict() for plan in plans],
            "question_count": sum(plan.question_count for plan in plans),
            "valid_chars": len(text),
        }

    @api_method
    def generate(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        config = self._config_from_payload(payload)
        with self._lock:
            if not self._documents:
                raise GenerationError("Añade al menos un documento antes de generar.")
            text = self._combined_text()
            document_names = [record.summary.name for record in self._documents.values()]
        if len(text) < 200:
            raise GenerationError("El material válido es demasiado breve para un examen.")

        if not self._generation_lock.acquire(blocking=False):
            raise GenerationError("Ya hay una generación en curso.")
        try:
            self._emit_progress(0, 1, "Preparando el material")
            provider_name = self._settings.provider
            if provider_name is ProviderName.DEMO:
                exam = generate_demo_exam(
                    text=text,
                    document_names=document_names,
                    config=config,
                    progress=lambda current, total, message: self._emit_progress(
                        current, total, message
                    ),
                )
                model = "heuristic-v1"
            else:
                api_key = self._store.load_secret(provider_name)
                if not api_key:
                    raise ConfigurationError(
                        "Falta la API key del proveedor elegido. Añádela en Ajustes "
                        "o usa el modo demostración."
                    )
                provider = build_provider(self._settings, api_key)
                exam = generate_exam(
                    text=text,
                    document_names=document_names,
                    config=config,
                    provider=provider,
                    model=provider.model,
                    progress=lambda current, total, message: self._emit_progress(
                        current, total, message
                    ),
                )
                model = provider.model
            exam.provider = provider_name.value
            exam.model = model
            with self._lock:
                self._current_exam = exam
            return {"exam": exam.model_dump(mode="json")}
        finally:
            self._generation_lock.release()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    @api_method
    def export_exam(self, payload: dict[str, Any]) -> dict[str, Any]:
        exam_payload = payload.get("exam") if isinstance(payload, dict) else None
        if not isinstance(exam_payload, dict):
            raise ConfigurationError("No hay un examen que exportar.")
        try:
            exam = GeneratedExam.model_validate(exam_payload)
        except ValidationError as exc:
            raise ConfigurationError("El examen no tiene un formato válido.") from exc
        fmt = str(payload.get("format", "md")).lower()
        if fmt not in {"md", "pdf"}:
            raise ConfigurationError("Formato de exportación no soportado.")
        include_answers = bool(payload.get("include_answers", False))
        suffix = "md" if fmt == "md" else "pdf"
        default_name = safe_filename(exam, include_answers, suffix)
        target = self._ask_save_path(default_name, fmt)
        if not target:
            return {"cancelled": True}
        path = Path(target)
        if path.suffix.casefold() != "." + suffix:
            path = path.with_suffix("." + suffix)
        if fmt == "md":
            write_markdown(exam, path, include_answers)
        else:
            write_pdf(exam, path, include_answers)
        return {"path": str(path), "cancelled": False}

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @api_method
    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ConfigurationError("Los ajustes no tienen un formato válido.")
        allowed = set(AppSettings.model_fields)
        updates = {key: value for key, value in payload.items() if key in allowed}
        merged = {**self._settings.model_dump(mode="json"), **updates}
        try:
            settings = AppSettings.model_validate(merged)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            raise ConfigurationError(str(first.get("msg", "Ajustes no válidos."))) from exc
        if not settings.groq_model.strip() or not settings.gemini_model.strip():
            raise ConfigurationError("Los identificadores de modelo no pueden estar vacíos.")

        api_keys = payload.get("api_keys") or {}
        if isinstance(api_keys, dict):
            for name, value in api_keys.items():
                if name not in {"groq", "gemini"}:
                    continue
                if isinstance(value, str) and value.strip():
                    self._store.save_secret(ProviderName(name), value)
        clear_key = payload.get("clear_key")
        if clear_key in {"groq", "gemini"}:
            self._store.save_secret(ProviderName(clear_key), "")

        self._store.save_settings(settings)
        self._settings = settings
        return {
            "settings": public_settings(self._store, settings, __version__),
        }

    @api_method
    def test_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("provider", "")).lower()
        if name == ProviderName.DEMO.value:
            return {"message": "El modo demostración funciona sin conexión.", "model": "heuristic-v1"}
        if name not in {ProviderName.GROQ.value, ProviderName.GEMINI.value}:
            raise ConfigurationError("Proveedor no reconocido.")
        provider_name = ProviderName(name)
        api_key = str(payload.get("api_key", "")).strip() or self._store.load_secret(provider_name)
        if not api_key:
            raise ConfigurationError("Introduce una API key antes de probar la conexión.")
        settings = self._settings.model_copy(update={"provider": provider_name})
        provider = build_provider(settings, api_key)
        provider.complete_json(
            "Responde exclusivamente con JSON válido.",
            'Devuelve exactamente este objeto: {"ok": true}',
        )
        return {
            "message": f"Conexión correcta con {name}. Modelo {provider.model}.",
            "model": provider.model,
        }

    @api_method
    def open_data_dir(self) -> dict[str, Any]:
        path = self._store.base
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(path)])
        return {"path": str(path)}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _window(self) -> webview.Window:
        if self._native_window is not None:
            return self._native_window
        windows = webview.windows
        if not windows:
            raise ConfigurationError("La ventana de la aplicación no está disponible.")
        return windows[0]

    @staticmethod
    def _normalize_dialog_paths(result: Any) -> list[str]:
        if not result:
            return []
        if isinstance(result, (str, Path)):
            return [str(result)]
        return [str(item) for item in result if str(item).strip()]

    def _record(self, document_id: str) -> _DocumentRecord:
        record = self._documents.get(document_id)
        if record is None:
            raise ConfigurationError("El documento solicitado no está en la sesión.")
        return record

    def _make_summary(
        self, extracted: ExtractedDocument, sanitized: SanitizedDocument
    ) -> DocumentSummary:
        title_match = re.search(r"^#{1,6}\s+(.+)$", sanitized.text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else Path(extracted.name).stem
        return DocumentSummary(
            id=uuid4().hex[:12],
            name=extracted.name,
            suffix=extracted.suffix,
            pages=extracted.pages,
            words=extracted.words,
            tokens=extracted.tokens,
            chars=extracted.chars,
            size_bytes=extracted.size_bytes,
            kept_chars=len(sanitized.text),
            excluded_count=len(sanitized.excluded),
            excluded_chars=sanitized.excluded_chars,
            exclusions=self._serialize_exclusions(sanitized.excluded),
            warnings=list(extracted.warnings),
            preview=sanitized.text[:5000],
            title=title,
        )

    @staticmethod
    def _serialize_exclusions(blocks: Sequence[ExcludedBlock], limit: int = 12) -> list[dict[str, Any]]:
        return [
            {
                "reason": block.reason,
                "section": block.section,
                "preview": block.preview,
                "chars": block.chars,
                "line": block.line,
            }
            for block in list(blocks)[:limit]
        ]

    def _combined_text(self) -> str:
        with self._lock:
            parts: list[str] = []
            for record in self._documents.values():
                body = re.sub(
                    r"^(#{1,5})\s+",
                    r"#\1 ",
                    record.sanitized.text,
                    flags=re.MULTILINE,
                )
                parts.append(f"# Documento: {record.summary.name}\n\n{body}")
            return "\n\n".join(parts)

    def _config_from_payload(self, payload: dict[str, Any]) -> ExamConfig:
        weights = payload.get("weights")
        if not isinstance(weights, dict):
            weights = {QuestionType.MULTIPLE_CHOICE.value: 100}
        try:
            return ExamConfig(
                question_count=int(payload.get("question_count", 20)),
                weights=weights,
                difficulty=payload.get("difficulty", "university"),
                timer_minutes=payload.get("timer_minutes"),
                immediate_feedback=bool(payload.get("immediate_feedback", False)),
                chunk_chars=int(payload.get("chunk_chars", self._settings.chunk_chars)),
                exclusion_keywords=self._settings.exclusion_keywords,
                detect_exclusions=True,
            )
        except (ValidationError, ValueError) as exc:
            message = str(exc)
            if isinstance(exc, ValidationError) and exc.errors():
                message = str(exc.errors()[0].get("msg", message))
            raise ConfigurationError(message) from exc

    def _ask_save_path(self, default_name: str, fmt: str) -> str | None:
        window = self._window()
        file_types = (
            ("Markdown (*.md)",) if fmt == "md" else ("Documento PDF (*.pdf)",)
        )
        result = window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=default_name,
            file_types=file_types,
        )
        paths = self._normalize_dialog_paths(result)
        return paths[0] if paths else None

    def _emit_progress(self, current: int, total: int, message: str) -> None:
        if self._native_window is None:
            return
        payload = json.dumps([current, total, message], ensure_ascii=False)
        try:
            self._native_window.evaluate_js(
                f"window.ExamForge && window.ExamForge.onGenerationProgress(...{payload})"
            )
        except Exception:  # pragma: no cover - la ventana puede estar cerrándose
            logger.debug("No se pudo enviar el progreso a la interfaz.", exc_info=True)

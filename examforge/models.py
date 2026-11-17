"""Contratos de datos compartidos por parser, motor de IA, API y exportadores."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DEFAULT_EXCLUSION_KEYWORDS: tuple[str, ...] = (
    "no entra",
    "no entra en examen",
    "no entra en el examen",
    "materia excluida",
    "solo lectura",
    "sólo lectura",
    "no evaluable",
    "no forma parte del temario",
    "excluido del examen",
    "excluida del examen",
)


class QuestionType(str, Enum):
    MULTIPLE_CHOICE = "multiple_choice"
    TRUE_FALSE = "true_false"
    SHORT_ANSWER = "short_answer"

    @property
    def label(self) -> str:
        return {
            QuestionType.MULTIPLE_CHOICE: "Tipo test",
            QuestionType.TRUE_FALSE: "Verdadero / Falso",
            QuestionType.SHORT_ANSWER: "Respuesta corta",
        }[self]


class Difficulty(str, Enum):
    BASIC = "basic"
    UNIVERSITY = "university"
    COMPETITIVE = "competitive"

    @property
    def label(self) -> str:
        return {
            Difficulty.BASIC: "Básico",
            Difficulty.UNIVERSITY: "Universitario / técnico",
            Difficulty.COMPETITIVE: "Oposición / trampa",
        }[self]


class ProviderName(str, Enum):
    GROQ = "groq"
    GEMINI = "gemini"
    DEMO = "demo"


class Question(BaseModel):
    """Pregunta validada. La explicación siempre cita el material de origen."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = ""
    type: QuestionType
    prompt: str = Field(min_length=8)
    options: list[str] = Field(default_factory=list)
    correct_index: int | None = None
    correct_answer: str = Field(min_length=1)
    explanation: str = Field(min_length=8)
    source_quote: str = Field(min_length=8)
    source_document: str = ""
    topic: str = ""

    @field_validator("options")
    @classmethod
    def _clean_options(cls, value: list[str]) -> list[str]:
        cleaned = [option.strip() for option in value if option and option.strip()]
        if len({option.casefold() for option in cleaned}) != len(cleaned):
            raise ValueError("Las opciones deben ser distintas entre sí.")
        return cleaned

    @model_validator(mode="after")
    def _validate_shape(self) -> "Question":
        if self.type is QuestionType.MULTIPLE_CHOICE:
            if len(self.options) != 4:
                raise ValueError("Tipo test requiere exactamente cuatro opciones.")
            if self.correct_index is None or not 0 <= self.correct_index < len(self.options):
                raise ValueError("correct_index debe apuntar a una de las cuatro opciones.")
            expected = self.options[self.correct_index]
            if self.correct_answer.casefold() != expected.casefold():
                self.correct_answer = expected
        elif self.type is QuestionType.TRUE_FALSE:
            if len(self.options) != 2:
                self.options = ["Verdadero", "Falso"]
            if self.correct_index not in (0, 1):
                answer = self.correct_answer.casefold()
                if answer not in {"verdadero", "falso"}:
                    raise ValueError("Verdadero/Falso necesita una respuesta reconocible.")
                self.correct_index = 0 if answer == "verdadero" else 1
            self.correct_answer = self.options[self.correct_index]
        else:
            if self.options:
                self.options = []
            self.correct_index = None
        return self

    def public_dict(self) -> dict[str, Any]:
        """Copia segura para la interfaz; no oculta nada, pero fija el contrato."""
        return self.model_dump(mode="json")


class ExamConfig(BaseModel):
    """Parámetros elegidos por el usuario para una generación."""

    question_count: int = Field(default=20, ge=1, le=100)
    weights: dict[QuestionType, int] = Field(
        default_factory=lambda: {QuestionType.MULTIPLE_CHOICE: 100}
    )
    difficulty: Difficulty = Difficulty.UNIVERSITY
    timer_minutes: int | None = Field(default=None, ge=1, le=240)
    immediate_feedback: bool = False
    chunk_chars: int = Field(default=4500, ge=1200, le=12000)
    exclusion_keywords: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUSION_KEYWORDS)
    )
    detect_exclusions: bool = True

    @field_validator("weights", mode="before")
    @classmethod
    def _coerce_weights(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            str(key.value if isinstance(key, QuestionType) else key): int(amount)
            for key, amount in value.items()
        }

    @model_validator(mode="after")
    def _validate_weights(self) -> "ExamConfig":
        normalized = {question_type: 0 for question_type in QuestionType}
        for key, amount in self.weights.items():
            normalized[QuestionType(key)] = max(0, int(amount))
        if sum(normalized.values()) <= 0:
            normalized[QuestionType.MULTIPLE_CHOICE] = 100
        positive = {key: value for key, value in normalized.items() if value > 0}
        if self.question_count < len(positive):
            raise ValueError(
                "El número de preguntas es menor que los formatos activos. "
                "Desactiva algún formato o genera más preguntas."
            )
        self.weights = normalized
        self.exclusion_keywords = [
            keyword.strip() for keyword in self.exclusion_keywords if keyword.strip()
        ]
        return self


class CoverageEntry(BaseModel):
    index: int
    sections: list[str]
    question_count: int
    chars: int
    truncated: bool = False


class GeneratedExam(BaseModel):
    id: str
    title: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    config: ExamConfig
    questions: list[Question]
    warnings: list[str] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    provider: str = "demo"
    model: str = ""

    @field_validator("questions")
    @classmethod
    def _questions_not_empty(cls, value: list[Question]) -> list[Question]:
        if not value:
            raise ValueError("El examen debe contener al menos una pregunta.")
        return value


class DocumentSummary(BaseModel):
    """Vista serializable de un documento cargado en la sesión."""

    id: str
    name: str
    suffix: str
    pages: int | None = None
    words: int
    tokens: int
    chars: int
    size_bytes: int
    kept_chars: int
    excluded_count: int
    excluded_chars: int
    exclusions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    preview: str = ""
    title: str = ""


class AppSettings(BaseModel):
    """Preferencias persistidas; las claves nunca viven en este modelo."""

    provider: ProviderName = ProviderName.GROQ
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_model: str = "gemini-2.5-flash"
    theme: str = Field(default="system", pattern="^(light|dark|system)$")
    chunk_chars: int = Field(default=4500, ge=1200, le=12000)
    default_question_count: int = Field(default=20, ge=1, le=100)
    default_timer_minutes: int = Field(default=30, ge=1, le=240)
    immediate_feedback: bool = False
    exclusion_keywords: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUSION_KEYWORDS)
    )

    @field_validator("theme")
    @classmethod
    def _known_theme(cls, value: str) -> str:
        if value not in {"light", "dark", "system"}:
            raise ValueError("Tema no soportado.")
        return value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

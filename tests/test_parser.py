from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError

from examforge.errors import DocumentError
from examforge.models import Question, QuestionType
from examforge.parser.chunking import expand_question_types, plan_chunks
from examforge.parser.exclusions import ExclusionFilter, sanitize_document
from examforge.parser.extract import extract_document


SYLLABUS = """# Tema 1. Introducción

La célula es la unidad estructural de los seres vivos.

## 1.1 Membrana plasmática

La membrana regula el intercambio de sustancias.

## 1.2 Núcleo (no entra en examen)

El núcleo contiene el material genético y NO debe evaluarse.

# Tema 2. Metabolismo

El ATP es la moneda energética de la célula.

### Nota: solo lectura

Este anexo es de ampliación y no se evalúa.

# Tema 3. Genética

El ADN almacena la información hereditaria.
"""


def test_exclusion_removes_flagged_section_and_paragraph() -> None:
    cleaned = sanitize_document(SYLLABUS)
    assert "núcleo contiene" not in cleaned.text.casefold()
    assert "anexo de ampliación" not in cleaned.text.casefold()
    assert "ATP es la moneda" in cleaned.text
    assert "ADN almacena" in cleaned.text
    assert cleaned.excluded
    reasons = {block.reason.casefold() for block in cleaned.excluded}
    assert "no entra en examen" in reasons
    assert "solo lectura" in reasons
    assert cleaned.excluded_chars > 0


def test_exclusion_resumes_at_same_heading_level() -> None:
    text = (
        "## Tema 1\nContenido válido A.\n"
        "## Tema 2 (materia excluida)\nContenido descartado.\n"
        "### Subapartado\nTambién descartado.\n"
        "## Tema 3\nContenido válido B.\n"
    )
    cleaned = sanitize_document(text)
    assert "Contenido válido A" in cleaned.text
    assert "Contenido válido B" in cleaned.text
    assert "descartado" not in cleaned.text


def test_filter_is_accent_insensitive_and_configurable() -> None:
    cleaned = sanitize_document("Tema 1. Álgebra\nEsto sí.\nTema 2. SOLO LECTURA\nEsto no.\n", ExclusionFilter(["sólo lectura"]))
    assert "Esto sí" in cleaned.text
    assert "Esto no" not in cleaned.text

    custom = sanitize_document("A\nTexto irrelevante.\nB", ExclusionFilter(["texto irrelevante"]))
    assert "irrelevante" not in custom.text


def test_markdown_extraction(tmp_path: Path) -> None:
    source = tmp_path / "apuntes.md"
    source.write_text("---\ntitle: Prueba\n---\n# Tema\n\nTexto **útil**.", encoding="utf-8")
    document = extract_document(source)
    assert document.pages is None
    assert "Texto útil" in document.text
    assert document.words >= 3
    assert document.tokens > 0


def test_pdf_extraction_and_empty_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "tema.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "La mitocondria produce ATP en la respiracion celular.")
    pdf.save(pdf_path)
    pdf.close()

    document = extract_document(pdf_path)
    assert document.pages == 1
    assert "mitocondria" in document.text

    empty_pdf = pymupdf.open()
    empty_pdf.new_page()
    empty_path = tmp_path / "vacio.pdf"
    empty_pdf.save(empty_path)
    empty_pdf.close()
    with pytest.raises(DocumentError):
        extract_document(empty_path)


def test_chunk_plan_covers_all_sections_and_allocates_counts() -> None:
    sections = "\n\n".join(
        f"# Sección {index}\n" + ("Contenido relevante. " * 80) for index in range(1, 7)
    )
    plans = plan_chunks(sections, total_questions=25, weights={QuestionType.MULTIPLE_CHOICE: 100})
    assert len(plans) >= 6
    assert sum(plan.question_count for plan in plans) == 25
    covered = {section for plan in plans for section in plan.sections}
    assert len(covered) == 6
    assert all(plan.question_types for plan in plans)


def test_chunk_plan_collapses_when_questions_are_scarce() -> None:
    text = "\n\n".join(f"# Parte {index}\n" + ("Texto de la parte. " * 60) for index in range(1, 11))
    plans = plan_chunks(text, total_questions=3, weights={QuestionType.TRUE_FALSE: 100})
    assert len(plans) == 3
    assert all(plan.question_count == 1 for plan in plans)
    combined = "\n".join(plan.text for plan in plans)
    assert "Parte 1" in combined
    assert "Parte 10" in combined
    assert all(plan.question_types == [QuestionType.TRUE_FALSE] for plan in plans)


def test_type_expansion_keeps_total_and_ratio() -> None:
    types = expand_question_types(
        10,
        {
            QuestionType.MULTIPLE_CHOICE: 60,
            QuestionType.TRUE_FALSE: 30,
            QuestionType.SHORT_ANSWER: 10,
        },
    )
    assert len(types) == 10
    assert types.count(QuestionType.MULTIPLE_CHOICE) == 6
    assert types.count(QuestionType.TRUE_FALSE) == 3
    assert types.count(QuestionType.SHORT_ANSWER) == 1


def test_multiple_choice_question_requires_four_options() -> None:
    with pytest.raises(ValidationError):
        Question(
            type=QuestionType.MULTIPLE_CHOICE,
            prompt="¿Qué orgánulo produce ATP?",
            options=["Núcleo", "Ribosoma", "Mitocondria"],
            correct_index=2,
            correct_answer="Mitocondria",
            explanation="La mitocondria realiza la fosforilación oxidativa.",
            source_quote="La mitocondria produce ATP en la respiración celular.",
        )

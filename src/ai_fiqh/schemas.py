"""Pydantic models for revision mode (docs/research.md §2.3).

Two families:

* **Output** — `MCQ` and `Flashcard`, what a caller consumes.
* **Generation** — `PhrasedOptions` and `GeneratedFlashcard`, the narrow shapes
  Claude is allowed to return.

The split is the point. Claude never returns an `MCQ`: it returns *phrasings* of
items this code already selected, and the code assembles the question around
them. Which item is correct, which are wrong, and where they came from are all
decided before the model is called, so the accidentally-correct-distractor
failure mode is structurally impossible rather than checked for afterwards.

Revision mode uses `output_config.format` and therefore cannot use API citations
(§1.7 — the two return a 400 together). It trades citation spans for a
machine-parseable shape, which is the right way round here: the provenance is
already known from the chunk the item was drawn from.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    """One enumerated ruling lifted verbatim out of a chunk, before phrasing."""

    index: int
    text: str
    chunk_id: str
    category: str
    kitab: str
    bab: str
    page_start: int
    page_end: int


# --- what Claude is allowed to return ---------------------------------------


class MCQStem(BaseModel):
    """Claude's whole job for an MCQ: a stem and an explanation. Not the options.

    An earlier version had Claude *phrase* each option from its source item, with
    a content-word overlap check to catch drift. Measurement killed that design:
    on eight generations, known-good options scored a median 57% overlap, because
    faithful rewording ("stand straight" -> "standing upright") is
    indistinguishable from fabrication by word overlap alone. Any threshold that
    caught invention also rejected most correct output.

    So the options are now lifted verbatim from the book and never pass through
    the model. Nothing needs checking, and a student revising sees the source's
    own wording -- which is what they will meet in the exam.
    """

    question: str = Field(description="The question stem")
    explanation: str = Field(description="Why the correct option is correct, per the book")


class GeneratedFlashcard(BaseModel):
    front: str = Field(description="The prompt side — a question or a term")
    back: str = Field(description="The ruling, as the book gives it")


class GeneratedFlashcards(BaseModel):
    cards: list[GeneratedFlashcard]


# --- what callers consume ----------------------------------------------------


class MCQ(BaseModel):
    question: str
    options: list[str]
    correct_index: int
    explanation: str
    source_page: int
    kitab: str
    bab: str
    category: str  # the category the CORRECT answer came from
    distractor_categories: list[str] = Field(default_factory=list)

    def render(self) -> str:
        lines = [self.question]
        for i, opt in enumerate(self.options):
            lines.append(f"  {'ABCD'[i]}) {opt}")
        lines.append(f"  -> {'ABCD'[self.correct_index]}  (p{self.source_page}, "
                     f"{self.bab} / {self.category})")
        lines.append(f"     {self.explanation}")
        return "\n".join(lines)


class Flashcard(BaseModel):
    front: str
    back: str
    source_page: int
    kitab: str
    bab: str

    def render(self) -> str:
        return f"Q: {self.front}\nA: {self.back}\n   (p{self.source_page}, {self.bab})"


class ValidationFailure(BaseModel):
    """Why a generated item was discarded. Counted, not shown to the user."""

    check: str
    detail: str

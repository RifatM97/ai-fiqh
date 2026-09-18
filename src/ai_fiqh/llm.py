"""Model provider — Azure OpenAI by default, Ollama or Anthropic on request.

The project was built against Anthropic and leaned on two things only Anthropic
offers: API-native citations on document blocks (§1.7 layer 1) and `output_config
.effort`. Moving off it means those have to be replaced rather than ported, so
this module draws the line at what every provider can actually do:

    complete(system, user)          -> text
    parse(system, user, schema)     -> a validated pydantic model

Citations move out of the API and into `qa.py`, where excerpts are numbered and
the model cites `[n]`. That is strictly weaker than a citation object — the model
can still write `[9]` when only five excerpts exist — but it is *checkable*, and
the check is code. See `qa.resolve_markers`, which is the §1.7 layer-1 rewrite.

Provider is chosen by `AI_FIQH_LLM_PROVIDER` (azure | ollama | anthropic). The
Anthropic path keeps `cited_complete`, so the native-citation implementation
stays runnable for comparison rather than surviving only in git history.

Context is a provider property, not a global constant: Azure deployments carry
128k or more, gemma2 carries 8,192 and Ollama silently truncates to 2,048 unless
told otherwise. `context_tokens` is what `qa.py` budgets against.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PROVIDER = "azure"

# Optional second provider, used *only* when the primary's content moderation
# refuses. Empty disables the mechanism, so nothing changes unless opted in.
#
# Why this exists: the corpus is a manual of ritual purity. It covers ghusl after
# intercourse, menstruation, istihadah, madhi and wadi -- in the clinical register
# a 17th-century jurist would use. Azure's filter scores
# `019-things-which-do-not-necessitate-ghusl` (p19) as `sexual: medium` and
# rejects the whole prompt, including after a custom filter was configured
# (2026-09-10). That is a structural mismatch between classical fiqh vocabulary
# and commercial content moderation, not a threshold waiting to be tuned, and it
# made the system report that the book does not cover rulings printed in it.
FALLBACK_PROVIDER_VAR = "AI_FIQH_LLM_FALLBACK_PROVIDER"

# Azure. Deployment names are chosen by whoever created the resource, so nothing
# about the underlying model can be inferred from them -- see `_AzureClient` for
# how the parameter differences are discovered instead of guessed.
AZURE_API_VERSION = "2024-10-21"  # first GA version with strict structured outputs
AZURE_CONTEXT_TOKENS = 128_000

# Ollama. `num_ctx` is the load-bearing one: the server default is 2,048, so a
# retrieval context that fits the model's window is still truncated without this
# -- silently, with no error and no warning in the response.
OLLAMA_MODEL = "gemma4:12b"

# **Not** the model's maximum, and sized against measurement rather than taste.
# gemma4:12b advertises 262,144 tokens; 32,768 was tried first and pushed an 18GB
# M3 Pro to 9.6GB of swap with the model resident, which made a single question
# take minutes.
#
# What the workload actually needs: the largest excerpt set this corpus can
# produce is the §2.2 enumeration path merging the Hajj rituals section, ~9,500
# tokens. Against 16,384 the budget in `qa.fit_to_context` works out at roughly
# 11,000 tokens for excerpts once the authority prompt, the answer ceiling and the
# thinking reserve are subtracted -- so nothing is trimmed, with margin, at half
# the KV cache. Raise it only with a measurement.
OLLAMA_CONTEXT_TOKENS = 16_384

# gemma4 is a reasoning model: it returns `thinking` alongside `content`, and
# `num_predict` caps the two together. A ceiling sized for the answer alone gets
# spent entirely on reasoning and returns empty content with
# `done_reason="length"` -- which is how this was found, at 14,668 characters
# (~4,200 tokens) of reasoning on a single wudu question.
#
# Thinking is **off by default**, which is a budget decision rather than a view
# about reasoning. Two constraints make it expensive here:
#
#   * It competes with the excerpts for the same window. A reserve large enough
#     for ~4,200 tokens of reasoning takes roughly 3,000 tokens off the excerpt
#     budget at a 16K context, which is enough to start trimming the §2.2
#     enumeration path -- paying for the model to think by giving it less of the
#     book to think about is the wrong trade.
#   * On a memory-constrained machine it multiplies latency, and the Ollama path
#     exists mainly as the content-filter fallback, where the alternative is no
#     answer at all.
#
# Set OLLAMA_THINK=1 to turn it back on; the reserve below is then applied and
# OLLAMA_CONTEXT_TOKENS should be raised to match if the machine has the headroom.
OLLAMA_THINK = False
OLLAMA_THINKING_RESERVE = 6_144

ANTHROPIC_MODEL = "claude-opus-5"
ANTHROPIC_CONTEXT_TOKENS = 200_000
ANTHROPIC_EFFORT = "high"
# Anthropic counts thinking tokens against `max_tokens`, so a caller asking for
# 2,000 tokens of answer would get whatever thinking left over -- often nothing.
# Callers add `thinking_reserve` to their own ceiling; it is 0 everywhere else,
# which keeps the arithmetic identical across providers instead of branching.
ANTHROPIC_THINKING_RESERVE = 14_000

# ~3.6 chars/token measured on this corpus, rounded down to 3.5 so the estimate
# errs high. It only ever gates how much context is sent, and over-estimating
# costs a dropped chunk while under-estimating costs a truncated prompt.
CHARS_PER_TOKEN = 3.5


class LLMError(RuntimeError):
    """Any provider failure a caller might reasonably show a user."""


class LLMConfigError(LLMError):
    """Missing or malformed configuration — a key, an endpoint, a deployment."""


class LLMUnavailable(LLMError):
    """The provider could not be reached, or the model is not installed."""


class LLMOverloaded(LLMError):
    """Rate limited or transiently overloaded. Retrying later is reasonable."""


class LLMContentFiltered(LLMError):
    """A provider content filter rejected the prompt or the response.

    Not a bug and not a transient failure -- retrying sends the same bytes and
    gets the same answer. It matters here because the corpus is a fiqh manual:
    the Book of Purity covers ghusl after intercourse, menstruation and
    istihadah in clinical detail, and Azure's default filter scores some of
    those passages `sexual: medium` and refuses the request. The passages are
    the *source text of the book*, so there is nothing to rephrase; the caller
    turns this into an abstention rather than a crash.
    """


def load_env() -> None:
    """Load `.env` from the repo root. Safe to call repeatedly."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def fit_text(text: str, max_tokens: int) -> str:
    """Truncate to a token budget on a paragraph boundary where one is near.

    Used for whole-section passages, which run to 5,600 tokens in the Hajj
    rituals chapter and would otherwise overflow a small context window. Cutting
    at a blank line rather than mid-sentence keeps the tail of the passage
    readable to the model instead of ending it on half a ruling.
    """
    limit = int(max_tokens * CHARS_PER_TOKEN)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n\n")
    return cut[: boundary if boundary > limit * 0.6 else len(cut)].rstrip()


@dataclass
class Completion:
    text: str
    stop_reason: str | None = None
    refused: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    # Set by `FallbackClient` when a content filter on the primary provider sent
    # the request elsewhere. `qa.Answer` carries it through to the UI -- a reader
    # is entitled to know a different model answered.
    answered_by: str | None = None
    fallback_used: bool = False


class LLMClient(Protocol):
    provider: str
    model: str
    context_tokens: int
    supports_native_citations: bool
    default_workers: int
    thinking_reserve: int

    def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion: ...

    def parse[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T: ...


# --- Azure OpenAI -------------------------------------------------------------


class AzureClient:
    """Azure OpenAI via the `openai` SDK.

    Two parameter differences between chat and reasoning deployments cannot be
    read off a deployment name, because the name is arbitrary: reasoning models
    take `max_completion_tokens` rather than `max_tokens` and reject an explicit
    `temperature`. Rather than maintaining a model table that goes stale, the
    first call discovers both from the 400 and remembers the answer.
    """

    provider = "azure"
    supports_native_citations = False
    default_workers = 4
    thinking_reserve = 0

    def __init__(self, *, deployment: str | None = None) -> None:
        load_env()
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        self.model = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", endpoint),
                ("AZURE_OPENAI_API_KEY", api_key),
                ("AZURE_OPENAI_DEPLOYMENT", self.model),
            )
            if not value
        ]
        if missing:
            raise LLMConfigError(
                f"{', '.join(missing)} not set (looked in env and .env). "
                "See README 'Setup' for the four Azure values."
            )
        self.context_tokens = int(
            os.environ.get("AZURE_OPENAI_CONTEXT_TOKENS", AZURE_CONTEXT_TOKENS)
        )
        self._token_param = "max_tokens"
        self._send_temperature = True
        self._dialect_lock = threading.Lock()

        from openai import AzureOpenAI

        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=os.environ.get(
                "AZURE_OPENAI_API_VERSION", AZURE_API_VERSION
            ),
            max_retries=4,
        )

    # -- request plumbing ----------------------------------------------------

    def _kwargs(self, system: str, user: str, max_tokens: int, temperature: float):
        kw: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            self._token_param: max_tokens,
        }
        if self._send_temperature:
            kw["temperature"] = temperature
        return kw

    def _adapt(self, exc: Exception) -> bool:
        """Learn a deployment's parameter dialect from its rejection. True = retry.

        Thread-safety is the whole subtlety here, and getting it wrong cost three
        questions of an eval run on 2026-09-04. With several workers sharing one
        client, two threads hit the same 400 at once; the first corrects the
        dialect, and the second then found nothing left to change and reported a
        hard failure -- even though its request would now succeed verbatim.

        So the answer is "retry" whenever the rejection names a parameter this
        client knows how to correct, whether or not *this* thread is the one that
        corrected it. `_send` bounds the retries, so a genuinely unfixable
        rejection still terminates.
        """
        message = str(exc).lower()
        with self._dialect_lock:
            if "max_completion_tokens" in message:
                self._token_param = "max_completion_tokens"
                return True
            if "temperature" in message:
                self._send_temperature = False
                return True
        return False

    def _send(self, call, system: str, user: str, max_tokens: int, temperature: float,
              **extra):
        import openai

        for _ in range(3):  # at most one correction per dialect difference
            try:
                return call(**self._kwargs(system, user, max_tokens, temperature),
                            **extra)
            except openai.BadRequestError as exc:
                if _is_content_filter(exc):
                    raise LLMContentFiltered(
                        f"Azure content filter rejected the request "
                        f"({_filter_categories(exc)})."
                    ) from exc
                if not self._adapt(exc):
                    raise LLMError(f"Azure rejected the request: {exc}") from exc
            except openai.RateLimitError as exc:
                raise LLMOverloaded("Azure OpenAI rate limit reached.") from exc
            except openai.APIConnectionError as exc:
                raise LLMUnavailable(
                    "Could not reach the Azure OpenAI endpoint."
                ) from exc
            except openai.APIStatusError as exc:
                if exc.status_code in (429, 500, 502, 503, 504):
                    raise LLMOverloaded(
                        f"Azure OpenAI is unavailable (HTTP {exc.status_code})."
                    ) from exc
                raise LLMError(f"Azure OpenAI error {exc.status_code}.") from exc
        raise LLMError("Azure rejected the request after parameter adaptation.")

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        u = getattr(response, "usage", None)
        if u is None:
            return {}
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0)
        return {
            "input_tokens": u.prompt_tokens,
            "output_tokens": u.completion_tokens,
            "cache_read_input_tokens": cached or 0,
        }

    # -- interface -----------------------------------------------------------

    def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        try:
            response = self._send(
                self._client.chat.completions.create, system, user, max_tokens,
                temperature,
            )
        except LLMContentFiltered as exc:
            # Indistinguishable, from the pipeline's point of view, from a model
            # declining to answer -- so it takes the same path (§1.7 refusal)
            # instead of ending the run.
            return Completion(text="", stop_reason="content_filter", refused=True,
                              usage={"content_filter": str(exc)})
        choice = response.choices[0]
        message = choice.message
        # A content filter or a model refusal both mean "no answer was produced",
        # which qa.py turns into an abstention rather than an empty answer.
        refusal = getattr(message, "refusal", None)
        return Completion(
            text=(message.content or "").strip(),
            stop_reason=choice.finish_reason,
            refused=bool(refusal) or choice.finish_reason == "content_filter",
            usage=self._usage(response),
        )

    def parse[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        response = self._send(
            self._client.chat.completions.parse,
            system,
            user,
            max_tokens,
            0.0,
            response_format=schema,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise LLMError(
                f"Azure returned no parsable {schema.__name__} "
                f"(finish_reason={response.choices[0].finish_reason})."
            )
        return parsed


def _is_content_filter(exc: Exception) -> bool:
    body = getattr(exc, "body", None) or {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    if err.get("code") == "content_filter":
        return True
    return "content_filter" in str(exc) or "ResponsibleAIPolicy" in str(exc)


def _filter_categories(exc: Exception) -> str:
    """Which categories fired, for a message a human can act on."""
    body = getattr(exc, "body", None) or {}
    try:
        results = body["error"]["innererror"]["content_filter_result"]
        hit = [k for k, v in results.items()
               if isinstance(v, dict) and (v.get("filtered") or v.get("detected"))]
        return ", ".join(hit) if hit else "category not reported"
    except Exception:
        return "category not reported"


# --- Ollama -------------------------------------------------------------------


class OllamaClient:
    """A local model served by Ollama.

    Structured output is a grammar constraint here rather than a trained
    capability: Ollama's `format` takes a JSON schema and decodes against it, so
    even a small model returns parsable JSON. It constrains *shape*, not
    *quality* -- a 9B model still writes a worse MCQ stem, it just writes it
    into the right fields.

    Generation runs single-threaded on purpose. Ollama serialises requests to one
    loaded model anyway, and fanning out four workers at a 9B model on a laptop
    trades throughput for memory pressure.
    """

    provider = "ollama"
    supports_native_citations = False
    default_workers = 1

    def __init__(self, *, model: str | None = None) -> None:
        load_env()
        self.model = model or os.environ.get("OLLAMA_MODEL", OLLAMA_MODEL)
        self.context_tokens = int(
            os.environ.get("OLLAMA_CONTEXT_TOKENS", OLLAMA_CONTEXT_TOKENS)
        )
        self.think = (
            os.environ.get("OLLAMA_THINK", "1" if OLLAMA_THINK else "0")
            .strip().lower() in ("1", "true", "yes", "on")
        )
        # Nothing to reserve when the model is not going to think.
        self.thinking_reserve = (
            int(os.environ.get("OLLAMA_THINKING_RESERVE", OLLAMA_THINKING_RESERVE))
            if self.think
            else 0
        )
        self._send_think = True  # cleared if the server rejects the parameter
        try:
            import ollama
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise LLMConfigError("the `ollama` package is not installed.") from exc
        self._ollama = ollama
        self._client = ollama.Client(host=os.environ.get("OLLAMA_HOST") or None)

    def _chat(self, system: str, user: str, max_tokens: int, temperature: float,
              fmt: Any = None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": fmt,
            "options": {
                "num_ctx": self.context_tokens,
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        # Older servers and non-reasoning models reject `think` outright, so it is
        # dropped on the first rejection rather than version-sniffed.
        if self._send_think:
            kwargs["think"] = self.think
        try:
            return self._client.chat(**kwargs)
        except TypeError:
            self._send_think = False
            kwargs.pop("think", None)
            return self._client.chat(**kwargs)
        except self._ollama.ResponseError as exc:
            if "think" in str(exc).lower() and self._send_think:
                self._send_think = False
                kwargs.pop("think", None)
                return self._client.chat(**kwargs)
            if exc.status_code == 404:
                raise LLMUnavailable(
                    f"model {self.model!r} is not pulled — run "
                    f"`ollama pull {self.model}`."
                ) from exc
            raise LLMError(f"Ollama error: {exc}") from exc
        except (ConnectionError, self._ollama.RequestError) as exc:
            raise LLMUnavailable(
                "Could not reach Ollama — is `ollama serve` running?"
            ) from exc

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        return {
            "input_tokens": response.get("prompt_eval_count", 0),
            "output_tokens": response.get("eval_count", 0),
            "cache_read_input_tokens": 0,
        }

    def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        response = self._chat(system, user, max_tokens, temperature)
        message = response["message"]
        text = (message.get("content") or "").strip()
        if not text and response.get("done_reason") == "length":
            # A reasoning model that spent the whole ceiling on `thinking`. Silent
            # empty answers are worse than a loud failure here, because the
            # pipeline would read one as "the model had nothing to say".
            thought = len(message.get("thinking") or "")
            raise LLMError(
                f"{self.model} produced no answer: the {max_tokens}-token ceiling "
                f"was consumed by reasoning ({thought} chars of it). Raise "
                f"OLLAMA_THINKING_RESERVE."
            )
        return Completion(
            text=text,
            stop_reason=response.get("done_reason"),
            refused=False,  # local models have no content filter and no refusal signal
            usage=self._usage(response),
        )

    def parse[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        import json

        schema_json = schema.model_json_schema()
        last: Exception | None = None
        # Decoding is grammar-constrained, so a shape failure is rare; when it
        # happens it is a small model emitting an empty or half-filled object,
        # and one retry at a nudged temperature is cheaper than failing the run.
        for attempt in range(2):
            response = self._chat(
                system, user, max_tokens, 0.0 if attempt == 0 else 0.4, schema_json
            )
            try:
                return schema.model_validate_json(response["message"]["content"])
            except Exception as exc:  # pydantic ValidationError or JSONDecodeError
                last = exc
        raise LLMError(
            f"{self.model} did not return a valid {schema.__name__}: {last}"
        ) from last


# --- Anthropic ----------------------------------------------------------------


class AnthropicClient:
    """The original provider, kept for its citations.

    `cited_complete` is the only method on any client that returns provenance
    from the API rather than from `qa.py`'s marker parsing. It is what the marker
    scheme is measured against, which is the reason this path still exists.
    """

    provider = "anthropic"
    supports_native_citations = True
    default_workers = 4
    thinking_reserve = ANTHROPIC_THINKING_RESERVE

    def __init__(self, *, model: str | None = None) -> None:
        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMConfigError("ANTHROPIC_API_KEY is not set (env or .env).")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL)
        self.context_tokens = ANTHROPIC_CONTEXT_TOKENS
        self.effort = os.environ.get("ANTHROPIC_EFFORT", ANTHROPIC_EFFORT)
        try:
            import anthropic
        except ModuleNotFoundError as exc:
            raise LLMConfigError(
                "the `anthropic` package is not installed — "
                "run `uv sync --group cloud`."
            ) from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(max_retries=4)

    def _guard(self, fn, *args, **kwargs):
        a = self._anthropic
        try:
            return fn(*args, **kwargs)
        except a.RateLimitError as exc:
            raise LLMOverloaded("Anthropic rate limit reached.") from exc
        except a.APIConnectionError as exc:
            raise LLMUnavailable("Could not reach the Anthropic API.") from exc
        except a.APIStatusError as exc:
            if exc.status_code == 529:
                raise LLMOverloaded("The Anthropic API is overloaded.") from exc
            if exc.status_code == 400 and "credit balance" in str(exc).lower():
                raise LLMConfigError("The Anthropic account is out of credit.") from exc
            raise LLMError(f"Anthropic error {exc.status_code}.") from exc

    @staticmethod
    def _usage(usage: Any) -> dict[str, Any]:
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", 0
            ),
        }

    def _system(self, system: str) -> list[dict]:
        # Byte-stable across every question, so it caches once and every later
        # call reads it.
        return [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        response = self._guard(
            self._client.messages.create,
            model=self.model,
            max_tokens=max_tokens,
            output_config={"effort": self.effort},
            system=self._system(system),
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return Completion(
            text=text,
            stop_reason=response.stop_reason,
            refused=response.stop_reason == "refusal",
            usage=self._usage(response.usage),
        )

    def cited_complete(
        self, system: str, documents: list[dict], user: str, *, max_tokens: int
    ) -> tuple[Completion, list[tuple[int, str]]]:
        """Answer over document blocks, returning `(completion, [(index, quote)])`.

        The indices point into `documents` and cannot point outside it, which is
        the guarantee the marker scheme has to reconstruct by checking.
        """
        response = self._guard(
            self._client.messages.create,
            model=self.model,
            max_tokens=max_tokens,
            output_config={"effort": self.effort},
            system=self._system(system),
            messages=[
                {"role": "user", "content": [*documents, {"type": "text", "text": user}]}
            ],
        )
        parts: list[str] = []
        cites: list[tuple[int, str]] = []
        for block in response.content:
            if block.type != "text":
                continue
            parts.append(block.text)
            for raw in getattr(block, "citations", None) or []:
                idx = getattr(raw, "document_index", None)
                if idx is not None:
                    cites.append((idx, getattr(raw, "cited_text", "") or ""))
        completion = Completion(
            text="".join(parts).strip(),
            stop_reason=response.stop_reason,
            refused=response.stop_reason == "refusal",
            usage=self._usage(response.usage),
        )
        return completion, cites

    def parse[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        response = self._guard(
            self._client.messages.parse,
            model=self.model,
            max_tokens=max_tokens,
            output_config={"effort": self.effort},
            system=self._system(system),
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        return response.parsed_output


# --- content-filter fallback --------------------------------------------------


class FallbackClient:
    """A primary provider, plus a second one used only when moderation refuses.

    Deliberately narrow. It does **not** fall back on rate limits, outages, or
    bad requests -- those are transient or are real bugs, and silently answering
    them from a different model would hide both. It fires on exactly one
    condition: the primary returned a content-filter refusal, which is
    permanent, reproducible, and not the user's fault.

    Three invariants make a shared prompt safe to send to either provider:

    * **Citation style must match.** Native citations and `[n]` markers need
      different prompts and different parsing, so mixing the two would mean the
      fallback received a prompt built for the other scheme. Construction fails
      rather than allowing it.
    * **Context is budgeted against the smaller window.** The prompt is built
      once, before anyone knows which provider will answer it, so it has to fit
      the narrower of the two -- 32K for Ollama against Azure's 128K.
    * **The token ceiling is the larger reserve.** `num_predict` is a ceiling,
      not a target, so being generous costs a non-reasoning model nothing and
      stops a reasoning model spending the whole budget on `thinking`.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        if primary.supports_native_citations != fallback.supports_native_citations:
            raise LLMConfigError(
                f"cannot pair {primary.provider!r} with {fallback.provider!r}: they "
                f"disagree on native citations, so one would receive a prompt built "
                f"for the other's citation scheme."
            )
        self.primary = primary
        self.fallback = fallback
        self.provider = primary.provider
        self.model = primary.model
        self.supports_native_citations = primary.supports_native_citations
        self.default_workers = primary.default_workers
        self.context_tokens = min(primary.context_tokens, fallback.context_tokens)
        self.thinking_reserve = max(primary.thinking_reserve, fallback.thinking_reserve)

    @staticmethod
    def _filtered(completion: Completion) -> bool:
        return completion.stop_reason == "content_filter"

    def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float = 0.0
    ) -> Completion:
        out = self.primary.complete(
            system, user, max_tokens=max_tokens, temperature=temperature
        )
        if not self._filtered(out):
            out.answered_by = describe(self.primary)
            return out

        log.warning(
            "%s returned a content filter refusal; falling back to %s",
            describe(self.primary),
            describe(self.fallback),
        )
        out = self.fallback.complete(
            system, user, max_tokens=max_tokens, temperature=temperature
        )
        out.answered_by = describe(self.fallback)
        out.fallback_used = True
        return out

    def parse[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        try:
            return self.primary.parse(system, user, schema, max_tokens=max_tokens)
        except LLMContentFiltered:
            return self.fallback.parse(system, user, schema, max_tokens=max_tokens)

    def cited_complete(self, *args, **kwargs):
        # Only reachable when both providers support native citations, i.e. both
        # are Anthropic, which the selection logic never builds.
        return self.primary.cited_complete(*args, **kwargs)


# --- selection ----------------------------------------------------------------

_BUILDERS = {
    "azure": AzureClient,
    "ollama": OllamaClient,
    "anthropic": AnthropicClient,
}


def active_provider() -> str:
    load_env()
    name = os.environ.get("AI_FIQH_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if name not in _BUILDERS:
        raise LLMConfigError(
            f"AI_FIQH_LLM_PROVIDER={name!r} is not one of "
            f"{', '.join(sorted(_BUILDERS))}."
        )
    return name


def fallback_provider() -> str | None:
    """The configured content-filter fallback, if any."""
    load_env()
    name = (os.environ.get(FALLBACK_PROVIDER_VAR) or "").strip().lower()
    return name or None


def get_client(provider: str | None = None, *, fallback: bool = True, **kwargs) -> LLMClient:
    """The configured client. Constructed per caller — these are cheap and thread-safe.

    Wrapped in a `FallbackClient` when a fallback provider is configured and the
    caller did not name a provider explicitly. Naming one means "use this and
    nothing else", which is what the eval harness's `--provider` needs.
    """
    explicit = provider is not None
    name = (provider or active_provider()).strip().lower()
    if name not in _BUILDERS:
        raise LLMConfigError(f"unknown provider {name!r}.")
    client = _BUILDERS[name](**kwargs)

    second = fallback_provider() if (fallback and not explicit) else None
    if second and second != name:
        if second not in _BUILDERS:
            raise LLMConfigError(
                f"{FALLBACK_PROVIDER_VAR}={second!r} is not one of "
                f"{', '.join(sorted(_BUILDERS))}."
            )
        return FallbackClient(client, _BUILDERS[second]())
    return client


def describe(client: LLMClient) -> str:
    """`provider/model`, for stamping eval results so runs stay comparable."""
    return f"{client.provider}/{client.model}"

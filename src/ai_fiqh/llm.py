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

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PROVIDER = "azure"

# Azure. Deployment names are chosen by whoever created the resource, so nothing
# about the underlying model can be inferred from them -- see `_AzureClient` for
# how the parameter differences are discovered instead of guessed.
AZURE_API_VERSION = "2024-10-21"  # first GA version with strict structured outputs
AZURE_CONTEXT_TOKENS = 128_000

# Ollama. `num_ctx` is the load-bearing one: the server default is 2,048, so a
# retrieval context that fits gemma2's 8,192 window is still truncated in half
# without this -- silently, with no error and no warning in the response.
OLLAMA_MODEL = "gemma2:9b"
OLLAMA_CONTEXT_TOKENS = 8_192

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
        """Learn a deployment's parameter dialect from its rejection. True = retry."""
        message = str(exc).lower()
        if "max_completion_tokens" in message and self._token_param == "max_tokens":
            self._token_param = "max_completion_tokens"
            return True
        if "temperature" in message and self._send_temperature:
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
        response = self._send(
            self._client.chat.completions.create, system, user, max_tokens, temperature
        )
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
    thinking_reserve = 0

    def __init__(self, *, model: str | None = None) -> None:
        load_env()
        self.model = model or os.environ.get("OLLAMA_MODEL", OLLAMA_MODEL)
        self.context_tokens = int(
            os.environ.get("OLLAMA_CONTEXT_TOKENS", OLLAMA_CONTEXT_TOKENS)
        )
        try:
            import ollama
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise LLMConfigError("the `ollama` package is not installed.") from exc
        self._ollama = ollama
        self._client = ollama.Client(host=os.environ.get("OLLAMA_HOST") or None)

    def _chat(self, system: str, user: str, max_tokens: int, temperature: float,
              fmt: Any = None) -> Any:
        try:
            return self._client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                format=fmt,
                options={
                    "num_ctx": self.context_tokens,
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
            )
        except self._ollama.ResponseError as exc:
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
        return Completion(
            text=(response["message"]["content"] or "").strip(),
            stop_reason=response.get("done_reason"),
            refused=False,  # local models have no separate refusal signal
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


def get_client(provider: str | None = None, **kwargs) -> LLMClient:
    """The configured client. Constructed per caller — these are cheap and thread-safe."""
    name = (provider or active_provider()).strip().lower()
    if name not in _BUILDERS:
        raise LLMConfigError(f"unknown provider {name!r}.")
    return _BUILDERS[name](**kwargs)


def describe(client: LLMClient) -> str:
    """`provider/model`, for stamping eval results so runs stay comparable."""
    return f"{client.provider}/{client.model}"

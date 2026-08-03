"""Hybrid retrieval over the ingested chunks (docs/research.md §1.5).

    query
      |- fold + alias-expand -> BM25 over text_folded --|
      |- raw query           -> dense over text_raw   --|
                                                        v  reciprocal rank fusion -> 20
                                                           cross-encoder rerank   -> 5
                                                           group expansion (§1.3)
                                                           -> chunks

No vector store: 177 chunks is small enough that exhaustive numpy search *is* the
fast path, and every stage stays inspectable. `search` returns a `SearchTrace`
carrying each intermediate ranking for exactly that reason -- printing the BM25
ranking next to the dense one is how you see a polarity collision happen.

Embeddings are cached to `index/embeddings.npy` and keyed by a fingerprint over
the model name and chunk contents, so re-running after an ingest change refetches
but re-running otherwise does not.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from .normalize import expand_aliases

ROOT = Path(__file__).resolve().parents[2]
INDEX_DIR = ROOT / "index"
CHUNKS_PATH = INDEX_DIR / "chunks.json"
EMBEDDINGS_PATH = INDEX_DIR / "embeddings.npy"
EMBEDDINGS_META_PATH = INDEX_DIR / "embeddings.meta.json"
CHECKPOINT_PATH = INDEX_DIR / "embeddings.partial.npz"

# research.md §1.6 recommended voyage-3.5/voyage-3-large; both are deprecated as
# of 2026-08. voyage-4-large is the current quality tier -- and at 177 chunks the
# cost difference against voyage-4 is a rounding error, while prose homogeneity
# (§1.5) means retrieval quality is the binding constraint.
EMBED_MODEL = "voyage-4-large"
RERANK_MODEL = "rerank-2.5"
EMBED_DIM = 1024

# Request pacing. These were set for Voyage's free tier (3 RPM / 10K TPM), which
# paced a cold build of the ~89K-token corpus at about nine minutes; they were
# raised on 2026-08-03 once a payment method was added. Still conservative --
# the whole corpus is three requests, so there is nothing to gain by going wider.
# The retry/backoff path below stays regardless: it costs nothing when unused.
EMBED_BATCH_TOKENS = 32_000
TPM_BUDGET = 1_000_000
MIN_REQUEST_INTERVAL = 0.0

CANDIDATES = 20  # after fusion, before rerank
TOP_K = 5  # after rerank, before group expansion
RRF_K = 60  # standard reciprocal-rank-fusion damping constant

# Layer 2 of §1.7 abstains in code when the best reranked chunk scores below this.
#
# Measured on the 40-question golden set (eval/label_chunks.py, 2026-08-03):
#   answerable (n=24)     min 0.769  median 0.863  max 0.926
#   should-abstain (n=16) min 0.262  median 0.598  max 0.719
# The classes separate cleanly, so this sits mid-band. Two caveats: the margin is
# only 0.05, and it is fitted on the same set the eval reports against, so a 100%
# gate score there is not evidence of generalisation. The tightest negatives are
# cross-madhhab bait (max 0.719) -- of course, since the *topic* is in the book
# and only the madhhab is not. Those are precisely the cases layers 1/3/4 exist
# for; do not let a good number here talk you out of them.
MIN_RERANK_SCORE = 0.74


def load_env() -> None:
    """Load `.env` from the repo root. Safe to call repeatedly."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")


def load_chunks(path: Path = CHUNKS_PATH) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `uv run python -m ai_fiqh.ingest` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _voyage_client():
    load_env()
    if not os.environ.get("VOYAGE_API_KEY"):
        raise RuntimeError("VOYAGE_API_KEY is not set (looked in env and .env).")
    import voyageai

    return voyageai.Client()


def corpus_fingerprint(chunks: list[dict], model: str, dim: int) -> str:
    """Identity of an embedding set: same fingerprint means the cache is valid."""
    h = hashlib.sha256()
    h.update(f"{model}:{dim}".encode())
    for c in chunks:
        h.update(c["id"].encode())
        h.update(c["text_raw"].encode("utf-8"))
    return h.hexdigest()


def _l2_normalize(a: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.maximum(norms, 1e-12)


def _load_checkpoint(fingerprint: str) -> np.ndarray | None:
    """Partial vectors from an interrupted build, if they match this corpus."""
    if not CHECKPOINT_PATH.exists():
        return None
    with np.load(CHECKPOINT_PATH) as z:
        if str(z["fingerprint"]) != fingerprint:
            return None
        return z["vectors"]


def _save_checkpoint(fingerprint: str, vectors: list[list[float]]) -> None:
    INDEX_DIR.mkdir(exist_ok=True)
    np.savez(
        CHECKPOINT_PATH,
        fingerprint=np.array(fingerprint),
        vectors=np.asarray(vectors, dtype=np.float32),
    )


def _batch_by_tokens(
    client, texts: list[str], limit: int = EMBED_BATCH_TOKENS
) -> list[tuple[int, int, int]]:
    """Group texts into (start, end, token_count) spans under a token ceiling.

    Voyage's tokenizer runs locally, so measuring is free and beats guessing from
    character counts -- the TPM ceiling is low enough that overshooting costs a
    retry and a minute.
    """
    per = [client.count_tokens([t], model=EMBED_MODEL) for t in texts]
    spans: list[tuple[int, int, int]] = []
    start, total = 0, 0
    for i, n in enumerate(per):
        if total and total + n > limit:
            spans.append((start, i, total))
            start, total = i, 0
        total += n
    if start < len(texts):
        spans.append((start, len(texts), total))
    return spans


def _embed_with_retry(client, batch: list[str], *, verbose: bool) -> list[list[float]]:
    """Embed one batch, backing off on the free tier's rate limiter."""
    import voyageai.error

    delay = 30.0
    for attempt in range(6):
        try:
            return client.embed(
                batch,
                model=EMBED_MODEL,
                input_type="document",
                output_dimension=EMBED_DIM,
            ).embeddings
        except voyageai.error.RateLimitError:
            if attempt == 5:
                raise
            if verbose:
                print(f"    rate limited, retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 1.5
    raise AssertionError("unreachable")


def build_embeddings(
    chunks: list[dict], *, force: bool = False, verbose: bool = True
) -> np.ndarray:
    """Embed every chunk, or reuse the cache when the corpus is unchanged.

    Vectors are L2-normalized on the way in so a dot product is cosine similarity.
    Partial progress is checkpointed after every batch: a cold build takes minutes
    on the free tier, and losing it to one 429 at chunk 150 would be miserable.
    """
    fingerprint = corpus_fingerprint(chunks, EMBED_MODEL, EMBED_DIM)

    if not force and EMBEDDINGS_PATH.exists() and EMBEDDINGS_META_PATH.exists():
        meta = json.loads(EMBEDDINGS_META_PATH.read_text())
        if meta.get("fingerprint") == fingerprint:
            cached = np.load(EMBEDDINGS_PATH)
            if cached.shape[0] == len(chunks):
                if verbose:
                    print(f"embeddings: cache hit ({cached.shape[0]}x{cached.shape[1]})")
                return cached
        if verbose:
            print("embeddings: cache stale, re-embedding")

    client = _voyage_client()
    # `text_raw`, not `text_folded` -- §1.4: folding is a BM25-side device, and
    # discarding diacritics would throw away signal the embedding model can use.
    texts = [c["text_raw"] for c in chunks]

    vectors: list[list[float]] = []
    resume = _load_checkpoint(fingerprint)
    if resume is not None:
        vectors = [list(v) for v in resume]
        if verbose:
            print(f"embeddings: resuming from checkpoint at {len(vectors)}/{len(texts)}")

    spans = _batch_by_tokens(client, texts)
    if verbose:
        print(f"embeddings: {len(spans)} batches, ~{sum(s[2] for s in spans):,} tokens")

    for start, end, tokens in spans:
        if end <= len(vectors):  # already covered by the checkpoint
            continue
        vectors.extend(_embed_with_retry(client, texts[start:end], verbose=verbose))
        _save_checkpoint(fingerprint, vectors)
        if verbose:
            print(f"  embedded {len(vectors)}/{len(texts)} ({tokens:,} tokens)")
        if end < len(texts):
            time.sleep(max(MIN_REQUEST_INTERVAL, 60.0 * tokens / TPM_BUDGET))

    arr = _l2_normalize(np.asarray(vectors, dtype=np.float32))
    INDEX_DIR.mkdir(exist_ok=True)
    np.save(EMBEDDINGS_PATH, arr)
    CHECKPOINT_PATH.unlink(missing_ok=True)
    EMBEDDINGS_META_PATH.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "model": EMBED_MODEL,
                "dim": EMBED_DIM,
                "n_chunks": len(chunks),
            },
            indent=2,
        )
    )
    return arr


@dataclass
class Scored:
    """One chunk with the score and provenance of however it was ranked."""

    chunk: dict
    score: float
    rank: int
    source: str = "rerank"  # or "group-expansion"

    @property
    def id(self) -> str:
        return self.chunk["id"]

    def __repr__(self) -> str:
        c = self.chunk
        return (
            f"<{self.rank:>2}. {self.score:+.4f} {c['id']} "
            f"[{c['kitab']}/{c['category']}] p{c['page_start']}-{c['page_end']}>"
        )


@dataclass
class SearchTrace:
    """Every stage of one query, kept for notebook inspection (§3.2)."""

    query: str
    expanded_query: str
    bm25: list[Scored] = field(default_factory=list)
    dense: list[Scored] = field(default_factory=list)
    fused: list[Scored] = field(default_factory=list)
    reranked: list[Scored] = field(default_factory=list)
    results: list[Scored] = field(default_factory=list)  # after group expansion

    @property
    def top_score(self) -> float:
        return self.reranked[0].score if self.reranked else 0.0

    @property
    def is_confident(self) -> bool:
        """Layer 2 of §1.7. False means abstain without calling the model."""
        return self.top_score >= MIN_RERANK_SCORE

    def show(self, n: int = 5) -> None:
        """Print the stages side by side -- the whole reason there's no vector store."""
        print(f"query    : {self.query!r}")
        print(f"expanded : {self.expanded_query!r}")
        for name, stage in (
            ("BM25", self.bm25),
            ("DENSE", self.dense),
            ("FUSED", self.fused),
            ("RERANK", self.reranked),
            ("FINAL", self.results),
        ):
            print(f"\n--- {name} ---")
            for s in stage[:n]:
                tag = "  <- expanded" if s.source == "group-expansion" else ""
                print(f"  {s!r}{tag}")
        print(
            f"\ntop rerank score {self.top_score:.4f} "
            f"-> {'PASS' if self.is_confident else 'ABSTAIN'} "
            f"(gate {MIN_RERANK_SCORE})"
        )


def reciprocal_rank_fusion(
    rankings: list[list[Scored]], k: int = RRF_K
) -> dict[str, float]:
    """Fuse ranked lists by 1/(k + rank). Rank-based, so incommensurable
    BM25 and cosine scales never have to be reconciled."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for pos, scored in enumerate(ranking):
            fused[scored.id] = fused.get(scored.id, 0.0) + 1.0 / (k + pos + 1)
    return fused


class Retriever:
    """Hybrid search over the chunk corpus.

    Construct once and reuse -- BM25 tokenisation and the embedding load both
    happen lazily on first use.
    """

    def __init__(
        self,
        chunks: list[dict] | None = None,
        *,
        embeddings: np.ndarray | None = None,
        verbose: bool = True,
    ) -> None:
        self.chunks = chunks if chunks is not None else load_chunks()
        self.verbose = verbose
        self._embeddings = embeddings
        self._by_id = {c["id"]: i for i, c in enumerate(self.chunks)}
        self._groups: dict[str, list[int]] = {}
        for i, c in enumerate(self.chunks):
            if c.get("group_id"):
                self._groups.setdefault(c["group_id"], []).append(i)

    @cached_property
    def bm25(self) -> BM25Okapi:
        return BM25Okapi([c["text_folded"].split() for c in self.chunks])

    @property
    def embeddings(self) -> np.ndarray:
        if self._embeddings is None:
            self._embeddings = build_embeddings(self.chunks, verbose=self.verbose)
        return self._embeddings

    # --- individual stages, each usable alone from the notebook --------------

    def search_bm25(self, query: str, n: int = CANDIDATES) -> list[Scored]:
        expanded = expand_aliases(query)
        scores = self.bm25.get_scores(expanded.split())
        order = np.argsort(scores)[::-1][:n]
        return [
            Scored(self.chunks[i], float(scores[i]), rank + 1, "bm25")
            for rank, i in enumerate(order)
            if scores[i] > 0
        ]

    def search_dense(self, query: str, n: int = CANDIDATES) -> list[Scored]:
        client = _voyage_client()
        resp = client.embed(
            [query],
            model=EMBED_MODEL,
            input_type="query",
            output_dimension=EMBED_DIM,
        )
        q = _l2_normalize(np.asarray(resp.embeddings, dtype=np.float32))[0]
        sims = self.embeddings @ q
        order = np.argsort(sims)[::-1][:n]
        return [
            Scored(self.chunks[i], float(sims[i]), rank + 1, "dense")
            for rank, i in enumerate(order)
        ]

    def rerank(self, query: str, candidates: list[Scored], top_k: int = TOP_K) -> list[Scored]:
        if not candidates:
            return []
        client = _voyage_client()
        resp = client.rerank(
            query=query,
            documents=[s.chunk["text_raw"] for s in candidates],
            model=RERANK_MODEL,
            top_k=top_k,
        )
        return [
            Scored(candidates[r.index].chunk, float(r.relevance_score), rank + 1, "rerank")
            for rank, r in enumerate(resp.results)
        ]

    def expand_groups(self, results: list[Scored]) -> list[Scored]:
        """Pull in every sibling of any polarity group that surfaced (§1.3).

        The point is to never *choose* between "things which nullify X" and
        "things which do not nullify X" -- both go to the model, and the wrong
        one cannot be picked because neither is picked. Siblings are appended
        directly after the hit that dragged them in, so ordering stays readable.
        """
        retrieved = {s.id: s for s in results}
        out: list[Scored] = []
        seen: set[str] = set()

        def emit(s: Scored) -> None:
            # Copy rather than renumber in place: `results` is the caller's
            # reranked list, and mutating it would corrupt that stage of the trace.
            seen.add(s.id)
            out.append(replace(s, rank=len(out) + 1))

        for s in results:
            if s.id not in seen:
                emit(s)
            gid = s.chunk.get("group_id")
            if not gid:
                continue
            for i in self._groups.get(gid, []):
                sibling = self.chunks[i]
                if sibling["id"] in seen:
                    continue
                # A sibling the reranker found on its own keeps its real score and
                # provenance; only chunks that would otherwise have been missed
                # are attributed to expansion.
                emit(
                    retrieved.get(sibling["id"])
                    or Scored(sibling, s.score, 0, "group-expansion")
                )
        return out

    # --- the whole pipeline ---------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = TOP_K,
        candidates: int = CANDIDATES,
        expand: bool = True,
    ) -> SearchTrace:
        trace = SearchTrace(query=query, expanded_query=expand_aliases(query))
        trace.bm25 = self.search_bm25(query, candidates)
        trace.dense = self.search_dense(query, candidates)

        fused_scores = reciprocal_rank_fusion([trace.bm25, trace.dense])
        ordered = sorted(fused_scores.items(), key=lambda kv: kv[1], reverse=True)
        trace.fused = [
            Scored(self.chunks[self._by_id[cid]], score, rank + 1, "rrf")
            for rank, (cid, score) in enumerate(ordered[:candidates])
        ]

        trace.reranked = self.rerank(query, trace.fused, top_k)
        trace.results = self.expand_groups(trace.reranked) if expand else list(trace.reranked)
        return trace

    def get_section(self, chunk_id: str) -> list[dict]:
        """All parts of the section a chunk belongs to, in order (§2.2).

        Sub-split sections share every field but `part`, so revision mode can ask
        for a whole `bab` without caring how ingestion divided it.
        """
        if chunk_id not in self._by_id:
            raise KeyError(chunk_id)
        target = self.chunks[self._by_id[chunk_id]]
        return sorted(
            (c for c in self.chunks if c["bab"] == target["bab"] and c["kitab"] == target["kitab"]),
            key=lambda c: c["part"],
        )


def main() -> None:
    """Build the embedding cache and smoke-test the polarity case."""
    chunks = load_chunks()
    print(f"chunks loaded    : {len(chunks)}")
    build_embeddings(chunks)

    r = Retriever(chunks)
    print(f"polarity groups  : {len(r._groups)} -> "
          f"{ {g: len(v) for g, v in r._groups.items()} }")

    trace = r.search("does laughing aloud break wudu")
    print()
    trace.show()


if __name__ == "__main__":
    main()

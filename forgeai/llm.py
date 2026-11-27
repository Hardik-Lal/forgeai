"""Thin, defensive client for a local Ollama server.

Every stage of ForgeAI goes through this module, so it is where we
  * force structured output (Ollama `format` = JSON schema) because a 7B model
    will otherwise drift out of JSON,
  * validate + repair JSON instead of trusting it,
  * account every token and second against a run budget (stretch goal),
  * cache calls on disk so re-runs are reproducible and cheap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import requests

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
DEFAULT_MODEL = os.environ.get("FORGE_MODEL", "qwen2.5:7b-instruct")
DEFAULT_EMBED = os.environ.get("FORGE_EMBED_MODEL", "nomic-embed-text")
CACHE_DIR = Path(os.environ.get("FORGE_CACHE", Path.cwd() / ".cache"))


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class Budget:
    """Token / wall-clock budget for one factory run."""

    max_tokens: int = 2_000_000
    max_seconds: float = 4 * 3600
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    cache_hits: int = 0
    llm_seconds: float = 0.0
    cached_tokens: int = 0          # what the cache hits originally cost (when recorded), not counted against the budget
    cached_seconds: float = 0.0
    started: float = field(default_factory=time.time)
    by_stage: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def remaining_fraction(self) -> float:
        return max(0.0, min(1 - self.tokens / self.max_tokens, 1 - self.elapsed / self.max_seconds))

    def check(self) -> None:
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"token budget exhausted ({self.tokens}/{self.max_tokens})")
        if self.elapsed >= self.max_seconds:
            raise BudgetExceeded(f"time budget exhausted ({self.elapsed:.0f}s/{self.max_seconds:.0f}s)")

    def record(self, stage: str, tin: int, tout: int, secs: float, cached: bool) -> None:
        with self._lock:
            self.calls += 1
            if cached:
                self.cache_hits += 1
                self.cached_tokens += tin + tout
                self.cached_seconds += secs
                return
            self.tokens_in += tin
            self.tokens_out += tout
            self.llm_seconds += secs
            s = self.by_stage.setdefault(stage, {"calls": 0, "tokens": 0, "seconds": 0.0})
            s["calls"] += 1
            s["tokens"] += tin + tout
            s["seconds"] = round(s["seconds"] + secs, 2)

    def snapshot(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "llm_seconds": round(self.llm_seconds, 1),
            "cached_tokens": self.cached_tokens,
            "cached_llm_seconds": round(self.cached_seconds, 1),
            "wall_seconds": round(self.elapsed, 1),
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "by_stage": self.by_stage,
        }


class _Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self._lock = threading.Lock()

    def get(self, k: str):
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, k: str, v) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
            self._conn.commit()


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def extract_json(text: str) -> Any:
    """Best-effort JSON recovery from model output."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found")


def _validate(obj: Any, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema validation (type / required / enum / items)."""
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if t and t in types and not isinstance(obj, types[t]):
        return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if t == "object":
        for req in schema.get("required", []):
            if req not in obj:
                errs.append(f"{path}: missing '{req}'")
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                errs.extend(_validate(obj[k], sub, f"{path}.{k}"))
    if t == "array" and "items" in schema:
        for i, item in enumerate(obj):
            errs.extend(_validate(item, schema["items"], f"{path}[{i}]"))
    return errs


class LLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        embed_model: str = DEFAULT_EMBED,
        budget: Budget | None = None,
        use_cache: bool = True,
        num_ctx: int = 8192,
        seed: int = 7,
    ):
        self.model = model
        self.embed_model = embed_model
        self.budget = budget or Budget()
        self.num_ctx = num_ctx
        self.seed = seed
        self.cache = _Cache(CACHE_DIR / "llm.sqlite") if use_cache else None
        self.emb_cache = _Cache(CACHE_DIR / "emb.sqlite")
        self.session = requests.Session()

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        stage: str = "misc",
        model: str | None = None,
        seed: int | None = None,
    ) -> str:
        model = model or self.model
        seed = self.seed if seed is None else seed
        k = _key(model, messages, schema, temperature, max_tokens, seed, self.num_ctx)
        if self.cache is not None:
            hit = self.cache.get(k)
            if hit is not None:
                cost = self.cache.get(k + ":cost")
                tin, tout, secs = json.loads(cost) if cost else (0, 0, 0.0)
                self.budget.record(stage, tin, tout, secs, cached=True)
                return hit
        self.budget.check()
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
                "seed": seed,
            },
        }
        if schema is not None:
            payload["format"] = schema
        t0 = time.time()
        last_err = None
        for attempt in range(3):
            try:
                r = self.session.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600)
                r.raise_for_status()
                data = r.json()
                break
            except requests.RequestException as e:  # pragma: no cover - network
                last_err = e
                time.sleep(2 * (attempt + 1))
        else:
            raise LLMError(f"Ollama call failed: {last_err}. Is `ollama serve` running and '{model}' pulled?")
        text = data.get("message", {}).get("content", "")
        cost = (data.get("prompt_eval_count", 0), data.get("eval_count", 0), round(time.time() - t0, 3))
        self.budget.record(stage, *cost, cached=False)
        if self.cache is not None:
            self.cache.put(k, text)
            self.cache.put(k + ":cost", json.dumps(cost))
        return text

    def json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        stage: str = "misc",
        temperature: float = 0.0,
        max_tokens: int = 1500,
        retries: int = 2,
        model: str | None = None,
    ) -> dict:
        """Structured call: schema-constrained decoding + validation + repair loop."""
        msgs = list(messages)
        err = ""
        for attempt in range(retries + 1):
            raw = self.chat(
                msgs,
                schema=schema,
                temperature=temperature if attempt == 0 else min(0.4, temperature + 0.2 * attempt),
                max_tokens=max_tokens,
                stage=stage,
                model=model,
                seed=self.seed + attempt,
            )
            try:
                obj = extract_json(raw)
                errs = _validate(obj, schema)
                if not errs:
                    return obj
                err = "; ".join(errs[:5])
            except ValueError as e:
                err = str(e)
            msgs = list(messages) + [
                {"role": "assistant", "content": raw[:3000]},
                {"role": "user", "content": f"That output was invalid ({err}). Return ONLY valid JSON matching the schema."},
            ]
        raise LLMError(f"[{stage}] could not obtain valid JSON after {retries + 1} attempts: {err}")

    # ------------------------------------------------------------- embeddings
    def embed(self, texts: list[str], kind: str = "document") -> np.ndarray:
        """Embed texts (cached per text). `kind` selects nomic task prefixes."""
        prefix = ""
        if "nomic" in self.embed_model:
            prefix = "search_query: " if kind == "query" else "search_document: "
        out: list[list[float] | None] = []
        missing: list[int] = []
        keys = [_key(self.embed_model, prefix + t) for t in texts]
        for i, k in enumerate(keys):
            v = self.emb_cache.get(k)
            out.append(v)
            if v is None:
                missing.append(i)
        for start in range(0, len(missing), 32):
            idx = missing[start : start + 32]
            batch = [prefix + texts[i][:6000] for i in idx]
            t0 = time.time()
            r = self.session.post(
                f"{OLLAMA_URL}/api/embed", json={"model": self.embed_model, "input": batch}, timeout=600
            )
            r.raise_for_status()
            data = r.json()
            self.budget.record("embed", data.get("prompt_eval_count", 0), 0, time.time() - t0, cached=False)
            for i, vec in zip(idx, data["embeddings"]):
                out[i] = vec
                self.emb_cache.put(keys[i], vec)
        arr = np.array(out, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(len(texts), -1)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.maximum(norms, 1e-9)

    def ping(self) -> dict:
        r = self.session.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
        return {
            "models": names,
            "chat_model_ok": any(n == self.model or n.startswith(self.model + ":") for n in names),
            "embed_model_ok": any(n.split(":")[0] == self.embed_model.split(":")[0] for n in names),
        }

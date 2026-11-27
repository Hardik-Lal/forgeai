"""Stage 4: evaluation-set generator.

Builds the test set FROM THE DOCUMENTS (nothing hand-written):
  answerable    one seed passage -> question + key facts copied from it
  ambiguous     same, but vague / colloquial / typo'd phrasing
  multi_doc     two related passages from different documents
  out_of_scope  must-refuse topics
  gap           topics the groundability guard flagged as unsupported
  injection     tries to talk the agent into a must-refuse topic
  tool          values computed deterministically from tables / tools

Every generated case is *verified* before it is accepted (facts must be found in
the seed passage, question must not leak the answer, no near-duplicates).
Rejected candidates are logged with the reason. Cases are split into a
`dev` set (the optimizer may look at it) and a `heldout` set (it may not).
"""
from __future__ import annotations

import datetime as dt
import random
import re
from typing import Callable

import numpy as np

from . import prompts
from .documents import Document
from .llm import LLM, LLMError
from .retrieval import Chunk, Index, RetrievalConfig
from .textmatch import content_tokens, fact_support, numbers, quote_in
from .tools import ToolContext, calculator, current_date

ANSWERABLE_STYLES = [
    "a direct, specific question",
    "a practical scenario question ('I am ... can I ...?')",
    "a question about a condition, limit or exception",
]
AMBIGUOUS_STYLES = [
    "vague and informal, as typed quickly in chat, without the exact terminology of the passage (use everyday words or synonyms)",
    "indirect: describe the user's situation and let the reader infer what rule applies; include one small typo",
]
_RULE_WORDS = re.compile(r"\b(must|shall|entitled|eligible|maximum|minimum|within|up to|at least|not exceed|require|days?|hours?|per|rate|fee|cost|price|limit|deadline|approv)", re.I)
_LEAK = re.compile(r"\b(the|this) (passage|document|section|text|excerpt)\b|according to", re.I)

CASE_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}, "answer": {"type": "string"}, "key_facts": {"type": "array", "items": {"type": "string"}}},
    "required": ["question", "answer", "key_facts"],
}
MULTI_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "facts_from_a": {"type": "array", "items": {"type": "string"}},
        "facts_from_b": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question", "facts_from_a", "facts_from_b"],
}
QLIST_SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}}, "required": ["questions"]}
ONEQ_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}


def _informative(c: Chunk) -> float:
    body = c.text.split("\n", 1)[-1]
    words = len(body.split())
    if words < 30:
        return 0.0
    return min(words, 200) / 200 + 0.4 * min(len(_RULE_WORDS.findall(body)), 6) / 6 + 0.3 * min(len(numbers(body)), 4) / 4


class EvalGenerator:
    def __init__(self, req: dict, docs: list[Document], index: Index, llm: LLM, tools: list[str], seed: int = 13,
                 today: dt.date | None = None, log: Callable = print):
        self.req, self.docs, self.index, self.llm, self.tools = req, docs, index, llm, tools
        self.rng = random.Random(seed)
        self.today = today or dt.date.today()
        self.log = log
        self.cases: list[dict] = []
        self.rejected: list[dict] = []
        self._q_emb: list[np.ndarray] = []
        self._used_chunks: set[str] = set()

    # ------------------------------------------------------------ utilities
    def _meta(self, prompt_name: str | None) -> dict:
        return {"prompt": prompt_name, "prompt_sha": prompts.fingerprint(prompt_name)} if prompt_name else {"prompt": "deterministic", "prompt_sha": "-"}

    def _dup(self, q: str) -> bool:
        v = self.llm.embed([q], kind="query")[0]
        if any(float(v @ e) > 0.92 for e in self._q_emb):
            return True
        self._q_emb.append(v)
        return False

    def _reject(self, kind: str, why: str, payload) -> None:
        self.rejected.append({"type": kind, "reason": why, "candidate": payload})

    def _add(self, case: dict) -> None:
        case["id"] = f"{case['type'][:3].upper()}-{len(self.cases) + 1:02d}"
        self.cases.append(case)

    def _seed_chunks(self, n: int) -> list[Chunk]:
        text_chunks = [c for c in self.index.chunks if c.chunk_id not in self._used_chunks and _informative(c) > 0.3
                       and not c.chunk_id.split("#")[1].startswith("r")]
        by_doc: dict[str, list[Chunk]] = {}
        for c in text_chunks:
            by_doc.setdefault(c.doc_id, []).append(c)
        for lst in by_doc.values():
            self.rng.shuffle(lst)
            lst.sort(key=lambda c: -_informative(c) + self.rng.random() * 0.5)
        out, docs = [], list(by_doc)
        self.rng.shuffle(docs)
        while len(out) < n and any(by_doc.values()):
            for d in docs:
                if by_doc[d] and len(out) < n:
                    out.append(by_doc[d].pop(0))
        return out

    # ------------------------------------------------------------ generators
    def gen_single(self, kind: str, n: int) -> None:
        styles = ANSWERABLE_STYLES if kind == "answerable" else AMBIGUOUS_STYLES
        made, tries = 0, 0
        for c in self._seed_chunks(n * 2):
            if made >= n:
                break
            tries += 1
            passage = c.text.split("\n", 1)[-1][:1800]
            style = styles[tries % len(styles)]
            try:
                out = self.llm.json([{"role": "user", "content": prompts.render(
                    "eval_answerable", audience=self.req["audience"], scope=self.req["scope"], doc_id=c.doc_id, passage=passage, style=style)}],
                    CASE_SCHEMA, stage="evalgen", temperature=0.5, max_tokens=500)
            except LLMError as e:
                self._reject(kind, f"generation failed: {e}", c.chunk_id)
                continue
            q = out["question"].strip()
            facts, dropped = [], []
            for f in out["key_facts"][:3]:
                (facts if fact_support(f, passage) >= 0.75 and 1 <= len(content_tokens(f)) and len(f.split()) <= 25 else dropped).append(f.strip())
            if _LEAK.search(q):
                self._reject(kind, "question refers to 'the passage/document'", out)
                continue
            if not facts:
                self._reject(kind, f"no key fact could be verified in the seed passage (dropped: {dropped})", out)
                continue
            if all(fact_support(f, q) >= 0.9 for f in facts):
                self._reject(kind, "question leaks its own answer", out)
                continue
            if self._dup(q):
                self._reject(kind, "near-duplicate of an existing question", out)
                continue
            rank = self.index.rank_of_doc(q, c.doc_id, RetrievalConfig(), depth=10)
            self._used_chunks.add(c.chunk_id)
            self._add({
                "type": kind, "question": q, "expected_behavior": "answer", "key_facts": facts,
                "reference_answer": out["answer"], "expected_sources": [c.doc_id], "expected_tools": [],
                "seed_chunks": [c.chunk_id], "style": style,
                "validation": {"facts_dropped": dropped, "seed_doc_rank_at_generation": rank}, "generator": self._meta("eval_answerable"),
            })
            made += 1
        if made < n:
            self.log(f"  evalgen: only {made}/{n} {kind} cases passed validation")

    def gen_delta(self, changes: list[dict]) -> None:
        """Regression mode: one fresh test per changed sentence. The question is generated from the passage around the
        NEW sentence, and at least one verified key fact must carry a value that is new in this version (otherwise the
        test would not exercise the change)."""
        for ch in changes:
            new_vals = (set(numbers(ch["new"])) - set(numbers(ch["old"]))) if ch["old"] else set(numbers(ch["new"]))
            passage = ch["context"][:1800]
            try:
                out = self.llm.json([{"role": "user", "content": prompts.render(
                    "eval_answerable", audience=self.req["audience"], scope=self.req["scope"], doc_id=ch["doc_id"], passage=passage,
                    style=f"a direct question whose answer is stated in this sentence: \"{ch['new']}\"")}],
                    CASE_SCHEMA, stage="evalgen", temperature=0.3, max_tokens=500)
            except LLMError as e:
                self._reject("delta", f"generation failed: {e}", ch)
                continue
            q = out["question"].strip()
            facts = [f.strip() for f in out["key_facts"][:3] if fact_support(f, passage) >= 0.75 and len(f.split()) <= 25]
            facts = [f for f in facts if (not new_vals or set(numbers(f)) & new_vals) and self._needed(q, f)] or (
                [ch["new"]] if len(ch["new"].split()) <= 25 else [])
            if not facts or _LEAK.search(q) or all(fact_support(f, q) >= 0.9 for f in facts):
                self._reject("delta", "no verified fact exercising the changed value, or leaky question", {"change": ch, "out": out})
                continue
            self._add({"type": "delta", "question": q, "expected_behavior": "answer", "key_facts": facts,
                       "reference_answer": out["answer"], "expected_sources": [ch["doc_id"]], "expected_tools": [],
                       "change": {"old": ch["old"], "new": ch["new"]}, "generator": self._meta("eval_answerable")})

    def gen_multi(self, n: int) -> None:
        text_docs = sorted({c.doc_id for c in self.index.chunks if not c.chunk_id.split("#")[1].startswith("r")})
        if len(text_docs) < 2 and not any(d.kind == "table" for d in self.docs):
            self.log("  evalgen: fewer than 2 documents, skipping multi-document cases")
            return
        pos = {c.chunk_id: i for i, c in enumerate(self.index.chunks)}
        made = 0
        # multi-doc cases may reuse passages already used by single-doc cases (different question type);
        # small corpora would otherwise run out of passages and silently produce no multi-doc cases
        seeds = [c for c in self.index.chunks if _informative(c) > 0.3 and not c.chunk_id.split("#")[1].startswith("r")]
        self.rng.shuffle(seeds)
        used_pairs: set = set()
        used_a: set = set()
        for a in seeds:
            if made >= n:
                break
            if a.chunk_id in used_a:
                continue
            sims = self.index.emb @ self.index.emb[pos[a.chunk_id]]
            order = np.argsort(-sims)
            b = next((self.index.chunks[i] for i in order if self.index.chunks[i].doc_id != a.doc_id and float(sims[i]) >= 0.6
                      and frozenset((a.chunk_id, self.index.chunks[i].chunk_id)) not in used_pairs
                      and self.index.chunks[i].chunk_id not in used_a and _informative(self.index.chunks[i]) > 0.2), None)
            if b is None:
                continue
            used_pairs.add(frozenset((a.chunk_id, b.chunk_id)))
            pa, pb = a.text.split("\n", 1)[-1][:1400], b.text.split("\n", 1)[-1][:1400]
            try:
                out = self.llm.json([{"role": "user", "content": prompts.render(
                    "eval_multidoc", audience=self.req["audience"], scope=self.req["scope"], doc_a=a.doc_id, doc_b=b.doc_id,
                    passage_a=pa, passage_b=pb)}], MULTI_SCHEMA, stage="evalgen", temperature=0.5, max_tokens=500)
            except LLMError as e:
                self._reject("multi_doc", f"generation failed: {e}", [a.chunk_id, b.chunk_id])
                continue
            q = out["question"].strip()
            fa = [f for f in out["facts_from_a"][:2] if fact_support(f, pa) >= 0.75 and len(f.split()) <= 25 and self._needed(q, f)]
            fb = [f for f in out["facts_from_b"][:2] if fact_support(f, pb) >= 0.75 and len(f.split()) <= 25 and self._needed(q, f)]
            if not fa or not fb:
                self._reject("multi_doc", "facts could not be verified in BOTH passages", out)
                continue
            if _LEAK.search(q) or self._dup(q):
                self._reject("multi_doc", "leaky wording or duplicate", out)
                continue
            used_a.update({a.chunk_id, b.chunk_id})
            self._add({
                "type": "multi_doc", "question": q, "expected_behavior": "answer", "key_facts": fa + fb,
                "fact_sources": [a.doc_id] * len(fa) + [b.doc_id] * len(fb),
                "expected_sources": [a.doc_id, b.doc_id], "expected_tools": [], "seed_chunks": [a.chunk_id, b.chunk_id],
                "validation": {"pair_similarity": round(float(sims[pos[b.chunk_id]]), 3)}, "generator": self._meta("eval_multidoc"),
            })
            made += 1
        if made < n:
            self.log(f"  evalgen: only {made}/{n} multi-document cases passed validation")

    def gen_refusals(self, kind: str, topics: list[str]) -> None:
        """One question per topic, then verify it is really about that topic and not an answerable one."""
        if not topics:
            return
        gap_set = {g["topic"].lower() for g in self.req.get("groundability", {}).get("gaps", [])}
        answerable = [t for t in self.req.get("must_answer_topics", []) if t.lower() not in gap_set]
        ans_vecs = self.llm.embed(answerable, kind="query") if answerable else None
        for topic in topics:
            ok = False
            for attempt in range(3):
                try:
                    out = self.llm.json([{"role": "user", "content": prompts.render(
                        "eval_out_of_scope", audience=self.req["audience"], topic=topic,
                        answerable="; ".join(answerable) or "-")}],
                        ONEQ_SCHEMA, stage="evalgen", temperature=0.5 + 0.15 * attempt, max_tokens=150)
                except LLMError as e:
                    self._reject(kind, f"generation failed: {e}", topic)
                    continue
                q = out["question"].strip()
                qv = self.llm.embed([q], kind="query")[0]
                tv = self.llm.embed([topic], kind="query")[0]
                to_topic = float(qv @ tv)
                to_answerable = float((ans_vecs @ qv).max()) if ans_vecs is not None else 0.0
                if len(q) < 10:
                    self._reject(kind, "too short", q)
                    continue
                verdict = "embedding"
                if to_answerable >= to_topic:
                    # embeddings are noisy at these similarity levels: ask a strict classifier before rejecting
                    cls = self._classify_scope(q, topic, answerable)
                    if cls != "refuse":
                        self._reject(kind, f"closer to an answerable topic ({to_answerable:.2f} vs '{topic}' {to_topic:.2f}) and classifier said '{cls}'", q)
                        continue
                    verdict = "classifier"
                if self._dup(q):
                    self._reject(kind, "near-duplicate", q)
                    continue
                self._add({"type": kind, "question": q, "expected_behavior": "refuse", "key_facts": [], "expected_sources": [],
                           "expected_tools": [], "refuse_topic": topic, "generator": self._meta("eval_out_of_scope"),
                           "validation": {"sim_to_topic": round(to_topic, 3), "max_sim_to_answerable": round(to_answerable, 3), "accepted_by": verdict}})
                ok = True
                break
            if not ok:
                self.log(f"  evalgen: no valid {kind} question for topic '{topic}'")

    def _needed(self, q: str, fact: str) -> bool:
        """The model sometimes lists true-but-irrelevant facts from a passage; keep only facts the question asks for."""
        try:
            r = self.llm.json([{"role": "user", "content": prompts.render("eval_fact_needed", question=q, fact=fact)}],
                              {"type": "object", "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}}, "required": ["answer"]},
                              stage="evalgen", max_tokens=20)
            return r["answer"] == "yes"
        except LLMError:
            return True

    def _classify_scope(self, q: str, topic: str, answerable: list[str]) -> str:
        schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["allowed", "refuse"]}}, "required": ["label"]}
        hits = self.index.search(q, RetrievalConfig(top_k=3))
        excerpts = "\n\n".join(f"- ({h.chunk.doc_id}) {h.chunk.text.split(chr(10), 1)[-1][:700]}" for h in hits)
        try:
            r = self.llm.json([{"role": "user", "content": prompts.render(
                "eval_scope_check", question=q, topic=topic, answerable="; ".join(answerable), excerpts=excerpts)}], schema, stage="evalgen", max_tokens=20)
            return r["label"]
        except LLMError:
            return "error"

    def gen_injection(self) -> None:
        topics = self.req.get("must_refuse_topics") or ["topics outside your scope"]
        topic = self.rng.choice(topics)
        try:
            out = self.llm.json([{"role": "user", "content": prompts.render("eval_injection", audience=self.req["audience"], topic=topic)}],
                                ONEQ_SCHEMA, stage="evalgen", temperature=0.6, max_tokens=150)
        except LLMError:
            return
        self._add({"type": "injection", "question": out["question"].strip(), "expected_behavior": "refuse", "key_facts": [],
                   "expected_sources": [], "expected_tools": [], "refuse_topic": topic, "generator": self._meta("eval_injection")})

    def _phrase(self, task: str, must_keep: list[str]) -> tuple[str, str]:
        try:
            q = self.llm.json([{"role": "user", "content": prompts.render("eval_tool_phrase", audience=self.req["audience"], task=task)}],
                              ONEQ_SCHEMA, stage="evalgen", temperature=0.4, max_tokens=120)["question"].strip()
            if all(str(v).lower() in q.lower() for v in must_keep):
                return q, "eval_tool_phrase"
        except LLMError:
            pass
        return task, "deterministic"

    def gen_tools(self) -> None:
        tables = [d for d in self.docs if d.kind == "table" and d.rows]
        ctx = ToolContext(docs=self.docs, today=self.today)
        if tables:
            t = max(tables, key=lambda d: len(d.rows))
            cols = t.columns
            uniq = [c for c in cols if len({r.get(c, "") for r in t.rows}) == len(t.rows) and all(r.get(c) for r in t.rows)]
            id_col = next((c for c in uniq if any(ch.isdigit() for ch in t.rows[0][c]) and len(t.rows[0][c]) <= 20), uniq[0] if uniq else cols[0])
            name_col = next((c for c in uniq if c != id_col and len(t.rows[0][c].split()) >= 2), None)

            def num(v):
                try:
                    return float(str(v).replace(",", "").replace("$", "").strip())
                except ValueError:
                    return None

            num_cols = [c for c in cols if c != id_col and all(num(r.get(c)) is not None for r in t.rows)]
            price_col = next((c for c in num_cols if re.search(r"price|cost|rate|usd|inr|eur|amount", c, re.I)), num_cols[0] if num_cols else None)
            lead_col = next((c for c in num_cols if re.search(r"lead|days|delivery|eta", c, re.I)), None)
            other_cols = [c for c in cols if c not in (id_col, name_col)]

            def hum(c: str) -> str:
                h = re.sub(r"_?usd$", " (USD)", c, flags=re.I).replace("_", " ").strip()
                return h.replace("qty", "quantity").replace("no", "number") if h in ("part no", "stock qty") else h
            rows = self.rng.sample(t.rows, min(len(t.rows), 6))
            if "csv_lookup" in self.tools and other_cols:
                for r in rows[:2]:
                    target = self.rng.choice(other_cols)
                    if not r.get(target):
                        continue
                    task = f"What is the {hum(target)} of {hum(id_col)} {r[id_col]}" + (f" ({r[name_col]})" if name_col else "") + "?"
                    q, src = self._phrase(task, [r[id_col]])
                    self._add({"type": "tool", "question": q, "expected_behavior": "answer", "key_facts": [r[target]],
                               "expected_sources": [t.doc_id], "expected_tools": ["csv_lookup"], "seed_chunks": [],
                               "ground_truth": {"row": r, "column": target}, "generator": self._meta(src if src != "deterministic" else None)})
            if "calculator" in self.tools and price_col:
                r = rows[2 % len(rows)]
                qty = self.rng.choice([3, 4, 6, 8, 12, 15])
                total = calculator({"expression": f"{qty} * {num(r[price_col])}"}, ctx)["result"]
                total_s = f"{total:,.2f}".rstrip("0").rstrip(".") if isinstance(total, float) else str(total)
                task = f"What is the total {hum(price_col).replace('unit ', '')} for {qty} units of {hum(id_col)} {r[id_col]}" + (f" ({r[name_col]})" if name_col else "") + "?"
                q, src = self._phrase(task, [r[id_col], str(qty)])
                self._add({"type": "tool", "question": q, "expected_behavior": "answer", "key_facts": [total_s],
                           "expected_sources": [t.doc_id], "expected_tools": ["calculator"],
                           "ground_truth": {"row": r, "qty": qty, "unit": r[price_col], "total": total},
                           "generator": self._meta(src if src != "deterministic" else None)})
            if "current_date" in self.tools and lead_col:
                r = rows[3 % len(rows)]
                n = int(num(r[lead_col]))
                res = current_date({"add_days": n}, ctx)
                task = f"If I order {hum(id_col)} {r[id_col]}" + (f" ({r[name_col]})" if name_col else "") + f" today, on what date should it be ready given its {hum(lead_col)}?"
                q, src = self._phrase(task, [r[id_col]])
                self._add({"type": "tool", "question": q, "expected_behavior": "answer", "key_facts": [res["result_date"]],
                           "expected_sources": [t.doc_id], "expected_tools": ["current_date"], "fact_kind": "date",
                           "ground_truth": {"row": r, "lead_days": n, "today": self.today.isoformat(), "date": res["result_date"]},
                           "generator": self._meta(src if src != "deterministic" else None)})
        if "current_date" in self.tools and not (tables and any(re.search(r"lead|days|delivery|eta", c, re.I) for t in tables for c in t.columns)):
            m = None
            for c in self.index.chunks:
                m = re.search(r"within (\d{1,3}) (?:calendar |working )?days", c.text, re.I)
                if m and "working" not in m.group(0).lower():
                    break
                m = None
            if m:
                n = int(m.group(1))
                res = current_date({"add_days": n}, ctx)
                q, src = self._phrase(f"What is the date {n} days from today?", [str(n)])
                self._add({"type": "tool", "question": q, "expected_behavior": "answer", "key_facts": [res["result_date"]], "fact_kind": "date",
                           "expected_sources": [], "expected_tools": ["current_date"], "generator": self._meta(src if src != "deterministic" else None)})
        if "weather" in self.tools:
            places = re.findall(r"\b(?:in|at|port of|to) ([A-Z][a-z]{3,}(?: [A-Z][a-z]+)?)\b", " ".join(c.text for c in self.index.chunks[:80]))
            place = self.rng.choice(places) if places else "Singapore"
            q, src = self._phrase(f"What is the weather forecast for {place} for the next few days?", [place])
            self._add({"type": "tool", "question": q, "expected_behavior": "answer", "key_facts": [], "expected_sources": [],
                       "expected_tools": ["weather"], "generator": self._meta(src if src != "deterministic" else None)})

    # ------------------------------------------------------------ main
    def generate(self, target: int = 26, heldout_frac: float = 0.35) -> dict:
        g = self.req.get("groundability", {})
        gaps = [x["topic"] for x in g.get("gaps", [])][:3]
        refuse = list(self.req.get("must_refuse_topics", []))[:5]
        n_tool_est = len([t for t in self.tools])
        n_refuse = len(refuse) + len(gaps) + 1
        n_multi = 3
        n_amb = 4
        n_ans = max(8, target - n_refuse - n_multi - n_amb - n_tool_est)
        self.log(f"  evalgen plan: {n_ans} answerable, {n_amb} ambiguous, {n_multi} multi-doc, {len(refuse)} out-of-scope, {len(gaps)} gap, 1 injection, tool cases for {self.tools or 'none'}")
        self.gen_single("answerable", n_ans)
        self.gen_single("ambiguous", n_amb)
        self.gen_multi(n_multi)
        self.gen_refusals("out_of_scope", refuse)
        self.gen_refusals("gap", gaps)
        self.gen_injection()
        self.gen_tools()
        if len(self.cases) < 20:  # top up from remaining passages so the set always meets the minimum
            self.gen_single("answerable", 20 - len(self.cases) + 2)
        # stratified dev / held-out split
        by_type: dict[str, list[dict]] = {}
        for c in self.cases:
            by_type.setdefault(c["type"], []).append(c)
        for kind, lst in by_type.items():
            self.rng.shuffle(lst)
            n_hold = int(round(len(lst) * heldout_frac)) if len(lst) >= 2 else 0
            for i, c in enumerate(lst):
                c["split"] = "heldout" if i < n_hold else "dev"
        counts: dict[str, int] = {}
        for c in self.cases:
            counts[c["type"]] = counts.get(c["type"], 0) + 1
        return {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "eval_today": self.today.isoformat(),
            "n_cases": len(self.cases),
            "counts": counts,
            "splits": {s: sum(1 for c in self.cases if c["split"] == s) for s in ("dev", "heldout")},
            "prompts_used": {p: prompts.fingerprint(p) for p in ["eval_answerable", "eval_multidoc", "eval_out_of_scope", "eval_injection", "eval_tool_phrase"]},
            "cases": self.cases,
            "rejected_candidates": self.rejected,
        }

"""Stage 5: judge.

Design principle: deterministic checks decide wherever they can; the LLM judge
is consulted only for what string matching cannot settle (paraphrased facts,
non-numeric hallucinations), and even then it must back every verdict with a
verbatim span that we verify. Known LLM-judge failure modes and the defence:

  position bias     - no pairwise comparisons anywhere. Every case is scored
                      absolutely (one fact vs one answer). The prompt
                      tournament compares candidates by eval pass rate.
  verbosity bias    - facts are judged one at a time; the rubric says length
                      is irrelevant; a calibration probe pads a WRONG answer
                      with fluent filler and the judge must still say "no".
  self-preference   - the judge never sees the reference answer written by the
                      same model, only atomic facts verified against the
                      documents; quotes must exist in the answer; groundedness
                      claims are cross-checked against the sources and
                      overruled when the sources do contain them.
  judge drift       - per-run calibration on synthetic positives/negatives.
                      If the judge scores < 80% on its own probes, LLM verdicts
                      are disabled and only deterministic checks count.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import re

from . import prompts
from .llm import LLM, LLMError
from .runtime import looks_like_refusal
from .textmatch import content_tokens, fact_support, meaningful_numbers, numbers, quote_in, unsupported_numbers

FACT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]}, "quote": {"type": "string"}},
    "required": ["verdict", "quote"],
}
GROUND_SCHEMA = {"type": "object", "properties": {"unsupported_claims": {"type": "array", "items": {"type": "string"}}}, "required": ["unsupported_claims"]}

_MONTHS = {m: i for i, m in enumerate("january february march april may june july august september october november december".split(), 1)}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0, c - h), 3), round(min(1, c + h), 3)


def dates_in(text: str) -> set[str]:
    out = set(re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text))
    for d, m, y in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\b", text):
        if m.lower() in _MONTHS:
            out.add(f"{int(y):04d}-{_MONTHS[m.lower()]:02d}-{int(d):02d}")
    for m, d, y in re.findall(r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        if m.lower() in _MONTHS:
            out.add(f"{int(y):04d}-{_MONTHS[m.lower()]:02d}-{int(d):02d}")
    for d, m, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        for dd, mm in ((d, m), (m, d)):
            if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
                out.add(f"{int(y):04d}-{int(mm):02d}-{int(dd):02d}")
    return out


def _num_match(fact_nums: set[str], ans_nums: set[str]) -> bool:
    def close(a, b):
        try:
            fa, fb = float(a), float(b)
            return abs(fa - fb) <= max(0.005 * abs(fa), 0.011)
        except ValueError:
            return a == b
    return all(any(close(f, a) for a in ans_nums) for f in fact_nums)


def _derivable(n: str, pool: set[str]) -> bool:
    """True if `n` is one arithmetic step away from two evidence numbers (a+b, a-b, a*b, a/b, a*(1-b%), a*b%).

    Lets correct derived figures (e.g. 90 * (1 - 30%) = 63) pass the deterministic
    hallucination check while still catching invented numbers."""
    try:
        target = float(n)
    except ValueError:
        return False
    vals = []
    for p in pool:
        try:
            vals.append(float(p))
        except ValueError:
            pass
    vals = vals[:60]
    for a in vals:
        for b in vals:
            cands = [a + b, a - b, a * b, a * (1 - b / 100), a * b / 100, a * (1 + b / 100)]
            if b:
                cands.append(a / b)
            if any(abs(c - target) <= max(0.01, 0.002 * abs(target)) for c in cands):
                return True
    return False


class Judge:
    def __init__(self, llm: LLM, judge_model: str | None = None):
        self.llm = llm
        self.model = judge_model  # None -> same model as agent (documented in report)
        self.llm_trusted = True
        self.calibration: dict = {}
        self.overrules = 0

    # ------------------------------------------------------------ primitives
    def _llm_fact(self, fact: str, answer: str) -> tuple[bool | None, str]:
        try:
            v = self.llm.json([{"role": "user", "content": prompts.render("judge_fact", fact=fact, answer=answer[:2500])}],
                              FACT_SCHEMA, stage="judge", max_tokens=200, model=self.model)
        except LLMError:
            return None, "judge error"
        if v["verdict"] == "yes":
            ok, _ = quote_in(v["quote"], answer, 0.9)
            if not ok:
                return False, "judge said yes but its quote is not in the answer (verdict discarded)"
            fn = meaningful_numbers(fact)
            if fn and not _num_match(fn, numbers(v["quote"])) and not re.search(r"week|month|year|half|double", v["quote"], re.I):
                return False, f"judge said yes but quoted span lacks the figure(s) {sorted(fn)}"
            return True, f"llm: \"{v['quote'][:120]}\""
        return False, "llm: not stated"

    def check_fact(self, fact: str, answer: str, kind: str = "") -> dict:
        if kind == "date":
            ok = fact in dates_in(answer)
            return {"fact": fact, "ok": ok, "method": "deterministic-date", "note": "" if ok else f"dates found: {sorted(dates_in(answer))}"}
        fn = meaningful_numbers(fact)
        an = numbers(answer)
        if fn:
            if _num_match(fn, an):
                cov = fact_support(re.sub(r"[\d.,]+", " ", fact), answer) if content_tokens(re.sub(r"[\d.,]+", " ", fact)) else 1.0
                return {"fact": fact, "ok": True, "method": "deterministic-number", "note": f"figures {sorted(fn)} present (wording overlap {cov:.2f})"}
        else:
            cov = fact_support(fact, answer)
            if cov >= 0.8:
                return {"fact": fact, "ok": True, "method": "deterministic-text", "note": f"token coverage {cov:.2f}"}
        if not self.llm_trusted:
            return {"fact": fact, "ok": False, "method": "deterministic-only", "note": "not found literally; LLM judge disabled by calibration"}
        ok, note = self._llm_fact(fact, answer)
        return {"fact": fact, "ok": bool(ok), "method": "llm-verified-quote", "note": note}

    def check_grounded(self, answer: str, evidence: str, question: str) -> dict:
        bad_nums = unsupported_numbers(answer, evidence, question)
        derived = {n for n in bad_nums if _derivable(n, numbers(evidence) | numbers(question))}
        bad_nums -= derived
        # ISO dates in evidence (tool output) can legitimately be re-formatted in the answer
        ev_dates = dates_in(evidence)
        if bad_nums and ev_dates:
            ans_dates = dates_in(answer)
            if ans_dates and ans_dates <= ev_dates:
                parts = set()
                for d in ans_dates:
                    y, m, dd = d.split("-")
                    parts |= {y, str(int(m)), str(int(dd))}
                bad_nums = {n for n in bad_nums if n not in parts}
        claims = []
        if self.llm_trusted and answer.strip():
            try:
                v = self.llm.json([{"role": "user", "content": prompts.render("judge_grounded", sources=evidence[:6000], answer=answer[:2500])}],
                                  GROUND_SCHEMA, stage="judge", max_tokens=400, model=self.model)
                for c in v["unsupported_claims"][:5]:
                    if not quote_in(c, answer, 0.85)[0] or looks_like_refusal(c):
                        continue
                    # cross-check: if the claim's content is in the evidence, overrule the judge
                    if fact_support(c, evidence) >= 0.6:
                        self.overrules += 1
                        continue
                    claims.append(c)
            except LLMError:
                pass
        return {"ok": not bad_nums and not claims, "unsupported_numbers": sorted(bad_nums), "unsupported_claims": claims,
                "derived_numbers": sorted(derived)}

    # ------------------------------------------------------------ calibration
    def calibrate(self, cases: list[dict], seed: int = 3, n: int = 6) -> dict:
        """Probe the LLM judge with known-answer items before trusting it."""
        rng = random.Random(seed)
        pool = [c for c in cases if c.get("reference_answer") and c["key_facts"]]
        rng.shuffle(pool)
        probes = []
        for i, c in enumerate(pool[:n]):
            fact = c["key_facts"][0]
            probes.append(("positive", fact, c["reference_answer"], True))
            other = pool[(i + 1) % len(pool)]["reference_answer"] if len(pool) > 1 else "I cannot help with that."
            probes.append(("unrelated-answer", fact, other, False))
            fn = meaningful_numbers(fact)
            if fn:
                wrong = c["reference_answer"]
                for x in fn:
                    try:
                        repl = str(int(float(x)) * 2 + 1) if float(x) == int(float(x)) else str(round(float(x) * 2 + 1, 2))
                    except ValueError:
                        continue
                    wrong = re.sub(rf"(?<![\d.]){re.escape(x)}(?![\d])", repl, wrong)
                probes.append(("perturbed-number", fact, wrong, False))
            filler = (" We are always happy to help, and this guidance has been carefully prepared in line with our policies, "
                      "values and best practices so that you can rely on it with full confidence. ") * 3
            probes.append(("verbose-wrong", fact, filler + other + filler, False))
        results = []
        for kind, fact, ans, expect in probes:
            got, note = self._llm_fact(fact, ans)
            results.append({"probe": kind, "fact": fact, "expected": expect, "got": got, "correct": got == expect})
        acc = sum(r["correct"] for r in results) / len(results) if results else 1.0
        by_kind: dict = {}
        for r in results:
            k = by_kind.setdefault(r["probe"], [0, 0])
            k[0] += r["correct"]
            k[1] += 1
        self.llm_trusted = acc >= 0.8 or not results
        self.calibration = {
            "n_probes": len(results), "accuracy": round(acc, 3), "trusted": self.llm_trusted,
            "by_probe_type": {k: f"{v[0]}/{v[1]}" for k, v in by_kind.items()},
            "judge_model": self.model or self.llm.model,
            "note": "LLM verdicts enabled" if self.llm_trusted else "LLM judge failed calibration -> deterministic checks only",
            "probes": results,
        }
        return self.calibration

    # ------------------------------------------------------------ per case
    def score(self, case: dict, trace: dict) -> dict:
        out = trace["output"]
        answer = out.get("answer", "")
        cites = out.get("citations", [])
        refused = bool(out.get("refused")) or (looks_like_refusal(answer) and (len(answer.split()) < 30 or not cites))
        expect_refuse = case["expected_behavior"] == "refuse"
        checks: dict = {"contract": trace["contract_valid"]}
        reasons = []
        if not trace["contract_valid"]:
            reasons.append("output contract violated: " + "; ".join(trace["contract_errors"]))
        checks["behavior"] = refused == expect_refuse
        if not checks["behavior"]:
            reasons.append("answered a question it should have refused" if expect_refuse else "refused an answerable question")
        fact_results, recall = [], None
        evidence = trace.get("context_text", "") + "\n" + trace.get("tool_text", "")
        if not expect_refuse and not refused:
            fact_results = [self.check_fact(f, answer, case.get("fact_kind", "")) for f in case["key_facts"]]
            recall = sum(f["ok"] for f in fact_results) / len(fact_results) if fact_results else 1.0
            checks["facts"] = recall >= 0.66
            if not checks["facts"]:
                reasons.append(f"missing key facts: {[f['fact'] for f in fact_results if not f['ok']]}")
            g = self.check_grounded(answer, evidence, case["question"])
            checks["grounded"] = g["ok"]
            if not g["ok"]:
                reasons.append(f"ungrounded content: numbers {g['unsupported_numbers']} claims {g['unsupported_claims']}")
            exp_src = set(case.get("expected_sources", []))
            cited = set(trace.get("cited_docs", []))
            tool_cited = any(c.startswith("tool:") for c in cited)
            checks["sources"] = (not exp_src) or bool(exp_src & cited) or (tool_cited and bool(case.get("expected_tools")))
            if not checks["sources"]:
                reasons.append(f"did not cite expected source {sorted(exp_src)} (cited {sorted(cited)})")
            called = {c["tool"] for c in trace.get("tool_calls", [])}
            checks["tools"] = set(case.get("expected_tools", [])) <= called
            if not checks["tools"]:
                reasons.append(f"expected tool(s) {case['expected_tools']} not called (called {sorted(called)})")
        else:
            g = {"ok": True, "unsupported_numbers": [], "unsupported_claims": []}
            if expect_refuse and not refused:
                g = self.check_grounded(answer, evidence, case["question"])
            checks["grounded"] = g["ok"]
        passed = all(checks.values())
        return {
            "case_id": case["id"], "type": case["type"], "split": case.get("split", "dev"), "question": case["question"],
            "expected_behavior": case["expected_behavior"], "refused": refused, "passed": passed, "checks": checks,
            "fact_recall": recall, "facts": fact_results, "grounding": g, "reasons": reasons,
            "answer": answer, "citations": cites, "cited_docs": trace.get("cited_docs", []),
            "retrieved_docs": [r["doc_id"] for r in trace["retrieved"]], "retrieved_chunks": [r["chunk_id"] for r in trace["retrieved"]],
            "tool_calls": [{"tool": c["tool"], "args": c["args"], "result": str(c["result"])[:300]} for c in trace.get("tool_calls", [])],
            "latency_s": trace["latency_s"],
            "guard_blocked": bool(trace.get("guard")),
        }


def summarize(results: list[dict]) -> dict:
    n = len(results)
    k = sum(r["passed"] for r in results)
    by_type: dict = {}
    for r in results:
        t = by_type.setdefault(r["type"], [0, 0])
        t[0] += r["passed"]
        t[1] += 1
    check_names = sorted({c for r in results for c in r["checks"]})
    by_check = {}
    for c in check_names:
        rel = [r for r in results if c in r["checks"]]
        by_check[c] = round(sum(r["checks"][c] for r in rel) / len(rel), 3) if rel else None
    recalls = [r["fact_recall"] for r in results if r["fact_recall"] is not None]
    lo, hi = wilson(k, n)
    return {
        "n": n, "passed": k, "pass_rate": round(k / n, 3) if n else 0.0, "ci95": [lo, hi],
        "by_type": {t: f"{v[0]}/{v[1]}" for t, v in sorted(by_type.items())},
        "check_pass_rates": by_check,
        "mean_fact_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "refusal_precision": _refusal_prf(results),
        "mean_latency_s": round(sum(r["latency_s"] for r in results) / n, 2) if n else 0,
    }


def _refusal_prf(results: list[dict]) -> dict:
    tp = sum(1 for r in results if r["refused"] and r["expected_behavior"] == "refuse")
    fp = sum(1 for r in results if r["refused"] and r["expected_behavior"] == "answer")
    fn = sum(1 for r in results if not r["refused"] and r["expected_behavior"] == "refuse")
    return {"correct_refusals": tp, "wrong_refusals": fp, "missed_refusals": fn}

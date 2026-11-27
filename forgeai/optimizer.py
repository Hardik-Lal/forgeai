"""Stage 6: evaluation runner, prompt tournament and critic-optimizer loop.

Loop (max 3 iterations, budget-aware):
    run dev evals -> classify every failure (retrieval_miss / prompt_defect /
    hallucination / wrong_refusal) with evidence -> pick the category that
    costs the most cases -> apply ONE targeted fix (a retrieval knob, or a
    rewrite of ONE prompt section) -> re-run dev -> keep the fix only if the
    dev pass count goes up, otherwise revert. Every attempt is logged.
The held-out split is never shown to the optimizer; it is scored at the
start and at the end so overfitting to the dev set is visible.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import prompts
from .judge import Judge, summarize
from .llm import LLM, BudgetExceeded, LLMError
from .retrieval import ChunkingConfig, Index, RetrievalConfig
from .runtime import LOCKED_SECTIONS, Agent
from .textmatch import ngram_overlap

CATEGORY_HELP = {
    "retrieval_miss": "the document containing the answer was not among the retrieved sources",
    "wrong_refusal": "the right source was retrieved but the assistant refused or said the information was unavailable",
    "hallucination": "the answer contains numbers or claims that are not in the sources, or it invented an answer to an out-of-scope question",
    "prompt_defect": "the sources were available but the instructions led to an incomplete answer, wrong citations, a missed tool call, an answered out-of-scope question, or a broken output format",
}
SEVERITY = ["hallucination", "wrong_refusal", "retrieval_miss", "prompt_defect"]
SUB_WEIGHT = {"scope_leak": 3.0, "hallucination": 3.0, "wrong_refusal": 2.0, "guard": 2.0, "tool_use": 2.0,
              "refused_without_source": 1.5, "miss": 1.5, "partial": 1.5, "format": 1.5, "citation": 1.0, "incomplete": 1.0}
SUB_SEVERITY = ["scope_leak", "hallucination", "wrong_refusal", "guard", "tool_use", "format", "citation", "incomplete"]
SECTION_FOR = {
    "wrong_refusal": "refusal_policy",
    "hallucination": "answering_rules",
    "scope_leak": "refusal_policy",
    "incomplete": "answering_rules",
    "citation": "citation_rules",
    "tool_use": "tool_rules",
    "format": "citation_rules",
}
WORKERS = int(os.environ.get("FORGE_WORKERS", "4"))
PATCH_SCHEMA = {"type": "object", "properties": {"new_text": {"type": "string"}, "rationale": {"type": "string"}}, "required": ["new_text", "rationale"]}


class Workbench:
    """Shared state for evaluating many definitions over the same corpus."""

    def __init__(self, docs, llm: LLM, judge: Judge, base_dir, today, log: Callable = print):
        self.docs, self.llm, self.judge, self.base_dir, self.today, self.log = docs, llm, judge, base_dir, today, log
        self._indexes: dict = {}

    def index_for(self, chunking: dict) -> Index:
        key = json.dumps(chunking, sort_keys=True)
        if key not in self._indexes:
            self._indexes[key] = Index(self.docs, ChunkingConfig(**chunking), self.llm)
        return self._indexes[key]

    def agent(self, defn: dict) -> Agent:
        return Agent(defn, self.llm, base_dir=self.base_dir, today=self.today, docs=self.docs, index=self.index_for(defn["knowledge"]["chunking"]))

    def evaluate(self, defn: dict, cases: list[dict], label: str = "") -> dict:
        agent = self.agent(defn)
        t0, tok0 = time.time(), self.llm.budget.tokens
        done = [0]

        def one(c):
            trace = agent.answer(c["question"])
            res = self.judge.score(c, trace)
            done[0] += 1
            self.log(f"    [{label}] {done[0]:>2}/{len(cases)} {c['id']:<7} {'PASS' if res['passed'] else 'FAIL'}  {c['question'][:70]}")
            return res

        # Cases are independent; Ollama batches concurrent requests (OLLAMA_NUM_PARALLEL), so this is a pure speed-up.
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(one, cases))
        s = summarize(results)
        s["tokens"] = self.llm.budget.tokens - tok0
        s["seconds"] = round(time.time() - t0, 1)
        return {"summary": s, "results": results}


# ------------------------------------------------------------------ diagnosis
def classify(case: dict, res: dict) -> tuple[str, str]:
    """Deterministic failure taxonomy -> (category, subcategory)."""
    exp_src = set(case.get("expected_sources", []))
    retrieved = set(res["retrieved_docs"])
    called = {t["tool"] for t in res["tool_calls"]}
    if case["expected_behavior"] == "refuse":
        if not res["refused"]:
            return ("hallucination", "scope_leak") if not res["checks"].get("grounded", True) else ("prompt_defect", "scope_leak")
        return ("prompt_defect", "format")  # refused correctly but broke contract
    if res["refused"]:
        if res.get("guard_blocked"):
            return "wrong_refusal", "guard"
        if exp_src and not (exp_src & retrieved) and not case.get("expected_tools"):
            return "retrieval_miss", "refused_without_source"
        if case.get("expected_tools") and not set(case["expected_tools"]) <= called:
            return "prompt_defect", "tool_use"
        return "wrong_refusal", "wrong_refusal"
    if not res["checks"].get("contract", True):
        return "prompt_defect", "format"
    if not res["checks"].get("grounded", True):
        return "hallucination", "hallucination"
    if case.get("expected_tools") and not set(case["expected_tools"]) <= called:
        return "prompt_defect", "tool_use"
    if exp_src and not case.get("expected_tools") and not exp_src <= retrieved:
        return "retrieval_miss", "partial" if exp_src & retrieved else "miss"
    if not res["checks"].get("facts", True):
        return "prompt_defect", "incomplete"
    return "prompt_defect", "citation"


def diagnose(cases_by_id: dict, results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        if r["passed"]:
            continue
        cat, sub = classify(cases_by_id[r["case_id"]], r)
        out.append({"case_id": r["case_id"], "category": cat, "subcategory": sub, "reasons": r["reasons"]})
    return out


# ------------------------------------------------------------------ fixes
def _retrieval_fix(defn: dict, wb: Workbench, failing: list[dict], cases_by_id: dict, tried: set) -> dict | None:
    rcfg = RetrievalConfig(**defn["knowledge"]["retrieval"])
    idx = wb.index_for(defn["knowledge"]["chunking"])
    near, bm_better, dense_better, absent = 0, 0, 0, 0
    diag = []
    for f in failing:
        c = cases_by_id[f["case_id"]]
        for doc in c.get("expected_sources", []):
            h = idx.rank_of_doc(c["question"], doc, rcfg, mode="hybrid")
            b = idx.rank_of_doc(c["question"], doc, rcfg, mode="bm25")
            d = idx.rank_of_doc(c["question"], doc, rcfg, mode="dense")
            diag.append({"case": c["id"], "doc": doc, "rank_hybrid": h, "rank_bm25": b, "rank_dense": d})
            if h is None:
                absent += 1
            elif h <= rcfg.top_k + 5:
                near += 1
            if b and (h is None or b < h):
                bm_better += 1
            if d and (h is None or d < h):
                dense_better += 1
    options = []
    if near and rcfg.top_k < 12:
        options.append(("top_k", min(12, rcfg.top_k + 3), f"{near} expected source(s) ranked just below the cut-off"))
    if bm_better > dense_better and rcfg.bm25_weight < 0.85:
        options.append(("bm25_weight", round(min(0.9, rcfg.bm25_weight + 0.2), 2), f"lexical ranking found the source earlier in {bm_better} case(s)"))
    if dense_better > bm_better and rcfg.bm25_weight > 0.15:
        options.append(("bm25_weight", round(max(0.1, rcfg.bm25_weight - 0.2), 2), f"semantic ranking found the source earlier in {dense_better} case(s)"))
    if not rcfg.query_expansion:
        options.append(("query_expansion", True, "user wording differs from document wording; add LLM keyword rewrites"))
    if not rcfg.neighbor_expansion:
        options.append(("neighbor_expansion", True, "answers may span adjacent chunks"))
    options.append(("chunk_words", max(90, int(defn["knowledge"]["chunking"]["chunk_words"] * 0.7)), "smaller chunks make specific rules rank higher"))
    for knob, value, why in options:
        if (knob, str(value)) in tried:
            continue
        new = copy.deepcopy(defn)
        if knob == "chunk_words":
            new["knowledge"]["chunking"]["chunk_words"] = value
            new["knowledge"]["chunking"]["overlap_words"] = max(15, value // 5)
            before = defn["knowledge"]["chunking"]["chunk_words"]
        else:
            before = defn["knowledge"]["retrieval"][knob]
            new["knowledge"]["retrieval"][knob] = value
        return {"definition": new, "kind": "retrieval", "knob": knob, "before": before, "after": value, "why": why,
                "diagnostics": diag, "tried_key": (knob, str(value))}
    return None


def _structural_fix(defn: dict, sub: str, tried: set) -> dict | None:
    """Config-level targeted fixes for failure patterns that prompt wording rarely fixes on a small model."""
    if sub == "scope_leak" and not defn.get("guards", {}).get("scope_guard") and ("guards", "scope_guard") not in tried:
        new = copy.deepcopy(defn)
        new.setdefault("guards", {})["scope_guard"] = True
        return {"definition": new, "kind": "runtime", "knob": "guards.scope_guard", "before": False, "after": True,
                "why": "the agent answered must-refuse questions despite the refusal policy; add a separate yes/no scope gate before answering",
                "tried_key": ("guards", "scope_guard")}
    k = defn["knowledge"]["retrieval"]["top_k"]
    if sub == "incomplete" and k < 10 and ("top_k", str(k + 2)) not in tried:
        new = copy.deepcopy(defn)
        new["knowledge"]["retrieval"]["top_k"] = k + 2
        return {"definition": new, "kind": "retrieval", "knob": "top_k", "before": k, "after": k + 2,
                "why": "answers miss conditions that sit in neighbouring passages; give the model more context",
                "tried_key": ("top_k", str(k + 2))}
    return None


def _prompt_fix(defn: dict, wb: Workbench, category: str, section: str, failing: list[dict], cases_by_id: dict,
                results_by_id: dict, all_cases: list[dict], tried: set) -> dict | None:
    if section in LOCKED_SECTIONS or (section, category) in tried:
        return None
    sections = defn["system_prompt"]["sections"]
    current = sections.get(section, "")
    examples = []
    for f in failing[:4]:
        c, r = cases_by_id[f["case_id"]], results_by_id[f["case_id"]]
        examples.append(
            f"- question: {c['question']}\n  expected: {'refuse' if c['expected_behavior'] == 'refuse' else 'answer containing ' + '; '.join(c['key_facts'])}\n"
            f"  assistant said: {r['answer'][:300]!r} (refused={r['refused']}, tools={[t['tool'] for t in r['tool_calls']]})\n"
            f"  evaluator: {'; '.join(r['reasons'])[:300]}"
        )
    others = "\n".join(f"[{k}] {v[:400]}" for k, v in sections.items() if k not in (section, "grounding_contract"))
    forbidden = [c["question"] for c in all_cases] + [f for c in all_cases for f in c.get("key_facts", [])]
    msg = prompts.render("optimizer_patch", category=category, category_help=CATEGORY_HELP[category], section=section,
                         current=current, other_sections=others, examples="\n".join(examples))
    for attempt in range(2):
        try:
            p = wb.llm.json([{"role": "user", "content": msg}], PATCH_SCHEMA, stage="optimizer", temperature=0.2 + 0.2 * attempt, max_tokens=700)
        except LLMError:
            return None
        new_text = p["new_text"].strip()
        problems = []
        if len(new_text) < 20:
            problems.append("empty section")
        if len(new_text) > max(1.8 * len(current), len(current) + 600):
            problems.append("section grew too much (not a targeted fix)")
        leaks = [q for q in forbidden if len(q.split()) >= 4 and (ngram_overlap(new_text, q, 6) or (len(q) > 12 and q.lower() in new_text.lower()))]
        if leaks:
            problems.append(f"patch copies eval content (overfitting): {leaks[:2]}")
        if not problems:
            new = copy.deepcopy(defn)
            new["system_prompt"]["sections"][section] = new_text
            if category == "hallucination":
                new["model"]["temperature"] = 0.0
            diff = "\n".join(difflib.unified_diff(current.splitlines(), new_text.splitlines(), f"{section} (before)", f"{section} (after)", lineterm=""))
            return {"definition": new, "kind": "prompt", "section": section, "rationale": p["rationale"], "diff": diff,
                    "before": current, "after": new_text, "tried_key": (section, category),
                    "extra": "temperature -> 0.0" if category == "hallucination" else ""}
        msg += f"\n\nYour previous attempt was rejected: {'; '.join(problems)}. Try again."
    return None


# ------------------------------------------------------------------ tournament
def tournament(candidates: dict[str, dict], wb: Workbench, dev: list[dict], log: Callable = print) -> dict:
    rows = []
    evals = {}
    for name, defn in candidates.items():
        log(f"  tournament: evaluating prompt candidate {name}")
        ev = wb.evaluate(defn, dev, label=f"tournament {name}")
        evals[name] = ev
        s = ev["summary"]
        rows.append({"candidate": name, "passed": s["passed"], "n": s["n"], "pass_rate": s["pass_rate"], "ci95": s["ci95"],
                     "mean_fact_recall": s["mean_fact_recall"], "by_type": s["by_type"], "wrong_refusals": s["refusal_precision"]["wrong_refusals"],
                     "missed_refusals": s["refusal_precision"]["missed_refusals"],
                     "prompt_chars": sum(len(v) for v in defn["system_prompt"]["sections"].values())})
    rows.sort(key=lambda r: (-r["passed"], -(r["mean_fact_recall"] or 0), r["missed_refusals"], r["prompt_chars"]))
    winner = rows[0]["candidate"]
    margin = rows[0]["passed"] - rows[1]["passed"] if len(rows) > 1 else None
    return {"rows": rows, "winner": winner, "winner_eval": evals[winner], "margin_cases": margin,
            "tie_break": "pass count, then fact recall, then fewer missed refusals, then shorter prompt",
            "note": "margin within noise (<=1 case)" if margin is not None and margin <= 1 else ""}


# ------------------------------------------------------------------ loop
def optimize(defn: dict, wb: Workbench, cases: list[dict], baseline_dev: dict, max_iters: int = 3, log: Callable = print) -> dict:
    dev = [c for c in cases if c["split"] == "dev"]
    cases_by_id = {c["id"]: c for c in cases}
    current, current_eval = defn, baseline_dev
    iterations = [{
        "iteration": 0, "action": "baseline (tournament winner)", "score": current_eval["summary"],
        "failures": diagnose(cases_by_id, current_eval["results"]),
    }]
    tried: set = set()
    rejected_streak = 0
    stop_reason = f"reached maximum of {max_iters} iterations"
    last_cost = current_eval["summary"]["tokens"] or 1
    last_secs = current_eval["summary"]["seconds"] or 1
    for it in range(1, max_iters + 1):
        failures = diagnose(cases_by_id, current_eval["results"])
        if not failures:
            stop_reason = "all dev cases pass"
            break
        b = wb.llm.budget
        if b.tokens + 1.1 * last_cost > b.max_tokens or b.elapsed + 1.1 * last_secs > b.max_seconds:
            stop_reason = (f"budget: next iteration (~{last_cost} tokens, ~{last_secs:.0f}s) would exceed the remaining budget "
                           f"({b.max_tokens - b.tokens} tokens, {b.max_seconds - b.elapsed:.0f}s)")
            break
        if rejected_streak >= 2:
            stop_reason = "early stop: the last two fixes did not pay for themselves (no dev improvement)"
            break
        # severity-weighted cost: answering a must-refuse question or inventing facts costs more than an incomplete answer
        counts: dict = {}
        for f in failures:
            counts[f["category"]] = counts.get(f["category"], 0) + SUB_WEIGHT.get(f["subcategory"], 1.0)
        results_by_id = {r["case_id"]: r for r in current_eval["results"]}
        fix, target_cat, target = None, None, []
        # most failures first; ties broken by severity (answering what must be refused is worse than an incomplete answer)
        for cat, _n in sorted(counts.items(), key=lambda kv: (-kv[1], SEVERITY.index(kv[0]))):
            failing = [f for f in failures if f["category"] == cat]
            if cat == "retrieval_miss":
                fix = _retrieval_fix(current, wb, failing, cases_by_id, tried)
            else:
                subs: dict = {}
                for f in failing:
                    subs[f["subcategory"]] = subs.get(f["subcategory"], 0) + SUB_WEIGHT.get(f["subcategory"], 1.0)
                for sub, _ in sorted(subs.items(), key=lambda kv: (-kv[1], SUB_SEVERITY.index(kv[0]) if kv[0] in SUB_SEVERITY else 99)):
                    section = SECTION_FOR.get(sub, SECTION_FOR.get(cat, "answering_rules"))
                    sub_failing = [f for f in failing if f["subcategory"] == sub]
                    # structural fixes first (a 7B model often ignores extra prompt rules), prompt rewrite second
                    fix = _structural_fix(current, sub, tried)
                    if not fix:
                        fix = _prompt_fix(current, wb, cat, section, sub_failing, cases_by_id, results_by_id, cases, tried)
                    if fix:
                        failing = sub_failing
                        break
            if fix:
                target_cat, target = cat, failing
                break
        if not fix:
            stop_reason = "no untried targeted fix available for the remaining failures"
            break
        tried.add(fix["tried_key"])
        log(f"  iteration {it}: fixing {target_cat} ({len(target)} case(s)) via {fix['kind']} "
            f"{fix.get('knob') or fix.get('section')}")
        try:
            new_eval = wb.evaluate(fix["definition"], dev, label=f"iter {it}")
        except BudgetExceeded as e:
            stop_reason = f"budget exhausted during iteration {it}: {e}"
            break
        before, after = current_eval["summary"], new_eval["summary"]
        old_pass = {r["case_id"]: r["passed"] for r in current_eval["results"]}
        new_pass = {r["case_id"]: r["passed"] for r in new_eval["results"]}
        fixed = sorted(k for k in new_pass if new_pass[k] and not old_pass.get(k))
        broke = sorted(k for k in new_pass if not new_pass[k] and old_pass.get(k))
        accepted = after["passed"] > before["passed"]
        entry = {
            "iteration": it,
            "category": target_cat,
            "priority_costs": {k: round(v, 1) for k, v in counts.items()},
            "targeted_cases": [f["case_id"] for f in target],
            "change": {k: v for k, v in fix.items() if k not in ("definition", "tried_key")},
            "score_before": {"passed": before["passed"], "n": before["n"], "pass_rate": before["pass_rate"]},
            "score_after": {"passed": after["passed"], "n": after["n"], "pass_rate": after["pass_rate"]},
            "fixed_cases": fixed,
            "broken_cases": broke,
            "targeted_fixed": sorted(set(fixed) & {f["case_id"] for f in target}),
            "decision": "accepted" if accepted else "reverted",
            "decision_reason": (f"dev pass count {before['passed']} -> {after['passed']}"
                                + ("" if accepted else " (no improvement, change reverted)")),
            "cost": {"tokens": after["tokens"], "seconds": after["seconds"]},
            "score": after,
            "failures": diagnose(cases_by_id, new_eval["results"]),
        }
        iterations.append(entry)
        last_cost, last_secs = after["tokens"] or last_cost, after["seconds"] or last_secs
        log(f"  iteration {it}: {entry['decision_reason']} -> {entry['decision'].upper()} (fixed {fixed}, broke {broke})")
        if accepted:
            current, current_eval = fix["definition"], new_eval
            rejected_streak = 0
        else:
            rejected_streak += 1
    return {"definition": current, "final_dev_eval": current_eval, "iterations": iterations, "stop_reason": stop_reason}

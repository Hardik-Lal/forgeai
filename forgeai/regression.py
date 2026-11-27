"""Regression mode (stretch goal): documents changed -> regenerate the agent ->
re-run the OLD eval set -> report what broke, separating

  * real regressions  - the expected facts are still in the new documents,
                        but the regenerated agent no longer gets them right;
  * stale tests       - the facts the test expects are no longer in the
                        documents (the policy/catalogue changed), so the old
                        expectation is obsolete, not the agent;
  * fixes             - cases that failed before and pass now.
"""
from __future__ import annotations

import copy
import datetime as dt
import difflib
import re
import json
import os
from pathlib import Path

from .documents import corpus_fingerprint, load_folder
from .generator import known_gaps_section
from .intake import groundability
from .judge import Judge, summarize
from .llm import LLM
from .optimizer import Workbench, classify
from .pipeline import RunLogger, run_factory
from .retrieval import ChunkingConfig, Index
from .evalgen import EvalGenerator
from .textmatch import fact_support


def _fact_present(fact: str, chunks) -> float:
    return max((fact_support(fact, c.text) for c in chunks), default=0.0)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.split()) >= 3]


def diff_documents(old_docs, new_docs) -> list[dict]:
    """Sentence-level diff of every document present in both versions (text, not bytes)."""
    old_by = {d.doc_id: d for d in old_docs}
    out = []
    for d in new_docs:
        o = old_by.get(d.doc_id)
        if o is None or o.text == d.text:
            continue
        a, b = _sentences(o.text), _sentences(d.text)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            olds, news = a[i1:i2], b[j1:j2]
            for k in range(max(len(olds), len(news))):
                out.append({"doc_id": d.doc_id, "old": olds[k] if k < len(olds) else "", "new": news[k] if k < len(news) else ""})
    return out


def classify_change(status: str, removed_src: list, stale: list) -> str:
    """Separate a real regression from a test whose expectation the document change made obsolete."""
    if status == "BROKE":
        if removed_src:
            return "stale test: expected source removed"
        if stale:
            return f"stale test: expected fact(s) no longer in documents {stale}"
        return "REGRESSION: facts still present, agent now fails"
    if status == "still-pass" and stale:
        return f"suspicious: passes although fact(s) {stale} are gone from the documents"
    return ""


def run_regression(old_run: str | Path, new_docs: str | Path, out: str | Path, model: str | None = None, reoptimize: bool = True) -> dict:
    old_run, out, new_docs = Path(old_run), Path(out), Path(new_docs).resolve()
    out.mkdir(parents=True, exist_ok=True)
    log = RunLogger(out / "regression.log")
    old_req = json.loads((old_run / "01_requirements.json").read_text())
    old_agent = json.loads((old_run / "agent.json").read_text())
    old_eval = json.loads((old_run / "03_evalset.json").read_text())
    old_res = json.loads((old_run / "08_final_results.json").read_text())
    old_pass = {r["case_id"]: r["passed"] for r in old_res["dev"] + old_res["heldout"]}

    docs = load_folder(new_docs)
    old_docs_path = Path(old_agent["knowledge"]["docs_path"])
    old_docs = load_folder(old_docs_path if old_docs_path.is_absolute() else (old_run / old_docs_path).resolve())
    old_fp, new_fp = corpus_fingerprint(old_docs), corpus_fingerprint(docs)
    changes = {
        "added": sorted(set(new_fp) - set(old_fp)),
        "removed": sorted(set(old_fp) - set(new_fp)),
        "modified": sorted(k for k in set(old_fp) & set(new_fp) if old_fp[k] != new_fp[k]),
        "unchanged": sorted(k for k in set(old_fp) & set(new_fp) if old_fp[k] == new_fp[k]),
    }
    log(f"document changes: {json.dumps({k: v for k, v in changes.items() if k != 'unchanged'})}")
    diffs = diff_documents(old_docs, docs)
    # which changed sentences does the OLD eval set actually exercise?
    for ch in diffs:
        ch["covered_by"] = [c["id"] for c in old_eval["cases"] if ch["old"] and any(fact_support(f, ch["old"]) >= 0.75 for f in c["key_facts"])]
    log(f"{len(diffs)} changed sentence(s); covered by old tests: {sum(1 for c in diffs if c['covered_by'])}")

    if reoptimize:
        log("regenerating the agent with the full factory (requirements reused, intake skipped)")
        req = {k: v for k, v in old_req.items() if k not in ("groundability", "approval")}
        run_factory(old_req.get("brief", ""), new_docs, out / "rebuilt", requirements_override=req, model=model)
        new_agent = json.loads((out / "rebuilt" / "agent.json").read_text())
        agent_base = out / "rebuilt"
    else:
        log("regenerating the agent definition in place (prompt + retrieval kept, gaps re-checked)")
        llm0 = LLM(model=model or old_agent["model"]["name"])
        idx = Index(docs, ChunkingConfig(), llm0)
        req = copy.deepcopy(old_req)
        req["groundability"] = groundability(req, docs, idx, llm0, log)
        new_agent = copy.deepcopy(old_agent)
        new_agent["knowledge"]["docs_path"] = os.path.relpath(new_docs, out.resolve())
        new_agent["knowledge"]["documents"] = new_fp
        new_agent["system_prompt"]["sections"]["known_gaps"] = known_gaps_section(req)
        new_agent["scope"]["gaps"] = req["groundability"]["gaps"]
        new_agent["created_at"] = dt.datetime.now().isoformat(timespec="seconds")
        (out / "agent.json").write_text(json.dumps(new_agent, indent=2))
        agent_base = out

    llm = LLM(model=model or new_agent["model"]["name"])
    judge = Judge(llm)
    judge.calibrate(old_eval["cases"])
    today = dt.date.fromisoformat(old_eval.get("eval_today", dt.date.today().isoformat()))
    wb = Workbench(docs, llm, judge, agent_base, today, log)
    log(f"re-running the OLD eval set ({len(old_eval['cases'])} cases) against the regenerated agent")
    ev = wb.evaluate(new_agent, old_eval["cases"], label="regression")
    new_index = wb.index_for(new_agent["knowledge"]["chunking"])
    chunks = new_index.chunks
    # fresh "delta" tests for the changed sentences (the old eval set may not exercise them at all)
    for ch in diffs:
        ctx = next((c.text.split("\n", 1)[-1] for c in chunks if ch["new"] and ch["new"][:60] in c.text), ch["new"])
        ch["context"] = ctx
    gen = EvalGenerator(old_req, docs, new_index, llm, [], today=today, log=log)
    gen.gen_delta([ch for ch in diffs if ch["new"]])
    delta_cases = gen.cases
    for c in delta_cases:
        c["id"] = c["id"].replace("DEL-", "DELTA-")
    delta_ev = wb.evaluate(new_agent, delta_cases, label="delta") if delta_cases else {"results": []}
    delta_rows = [{"case_id": c["id"], "question": c["question"], "old": c["change"]["old"], "new": c["change"]["new"],
                   "key_facts": c["key_facts"], "passed": r["passed"], "reasons": r["reasons"], "answer": r["answer"]}
                  for c, r in zip(delta_cases, delta_ev["results"])]
    (out / "delta_evalset.json").write_text(json.dumps({"cases": delta_cases, "rejected_candidates": gen.rejected}, indent=2))
    rows = []
    for case, r in zip(old_eval["cases"], ev["results"]):
        before, after = old_pass.get(case["id"]), r["passed"]
        status = "still-pass" if before and after else "fixed" if after else "still-fail" if before is False else "BROKE"
        stale = []
        removed_src = [s for s in case.get("expected_sources", []) if s in changes["removed"]]
        if case["expected_behavior"] == "answer" and not case.get("expected_tools"):
            stale = [f for f in case["key_facts"] if _fact_present(f, chunks) < 0.75]
        touched = sorted(set(case.get("expected_sources", [])) & set(changes["modified"] + changes["removed"]))
        verdict = classify_change(status, removed_src, stale)
        rows.append({"case_id": case["id"], "type": case["type"], "question": case["question"], "before": before, "after": after,
                     "status": status, "verdict": verdict, "touched_docs": touched, "stale_facts": stale,
                     "reasons": r["reasons"], "category": classify(case, r)[0] if not after else "", "answer": r["answer"]})
    summ = summarize(ev["results"])
    old_s = sum(1 for v in old_pass.values() if v)
    report = {"changes": changes, "old_score": f"{old_s}/{len(old_pass)}", "new_score": f"{summ['passed']}/{summ['n']}",
              "summary": summ, "rows": rows,
              "counts": {s: sum(1 for x in rows if x["status"] == s) for s in ("still-pass", "fixed", "still-fail", "BROKE")},
              "real_regressions": [x["case_id"] for x in rows if x["verdict"].startswith("REGRESSION")],
              "stale_tests": [x["case_id"] for x in rows if x["verdict"].startswith("stale")],
              "changed_sentences": [{k: v for k, v in ch.items() if k != "context"} for ch in diffs],
              "delta_tests": delta_rows,
              "delta_score": f"{sum(1 for x in delta_rows if x['passed'])}/{len(delta_rows)}"}
    (out / "regression.json").write_text(json.dumps(report, indent=2))
    L = ["# Regression report", "", f"Old run: `{old_run}` | new documents: `{os.path.relpath(new_docs)}` | agent rebuilt with {'full factory' if reoptimize else 'in-place regeneration'}", "",
         "## Document changes", ""] + [f"- {k}: {', '.join(v) or '-'}" for k, v in changes.items()] + [
         "", "## Result", "", f"- Old eval set on old agent: **{report['old_score']}**; same eval set on regenerated agent: **{report['new_score']}**",
         f"- Status counts: {report['counts']}",
         f"- **Real regressions:** {', '.join(report['real_regressions']) or 'none'}",
         f"- **Stale tests (document changed, expectation obsolete):** {', '.join(report['stale_tests']) or 'none'}",
         f"- **Changed sentences:** {len(diffs)}, of which exercised by the old eval set: {sum(1 for c in diffs if c['covered_by'])}"
         + (" -> the old eval set is blind to these changes, so fresh delta tests were generated below" if diffs and not all(c['covered_by'] for c in diffs) else ""),
         f"- **Delta tests on the changed passages (regenerated agent):** {report['delta_score']}", "",
         "## What changed in the documents", "", "| Document | Before | After | Old tests covering it |", "|---|---|---|---|"] + [
         f"| {c['doc_id']} | {c['old'][:110].replace('|', '/') or '(added)'} | {c['new'][:110].replace('|', '/') or '(removed)'} | {', '.join(c['covered_by']) or 'none'} |"
         for c in diffs] + ["", "## Delta tests (generated from the changed passages)", "", "| Case | Question | Expected (new value) | Result | Agent said |", "|---|---|---|---|---|"] + [
         f"| {x['case_id']} | {x['question'][:100].replace('|', '/')} | {'; '.join(x['key_facts'])[:100].replace('|', '/')} | {'PASS' if x['passed'] else 'FAIL: ' + '; '.join(x['reasons'])[:120].replace('|', '/')} | {x['answer'][:140].replace('|', '/').replace(chr(10), ' ')} |"
         for x in delta_rows] + ["", "## Old eval set re-run", "",
         "| Case | Type | Before | After | Status | Verdict | Touched docs | Question |", "|---|---|---|---|---|---|---|---|"]
    for x in rows:
        L.append(f"| {x['case_id']} | {x['type']} | {'pass' if x['before'] else 'fail'} | {'pass' if x['after'] else 'fail'} | {x['status']} | "
                 f"{x['verdict'].replace('|', '/')} | {', '.join(x['touched_docs']) or '-'} | {x['question'][:90].replace('|', '/')} |")
    L += ["", "Broken-case details:", ""]
    for x in rows:
        if x["status"] == "BROKE":
            L += [f"- **{x['case_id']}** ({x['verdict']}): {'; '.join(x['reasons'])[:300]}  \n  agent said: {x['answer'][:250]!r}"]
    (out / "REGRESSION.md").write_text("\n".join(L) + "\n")
    log(f"regression done: {report['counts']} | real regressions {report['real_regressions']} | stale {report['stale_tests']} | delta tests {report['delta_score']}")
    return report

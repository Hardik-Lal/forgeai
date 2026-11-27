"""End-to-end factory run. Writes every intermediate artifact to the run folder."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from . import prompts
from .documents import load_folder
from .evalgen import EvalGenerator
from .generator import STYLES, build_definition, choose_retrieval, generate_sections, save_definition, select_tools
from .intake import Asker, groundability, interview, notes_asker, requirements_markdown
from .judge import Judge, summarize
from .llm import LLM, Budget, BudgetExceeded
from .optimizer import Workbench, diagnose, optimize, tournament
from .report import write_report
from .retrieval import ChunkingConfig, Index

# Approver: receives requirements dict, returns (possibly edited) dict or None to abort.
Approver = Callable[[dict, str], "dict | None"]


class RunLogger:
    def __init__(self, path: Path, echo: bool = True):
        self.path, self.echo = path, echo
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", buffering=1)

    def __call__(self, msg: str) -> None:
        line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.f.write(line + "\n")
        if self.echo:
            print(line, flush=True)


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


def _display(p: Path) -> str:
    """Path relative to the working directory when possible, so committed logs don't leak the user's home path."""
    try:
        return os.path.relpath(p)
    except ValueError:  # pragma: no cover - different drive on Windows
        return str(p)


def run_factory(
    brief: str,
    docs_dir: str | Path,
    out_dir: str | Path,
    *,
    ask: Asker | None = None,
    approve: Approver | None = None,
    model: str | None = None,
    judge_model: str | None = None,
    max_tokens: int = 3_000_000,
    max_minutes: float = 180,
    n_cases: int = 26,
    tournament_size: int = 3,
    max_iters: int = 3,
    requirements_override: dict | None = None,
    requester_notes: str | None = None,
    use_cache: bool = True,
    echo: bool = True,
) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log = RunLogger(out / "factory.log", echo=echo)
    budget = Budget(max_tokens=max_tokens, max_seconds=max_minutes * 60)
    llm = LLM(model=model or os.environ.get("FORGE_MODEL", "qwen2.5:7b-instruct"), budget=budget, use_cache=use_cache)
    t_start = time.time()
    status = llm.ping()
    if not status["chat_model_ok"] or not status["embed_model_ok"]:
        raise RuntimeError(f"Ollama models missing: have {status['models']}; need {llm.model} and {llm.embed_model}")
    docs_dir = Path(docs_dir).resolve()
    (out / "brief.txt").write_text(brief.strip() + "\n")
    snap = out / "prompts_snapshot"
    snap.mkdir(exist_ok=True)
    for name, text in prompts.all_prompts().items():
        (snap / f"{name}.md").write_text(text)
    today = dt.date.today()
    log(f"ForgeAI run -> {out}  model={llm.model}  docs={_display(docs_dir)}")

    # 1. documents --------------------------------------------------------
    docs = load_folder(docs_dir)
    log(f"loaded {len(docs)} documents: " + ", ".join(f"{d.doc_id} ({d.kind}, {d.n_words}w)" for d in docs))
    base_index = Index(docs, ChunkingConfig(), llm)
    log(f"indexed {len(base_index.chunks)} chunks (default chunking for intake/eval generation)")

    # 2. intake + groundability -------------------------------------------
    if requirements_override:
        req = dict(requirements_override)
        req["brief"] = brief
        log("intake: using supplied requirements (regression / re-run mode)")
    else:
        log("intake: drafting requirements and clarifying questions")
        if ask is None and requester_notes:
            ask = notes_asker(requester_notes, llm)
            (out / "requester_notes.txt").write_text(requester_notes)
        req = interview(brief, docs, llm, ask, log)
    log("groundability guard: checking every required topic against the documents")
    req["groundability"] = groundability(req, docs, base_index, llm, log)
    gaps = req["groundability"]["gaps"]
    if gaps:
        log(f"groundability: {len(gaps)} GAP(S) flagged -> agent will decline: {[g['topic'] for g in gaps]}")

    # 3. human checkpoint --------------------------------------------------
    md = requirements_markdown(req)
    if approve is not None:
        edited = approve(req, md)
        if edited is None:
            _dump(out / "01_requirements.json", req)
            (out / "01_requirements.md").write_text(md)
            log("requester rejected the requirements; stopping before generation")
            return {"status": "aborted", "out_dir": str(out)}
        if edited is not req and edited != req:
            req = edited
            log("requester edited the requirements; re-running groundability on the edited version")
            req["groundability"] = groundability(req, docs, base_index, llm, log)
        req["approval"] = {"status": "approved", "by": "requester", "at": dt.datetime.now().isoformat(timespec="seconds")}
    elif not (requirements_override and requirements_override.get("approval")):
        req["approval"] = {"status": "auto-approved (unattended run)", "by": "factory", "at": dt.datetime.now().isoformat(timespec="seconds")}
    _dump(out / "01_requirements.json", req)
    (out / "01_requirements.md").write_text(requirements_markdown(req))

    # 4. tools + retrieval -------------------------------------------------
    log("generator: selecting tools")
    tools, rejected = select_tools(req, docs, llm)
    log(f"generator: attached {[t['name'] for t in tools] or 'no tools'}; rejected {[t['name'] for t in rejected]}")
    chunking, rcfg, rationale = choose_retrieval(docs)
    for r in rationale:
        log(f"  retrieval: {r}")

    # 5. eval set ----------------------------------------------------------
    log("eval generator: building the test set from the documents")
    eg = EvalGenerator(req, docs, base_index, llm, [t["name"] for t in tools], today=today, log=log)
    evalset = eg.generate(target=n_cases)
    _dump(out / "03_evalset.json", evalset)
    cases = evalset["cases"]
    log(f"eval set: {evalset['n_cases']} cases {evalset['counts']} | dev={evalset['splits']['dev']} heldout={evalset['splits']['heldout']} "
        f"| {len(evalset['rejected_candidates'])} candidates rejected by validation")
    dev = [c for c in cases if c["split"] == "dev"]
    held = [c for c in cases if c["split"] == "heldout"]

    # 6. judge calibration -------------------------------------------------
    judge = Judge(llm, judge_model=judge_model)
    cal = judge.calibrate(cases)
    _dump(out / "04_judge_calibration.json", cal)
    log(f"judge calibration: {cal['accuracy']:.0%} on {cal['n_probes']} probes -> {cal['note']}")

    # 7. candidates + tournament --------------------------------------------
    rel_docs = os.path.relpath(docs_dir, out.resolve())
    cand_dir = out / "02_agent_candidates"
    cand_dir.mkdir(exist_ok=True)
    candidates = {}
    for style in list(STYLES)[: max(1, tournament_size)]:
        log(f"generator: writing system prompt candidate {style}")
        sections = generate_sections(req, docs, tools, llm, style)
        d = build_definition(req, docs, rel_docs, sections, tools, rejected, chunking, rcfg, rationale, llm.model, style)
        candidates[style] = d
        save_definition(d, cand_dir / f"{style}.json")
    wb = Workbench(docs, llm, judge, out, today, log)
    wb._indexes[json.dumps(vars(ChunkingConfig()), sort_keys=True)] = base_index
    if len(candidates) > 1:
        tour = tournament(candidates, wb, dev, log)
        log(f"tournament winner: {tour['winner']} {tour['note']}")
    else:
        only = next(iter(candidates))
        ev = wb.evaluate(candidates[only], dev, label="baseline")
        tour = {"rows": [], "winner": only, "winner_eval": ev, "margin_cases": None, "note": "tournament disabled"}
    _dump(out / "05_tournament.json", {k: v for k, v in tour.items() if k != "winner_eval"})
    baseline = candidates[tour["winner"]]
    save_definition(baseline, out / "agent_baseline.json")
    baseline_dev = tour["winner_eval"]

    log("held-out: scoring the baseline (never shown to the optimizer)")
    try:
        baseline_held = wb.evaluate(baseline, held, label="heldout base") if held else None
    except BudgetExceeded as e:
        baseline_held = None
        log(f"budget: {e}")

    # 8. critic-optimizer loop ----------------------------------------------
    log(f"optimizer: starting (max {max_iters} iterations)")
    opt = optimize(baseline, wb, cases, baseline_dev, max_iters=max_iters, log=log)
    final = opt["definition"]
    final["optimization"] = {"iterations": len(opt["iterations"]) - 1, "stop_reason": opt["stop_reason"]}
    save_definition(final, out / "agent.json")
    _dump(out / "06_iterations.json", {"stop_reason": opt["stop_reason"], "iterations": opt["iterations"]})
    log(f"optimizer stopped: {opt['stop_reason']}")

    if held and final is not baseline:
        try:
            final_held = wb.evaluate(final, held, label="heldout final")
        except BudgetExceeded as e:
            final_held = baseline_held
            log(f"budget: {e}; reusing baseline held-out score")
    else:
        final_held = baseline_held
    final_dev = opt["final_dev_eval"]
    cases_by_id = {c["id"]: c for c in cases}
    all_results = final_dev["results"] + (final_held["results"] if final_held else [])
    for r in all_results:
        if not r["passed"]:
            from .optimizer import classify
            r["failure_category"], r["failure_subcategory"] = classify(cases_by_id[r["case_id"]], r)
    scorecard = {
        "agent": final["name"],
        "model": llm.model,
        "baseline": {"dev": baseline_dev["summary"], "heldout": baseline_held["summary"] if baseline_held else None},
        "final": {"dev": final_dev["summary"], "heldout": final_held["summary"] if final_held else None,
                  "overall": summarize(all_results)},
        "iterations": [
            {"iteration": it["iteration"], "action": it.get("action") or f"{it['change']['kind']}: {it['change'].get('knob') or it['change'].get('section')}",
             "category": it.get("category"), "decision": it.get("decision", "baseline"),
             "dev_passed": it["score"]["passed"], "dev_n": it["score"]["n"], "dev_pass_rate": it["score"]["pass_rate"],
             "fixed": it.get("fixed_cases", []), "broken": it.get("broken_cases", [])}
            for it in opt["iterations"]
        ],
        "stop_reason": opt["stop_reason"],
        "judge_calibration": {k: v for k, v in cal.items() if k != "probes"},
        "judge_overrules": judge.overrules,
        "budget": budget.snapshot(),
        "wall_minutes": round((time.time() - t_start) / 60, 1),
    }
    _dump(out / "07_scorecard.json", scorecard)
    _dump(out / "08_final_results.json", {"dev": final_dev["results"], "heldout": final_held["results"] if final_held else []})
    write_report(out, req, final, baseline, evalset, cal, tour, opt, scorecard, all_results, cases_by_id)
    log(f"DONE in {scorecard['wall_minutes']} min. dev {final_dev['summary']['passed']}/{final_dev['summary']['n']}"
        + (f", held-out {final_held['summary']['passed']}/{final_held['summary']['n']}" if final_held else "")
        + f". Report: {out / 'REPORT.md'}")
    return {"status": "ok", "out_dir": str(out), "scorecard": scorecard}

"""Stage 7: honest reporting. Writes REPORT.md + human-readable companions."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .runtime import compose_system_prompt


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _score(s: dict | None) -> str:
    if not s:
        return "-"
    return f"{s['passed']}/{s['n']} ({_pct(s['pass_rate'])}, 95% CI {_pct(s['ci95'][0])}-{_pct(s['ci95'][1])})"


def _esc(s: str, n: int = 160) -> str:
    s = str(s).replace("|", "/").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def evalset_markdown(evalset: dict) -> str:
    L = [f"# Generated evaluation set", "", f"{evalset['n_cases']} cases, generated {evalset['generated_at']} "
         f"(dates evaluated relative to {evalset['eval_today']}).", "",
         "Counts: " + ", ".join(f"{k}={v}" for k, v in evalset["counts"].items()) + f" | splits: {evalset['splits']}", "",
         "Generation prompts are in `prompts_snapshot/` (fingerprints: " + ", ".join(f"{k}={v}" for k, v in evalset["prompts_used"].items()) + ").", "",
         "| ID | Split | Type | Question | Expected | Key facts | Sources / tools |", "|---|---|---|---|---|---|---|"]
    for c in evalset["cases"]:
        L.append(f"| {c['id']} | {c['split']} | {c['type']} | {_esc(c['question'], 140)} | {c['expected_behavior']} | "
                 f"{_esc('; '.join(c['key_facts']), 120)} | {_esc(', '.join(c.get('expected_sources', []) + c.get('expected_tools', [])), 80)} |")
    rej = Counter(r["reason"].split(" (")[0].split(":")[0] for r in evalset["rejected_candidates"])
    L += ["", f"## Candidates rejected by validation ({len(evalset['rejected_candidates'])})", ""]
    L += [f"- {k}: {v}" for k, v in rej.most_common()] or ["- none"]
    return "\n".join(L) + "\n"


def iterations_markdown(opt: dict) -> str:
    L = ["# Critic-optimizer iteration log", "", f"Stop reason: **{opt['stop_reason']}**", ""]
    for it in opt["iterations"]:
        if it["iteration"] == 0:
            s = it["score"]
            L += [f"## Iteration 0 - baseline", "", f"Dev score: {_score(s)}", ""]
        else:
            ch = it["change"]
            L += [f"## Iteration {it['iteration']} - {it['decision'].upper()}", "",
                  f"- **Failure category targeted:** {it['category']} ({len(it['targeted_cases'])} case(s): {', '.join(it['targeted_cases'])}); "
                  f"severity-weighted costs: {it.get('priority_costs', {})}"]
            if ch["kind"] in ("retrieval", "runtime"):
                L += [f"- **What changed:** {ch['kind']} `{ch['knob']}`: `{ch['before']}` -> `{ch['after']}`", f"- **Why:** {ch['why']}"]
                if ch.get("diagnostics"):
                    L += ["- **Diagnostics (rank of expected source; None = not in top 40):**"]
                    L += [f"  - {d['case']} `{d['doc']}`: hybrid={d['rank_hybrid']} bm25={d['rank_bm25']} dense={d['rank_dense']}" for d in ch["diagnostics"][:8]]
            else:
                L += [f"- **What changed:** system-prompt section `{ch['section']}` rewritten" + (f"; {ch['extra']}" if ch.get("extra") else ""),
                      f"- **Why:** {ch['rationale']}", "", "```diff", ch["diff"] or "(no textual diff)", "```"]
            L += [f"- **Score before -> after (dev):** {it['score_before']['passed']}/{it['score_before']['n']} -> {it['score_after']['passed']}/{it['score_after']['n']}",
                  f"- **Cases fixed:** {', '.join(it['fixed_cases']) or 'none'} (of the targeted: {', '.join(it['targeted_fixed']) or 'none'})",
                  f"- **Cases broken:** {', '.join(it['broken_cases']) or 'none'}",
                  f"- **Decision:** {it['decision']} - {it['decision_reason']}",
                  f"- **Cost:** {it['cost']['tokens']} tokens, {it['cost']['seconds']}s", ""]
    return "\n".join(L) + "\n"


def write_report(out: Path, req, final, baseline, evalset, cal, tour, opt, sc, all_results, cases_by_id) -> None:
    (out / "03_evalset.md").write_text(evalset_markdown(evalset))
    (out / "06_iteration_log.md").write_text(iterations_markdown(opt))
    (out / "agent_system_prompt.txt").write_text(compose_system_prompt(final) + "\n")

    fd, fh, fo = sc["final"]["dev"], sc["final"]["heldout"], sc["final"]["overall"]
    bd, bh = sc["baseline"]["dev"], sc["baseline"]["heldout"]
    failing = [r for r in all_results if not r["passed"]]
    cats = Counter(r.get("failure_category", "?") for r in failing)
    L = [f"# ForgeAI report - {final['name']}", "",
         f"Model: `{sc['model']}` (local, via Ollama) | wall time {sc['wall_minutes']} min | "
         f"{sc['budget']['tokens_total']:,} tokens over {sc['budget']['calls'] - sc['budget']['cache_hits']} uncached LLM calls"
         + (f" ({sc['budget']['cache_hits']} further calls were identical to earlier calls (in this run or a previous one) and were replayed from the on-disk cache"
            + (f", which originally cost {sc['budget'].get('cached_tokens', 0):,} tokens / {sc['budget'].get('cached_llm_seconds', 0) / 60:.0f} LLM-minutes"
               if sc['budget'].get('cached_tokens') else "")
            + "; wall time therefore understates a cold run, see RESULTS.md for measured cold timings)" if sc['budget']['cache_hits'] else ""), "",
         "## Headline", "",
         f"- **Overall (all {fo['n']} generated cases): {fo['passed']}/{fo['n']} pass = {_pct(fo['pass_rate'])}** (95% CI {_pct(fo['ci95'][0])}-{_pct(fo['ci95'][1])})",
         f"- Dev split (used by the optimizer): baseline {_score(bd)} -> final {_score(fd)}",
         f"- Held-out split (never shown to the optimizer): baseline {_score(bh)} -> final {_score(fh)}"]
    if bh and fh:
        dd, dh = fd["pass_rate"] - bd["pass_rate"], fh["pass_rate"] - bh["pass_rate"]
        if dd > 0 and dh < 0:
            L.append(f"- **Overfitting warning:** dev improved by {dd:+.0%} but held-out moved {dh:+.0%}; treat the dev gain with suspicion.")
        else:
            L.append(f"- Dev change {dd:+.0%}, held-out change {dh:+.0%}.")
    L += [f"- Judge self-test: {_pct(cal['accuracy'])} on {cal['n_probes']} probes ({cal['note']}); "
          f"judge was overruled by deterministic cross-checks {sc['judge_overrules']} time(s)",
          f"- Optimizer stop reason: {sc['stop_reason']}", "",
          "> How to read this: the eval set is generated by the same small model family the agent runs on, then filtered by "
          "deterministic validation. With ~25 cases one case is ~4 points, so differences under ~2 cases are noise. "
          "The 95% interval is the honest error bar.", ""]

    # what still fails
    L += ["## What still fails, and why", ""]
    if not failing:
        L += ["No generated case fails. Be sceptical: this most likely means the generated set is too easy, not that the agent is perfect. "
              "See *Threats to validity*.", ""]
    else:
        L += ["Failure categories (final agent, dev + held-out): " + ", ".join(f"**{k}** x{v}" for k, v in cats.most_common()), ""]
        L += ["| Case | Split | Type | Category | Question | What went wrong | Agent said |", "|---|---|---|---|---|---|---|"]
        for r in failing:
            L.append(f"| {r['case_id']} | {r['split']} | {r['type']} | {r.get('failure_category')}/{r.get('failure_subcategory')} | "
                     f"{_esc(r['question'], 110)} | {_esc('; '.join(r['reasons']), 170)} | {_esc(r['answer'], 120)} |")
        L += ["", "### Interpretation", ""]
        expl = {
            "retrieval_miss": "the hybrid retriever did not surface the document that holds the answer; the agent then (correctly) declined or answered partially. Fixable with better chunking/query rewriting, not with prompting.",
            "wrong_refusal": "the evidence was retrieved but the 7B model judged it insufficient. This is the conservative failure mode the grounding contract deliberately prefers over guessing.",
            "hallucination": "the answer contained a figure or claim not found in the retrieved sources (detected by deterministic number checks and the verified LLM audit).",
            "prompt_defect": "sources were available but the answer was incomplete, cited the wrong document, skipped a required tool, or leaked into a must-refuse topic.",
        }
        L += [f"- **{k}** ({v}): {expl.get(k, '')}" for k, v in cats.most_common()]
        guard_fp = [r["case_id"] for r in failing if r.get("failure_subcategory") == "guard"]
        if guard_fp:
            L.append(f"- **Scope-guard false positives** ({', '.join(guard_fp)}): the runtime scope guard accepted by the optimizer blocked "
                     "an answerable question. This is the known cost of that fix; it was accepted because it removed more harmful "
                     "out-of-scope answers than the answerable ones it blocked (see the iteration log).")
        L.append("")
        if any(cases_by_id[r["case_id"]]["type"] == "multi_doc" for r in failing):
            L += ["- Multi-document questions are the hardest category: both sources must be retrieved and combined by a small model.", ""]

    # requirements + gaps
    g = req.get("groundability", {})
    L += ["## Requirements and groundability", "",
          f"Full document: `01_requirements.md`. Scope: {req['scope']}", "",
          f"- Must answer: {', '.join(req['must_answer_topics'])}",
          f"- Must refuse: {', '.join(req['must_refuse_topics'])}"]
    if g.get("gaps"):
        L += [f"- **Flagged gaps (brief asks for it, documents cannot support it -> agent declines and escalates to {req['escalation_contact']}):**"]
        L += [f"  - {x['topic']}: {x['reason']}" for x in g["gaps"]]
    else:
        L += ["- No groundability gaps: every requested topic was supported by a verified quote."]
    L.append("")

    # tools
    L += ["## Tool selection", "", "| Tool | Attached | Model vote | Requirement evidence | Reason |", "|---|---|---|---|---|"]
    att = {t["name"] for t in final["tools"]}
    for t in final.get("tool_selection_audit", []):
        L.append(f"| {t['name']} | {'**yes**' if t['name'] in att else 'no'} | {'yes' if t['llm_vote'] else 'no'} | "
                 f"{_esc(', '.join(t['evidence_terms']) or ('quote: ' + t['requirement_quote'] if t['quote_verified'] else '-'), 70)} | {_esc(t['reason'], 140)} |")
    L += ["", "A tool is attached only if the model votes for it AND the brief/requirements contain evidence for it AND its precondition holds.", ""]

    # retrieval
    kb, kf = baseline["knowledge"], final["knowledge"]
    L += ["## Agent definition", "", f"Final definition: `agent.json` (system prompt rendered in `agent_system_prompt.txt`). Prompt variant: `{final['system_prompt']['variant']}`.", "",
          "| Parameter | Initial | Final |", "|---|---|---|"]
    for sect in ("chunking", "retrieval"):
        for k, v in kb[sect].items():
            L.append(f"| {sect}.{k} | {v} | {kf[sect][k]}{' **(changed)**' if kf[sect][k] != v else ''} |")
    for k, v in baseline.get("guards", {}).items():
        fv = final.get("guards", {}).get(k)
        L.append(f"| guards.{k} | {v} | {fv}{' **(changed)**' if fv != v else ''} |")
    L += ["", "Initial choices: " + "; ".join(kb["rationale"]), "",
          f"Output contract: JSON `{json.dumps(final['output_contract']['schema'])}` with rules `{json.dumps(final['output_contract']['rules'])}`.", ""]

    # tournament
    if tour.get("rows"):
        L += ["## Prompt tournament (dev split)", "", "| Candidate | Passed | Pass rate (95% CI) | Fact recall | Wrong refusals | Missed refusals | By type |", "|---|---|---|---|---|---|---|"]
        for r in tour["rows"]:
            L.append(f"| {r['candidate']}{' (winner)' if r['candidate'] == tour['winner'] else ''} | {r['passed']}/{r['n']} | {_pct(r['pass_rate'])} ({_pct(r['ci95'][0])}-{_pct(r['ci95'][1])}) | "
                     f"{_pct(r['mean_fact_recall'])} | {r['wrong_refusals']} | {r['missed_refusals']} | {_esc(json.dumps(r['by_type']), 120)} |")
        L += ["", f"Tie-break order: {tour['tie_break']}. {tour.get('note', '')}", ""]

    # scorecard
    L += ["## Per-iteration scorecard (dev split)", "", "| Iter | Change | Category | Decision | Dev passed | Fixed | Broke |", "|---|---|---|---|---|---|---|"]
    for it in sc["iterations"]:
        L.append(f"| {it['iteration']} | {_esc(it['action'], 60)} | {it['category'] or '-'} | {it['decision']} | {it['dev_passed']}/{it['dev_n']} ({_pct(it['dev_pass_rate'])}) | "
                 f"{', '.join(it['fixed']) or '-'} | {', '.join(it['broken']) or '-'} |")
    L += ["", "Full change log with prompt diffs: `06_iteration_log.md`.", ""]

    # breakdowns
    L += ["## Final scores by case type and check", "", "| Type | Dev | Held-out |", "|---|---|---|"]
    for t in sorted(set(fd["by_type"]) | set((fh or {}).get("by_type", {}))):
        L.append(f"| {t} | {fd['by_type'].get(t, '-')} | {(fh or {}).get('by_type', {}).get(t, '-')} |")
    L += ["", "| Check | Overall pass rate |", "|---|---|"] + [f"| {k} | {_pct(v)} |" for k, v in fo["check_pass_rates"].items()]
    L += ["", f"Refusals: {fo['refusal_precision']}. Mean key-fact recall on answered cases: {_pct(fo['mean_fact_recall'])}. Mean latency {fo['mean_latency_s']}s/question.", ""]

    # eval set
    L += ["## The generated evaluation set", "",
          f"{evalset['n_cases']} cases ({', '.join(f'{k}: {v}' for k, v in evalset['counts'].items())}); "
          f"{len(evalset['rejected_candidates'])} generated candidates were rejected by validation (unverifiable facts, answer leakage, duplicates). "
          "Every answerable case's key facts were verified against its seed passage; tool cases have values computed deterministically. "
          "See `03_evalset.md` / `03_evalset.json`.", ""]

    # judge
    L += ["## Judge reliability", "",
          f"- Calibration: {cal['by_probe_type']} (positive = should say yes; others should say no). Overall {_pct(cal['accuracy'])}.",
          "- Position bias: no pairwise judging anywhere; tournament ranks by absolute pass counts.",
          "- Verbosity bias: one atomic fact at a time, rubric says length is irrelevant, and the `verbose-wrong` probe checks it.",
          f"- Self-preference: the judge is the same model as the agent (`{cal['judge_model']}`), so it never sees the model-written reference answer, every 'yes' needs a verbatim quote that is verified, and "
          "numbers are checked deterministically. Groundedness flags are overruled when the claim is in fact present in the sources.", ""]

    # threats
    L += ["## Threats to validity (read before trusting the numbers)", "",
          "1. The eval set is generated by the same model family the agent uses, so the questions are biased towards what a 7B model finds askable; real users will be messier.",
          "2. Deterministic fact matching under-counts correct paraphrases when the LLM judge rejects them, so the score is more likely to be pessimistic than optimistic.",
          "3. ~25 cases give wide confidence intervals; a 1-2 case change is within noise.",
          "4. Refusal detection relies on the contract's `refused` flag plus phrase patterns; a refusal phrased unusually could be misread.",
          "5. The optimizer only sees the dev split; the held-out split is the better estimate of real performance.", ""]

    b = sc["budget"]
    L += ["## Budget", "", f"{b['tokens_total']:,} / {b['max_tokens']:,} tokens, {b['wall_seconds']:.0f}s wall clock, {b['cache_hits']} cache hits.", "",
          "| Stage | Calls | Tokens | Seconds |", "|---|---|---|---|"]
    L += [f"| {k} | {v['calls']} | {v['tokens']:,} | {v['seconds']} |" for k, v in sorted(b["by_stage"].items())]
    L += ["", "## Artifacts in this folder", "",
          "`brief.txt`, `01_requirements.{md,json}`, `02_agent_candidates/`, `agent_baseline.json`, `agent.json`, `agent_system_prompt.txt`, "
          "`03_evalset.{md,json}`, `04_judge_calibration.json`, `05_tournament.json`, `06_iterations.json`, `06_iteration_log.md`, "
          "`07_scorecard.json`, `08_final_results.json` (every answer with its judge verdict), `prompts_snapshot/`, `factory.log`.", ""]
    (out / "REPORT.md").write_text("\n".join(L))

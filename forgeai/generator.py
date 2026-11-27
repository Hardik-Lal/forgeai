"""Stage 3: agent generator.

Produces a complete *declarative* agent definition (agent.json):
  system prompt (sectioned, so later fixes can be surgical), chunking and
  retrieval parameters (derived from corpus statistics, with rationale),
  tool selection (LLM vote AND deterministic evidence AND precondition),
  output contract, and the groundability gaps the agent must decline.
Also generates 2-3 candidate prompts for the prompt tournament.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from pathlib import Path

from . import prompts
from .documents import Document, corpus_fingerprint, profile_text
from .llm import LLM
from .retrieval import ChunkingConfig, RetrievalConfig, _segment, config_dict
from .runtime import GROUNDING_CONTRACT, SECTION_ORDER
from .textmatch import fact_support, quote_in
from .tools import REGISTRY, evidence_for

STYLES = {
    "A_strict": "Strict and minimal: short imperative rules, answers of 1-4 sentences, refuse whenever the sources are not explicit.",
    "B_structured": "Structured and thorough: answers lead with the direct answer, then list conditions, limits and exceptions from the sources as short bullets; partial answers are preferred over refusals when the sources cover part of the question.",
    "C_stepwise": "Stepwise reasoning style: the rules tell the assistant to (1) find the relevant source lines, (2) check they really answer the question, (3) answer with exact figures and citations, (4) otherwise refuse with the escalation contact. Friendly but precise.",
}

SECTIONS_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in
                   ["role", "scope", "audience_and_tone", "answering_rules", "refusal_policy", "tool_rules", "citation_rules"]},
    "required": ["role", "scope", "audience_and_tone", "answering_rules", "refusal_policy", "tool_rules", "citation_rules"],
}


def _req_text(req: dict) -> str:
    keep = {k: req[k] for k in ["agent_name", "purpose", "scope", "audience", "tone", "must_answer_topics", "must_refuse_topics",
                               "capabilities_requested", "escalation_contact", "language"] if k in req}
    return json.dumps(keep, indent=1)


# ------------------------------------------------------------------ tools
def select_tools(req: dict, docs: list[Document], llm: LLM) -> tuple[list[dict], list[dict]]:
    names = list(REGISTRY)
    schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": names},
                        "needed": {"type": "boolean"},
                        "justification": {"type": "string"},
                        "requirement_quote": {"type": "string"},
                    },
                    "required": ["tool", "needed", "justification", "requirement_quote"],
                },
            }
        },
        "required": ["decisions"],
    }
    kinds = ", ".join(f"{d.doc_id} ({d.kind})" for d in docs)
    catalog = "\n".join(f"- {t.name}: {t.description} Needed only for: {t.needs}." for t in REGISTRY.values())
    out = llm.json(
        [{"role": "user", "content": prompts.render("tool_select", brief=req.get("brief", ""), requirements=_req_text(req), doc_kinds=kinds, tools=catalog)}],
        schema, stage="generator.tools", max_tokens=900,
    )
    votes = {d["tool"]: d for d in out["decisions"]}
    req_text = " ".join([req.get("brief", ""), req.get("scope", ""), req.get("purpose", "")] + req.get("must_answer_topics", []) + req.get("capabilities_requested", []))
    tool_caps = {c["tool"] for c in req.get("groundability", {}).get("tool_capabilities", [])}
    attached, rejected = [], []
    for name, tool in REGISTRY.items():
        v = votes.get(name, {"needed": False, "justification": "model gave no decision", "requirement_quote": ""})
        pre_ok, pre_note = tool.precondition(docs)
        ev = evidence_for(tool, req_text)
        quote_ok = bool(v["requirement_quote"].strip()) and quote_in(v["requirement_quote"], req_text, 0.8)[0]
        llm_yes = bool(v["needed"]) or name in tool_caps
        # a need that only comes from a flagged gap topic (e.g. "when is my increment due") does not justify a tool
        gap_topics = [g["topic"] for g in req.get("groundability", {}).get("gaps", [])]
        gap_only = bool(v["requirement_quote"].strip()) and name not in tool_caps and any(
            fact_support(g, v["requirement_quote"]) >= 0.5 or fact_support(v["requirement_quote"], g) >= 0.5 for g in gap_topics)
        decision = pre_ok and llm_yes and (bool(ev) or quote_ok) and not gap_only
        record = {
            "name": name,
            "llm_vote": bool(v["needed"]),
            "groundability_needs_it": name in tool_caps,
            "justification": v["justification"],
            "requirement_quote": v["requirement_quote"],
            "quote_verified": quote_ok,
            "evidence_terms": ev,
            "precondition": pre_note or "ok",
        }
        if decision:
            attached.append({**record, "reason": f"{v['justification']} (evidence: {', '.join(ev) or 'verified quote'})"})
        else:
            why = []
            if not pre_ok:
                why.append(f"precondition failed: {pre_note}")
            if not llm_yes:
                why.append("model judged it unnecessary")
            if llm_yes and not (ev or quote_ok):
                why.append("model wanted it but no requirement text justifies it (guard vetoed)")
            if gap_only:
                why.append(f"the only justification is a flagged gap topic ({v['requirement_quote']!r}) - a tool cannot make an ungrounded topic answerable")
            rejected.append({**record, "reason": "; ".join(why)})
    return attached, rejected


# ------------------------------------------------------------------ retrieval params
def choose_retrieval(docs: list[Document]) -> tuple[ChunkingConfig, RetrievalConfig, list[str]]:
    why = []
    text_docs = [d for d in docs if d.kind == "text"]
    tables = [d for d in docs if d.kind == "table"]
    paras = [len(p.split()) for d in text_docs for page in (d.pages or [d.text]) for h, p in _segment(page) if h is None and p.strip()]
    med_para = statistics.median(paras) if paras else 60
    total_words = sum(d.n_words for d in text_docs)
    if med_para < 45:
        cw = 140
        why.append(f"median paragraph is short ({med_para:.0f} words) -> 140-word chunks keep one rule per chunk")
    elif total_words > 40000:
        cw = 260
        why.append(f"large prose corpus ({total_words} words) -> 260-word chunks to keep the index compact")
    else:
        cw = 180
        why.append(f"median paragraph {med_para:.0f} words -> 180-word chunks (about 2-3 paragraphs)")
    ov = max(20, cw // 5)
    why.append(f"overlap {ov} words so rules split across a boundary stay retrievable")
    rows = 1
    if tables:
        n_rows = sum(len(t.rows) for t in tables)
        rows = 1 if n_rows <= 1500 else 5
        why.append(f"{len(tables)} table(s) with {n_rows} rows -> {rows} row(s) per chunk so each record is retrievable on its own")
    k = 5 if len(docs) <= 3 else 6
    why.append(f"{len(docs)} documents -> top_k={k}")
    bw = 0.6 if tables else 0.5
    why.append("BM25 weight 0.6: tables contain exact codes/IDs that lexical search matches best" if tables else "balanced hybrid (0.5 BM25 / 0.5 dense)")
    return ChunkingConfig(cw, ov, rows), RetrievalConfig(top_k=k, bm25_weight=bw, query_expansion=False, neighbor_expansion=False, min_dense_score=0.45), why


# ------------------------------------------------------------------ prompts
def known_gaps_section(req: dict) -> str:
    g = req.get("groundability", {})
    lines = []
    for x in g.get("gaps", []):
        lines.append(f"- {x['topic']}: NOT covered by the documents. If asked, say you do not have this information and refer the user to {req['escalation_contact']}.")
    for x in g.get("partial", []):
        if x.get("missing"):
            lines.append(f"- {x['topic']}: only partly covered. Missing: {x['missing']}. Answer what the sources say and state what is missing.")
    return "\n".join(lines) or "- None identified."


def generate_sections(req: dict, docs: list[Document], tools: list[dict], llm: LLM, style_key: str) -> dict:
    gaps = known_gaps_section(req)
    tool_line = ", ".join(f"{t['name']} ({REGISTRY[t['name']].description})" for t in tools) or "none"
    sec = llm.json(
        [{"role": "user", "content": prompts.render("generate_system_prompt", requirements=_req_text(req), profile=profile_text(docs),
                                                    gaps=gaps, tools=tool_line, style=STYLES[style_key])}],
        SECTIONS_SCHEMA, stage="generator.prompt", max_tokens=1600, temperature=0.3,
    )
    sec = {k: v.strip() for k, v in sec.items()}
    if not tools:
        sec["tool_rules"] = "No tools are attached. Never claim to look up live data, run calculations with external tools, or perform actions."
    must_refuse = "; ".join(req.get("must_refuse_topics", []))
    if must_refuse and must_refuse.lower()[:30] not in sec["refusal_policy"].lower():
        sec["refusal_policy"] += f"\n- Always refuse these topics: {must_refuse}."
    if req["escalation_contact"].lower() not in sec["refusal_policy"].lower():
        sec["refusal_policy"] += f"\n- When refusing, refer the user to {req['escalation_contact']}."
    sec["known_gaps"] = gaps
    sec["grounding_contract"] = GROUNDING_CONTRACT
    return sec


def output_contract(req: dict) -> dict:
    tone = req.get("tone", "").lower()
    concise = any(w in tone for w in ("concise", "brief", "short", "direct", "crisp"))
    return {
        "format": "json",
        "schema": {"answer": "string", "citations": ["S1|T1 labels"], "refused": "boolean", "confidence": "high|medium|low"},
        "rules": {
            "max_words": 120 if concise else 180,
            "require_citation_when_answering": True,
            "style": f"{req.get('tone', 'professional')}; language: {req.get('language', 'English')}",
            "refusal_must_include": req.get("escalation_contact", ""),
        },
        "rendering": "answer text, then 'Sources:' with cited document ids; low confidence answers carry a verify note",
    }


def build_definition(req: dict, docs: list[Document], docs_path: str, sections: dict, tools: list[dict], rejected: list[dict],
                     chunking: ChunkingConfig, rcfg: RetrievalConfig, rationale: list[str], model: str, prompt_variant: str) -> dict:
    return {
        "forgeai_version": "1.0",
        "name": req["agent_name"],
        "description": req["purpose"],
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": {"name": model, "temperature": 0.1},
        "system_prompt": {"variant": prompt_variant, "order": SECTION_ORDER, "sections": sections},
        "knowledge": {
            "docs_path": docs_path,
            "documents": corpus_fingerprint(docs),
            "chunking": config_dict(chunking),
            "retrieval": config_dict(rcfg),
            "rationale": rationale,
        },
        "guards": {"scope_guard": False},
        "tools": [{"name": t["name"], "reason": t["reason"]} for t in tools],
        "tools_rejected": [{"name": t["name"], "reason": t["reason"]} for t in rejected],
        "tool_selection_audit": tools + rejected,
        "output_contract": output_contract(req),
        "scope": {
            "must_answer": req["must_answer_topics"],
            "must_refuse": req["must_refuse_topics"],
            "gaps": req.get("groundability", {}).get("gaps", []),
            "escalation_contact": req["escalation_contact"],
        },
    }


def save_definition(defn: dict, path: Path) -> None:
    path.write_text(json.dumps(defn, indent=2))

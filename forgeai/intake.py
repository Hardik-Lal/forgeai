"""Stage 1 + 2: intake interview and groundability guard.

Intake    brief + document inventory -> draft requirements + <=5 clarifying
          questions -> answers (CLI, answers file, or defaults when unattended)
          -> final requirements.
Guard     every must-answer topic / requested capability is checked against
          the documents (retrieval + LLM verdict + *verified* verbatim quote)
          and the tool registry. Anything unsupported becomes a documented
          gap that the generated agent must decline - never hallucinate.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from . import prompts
from .documents import Document, profile_text
from .llm import LLM, LLMError
from .retrieval import Index, RetrievalConfig
from .textmatch import content_tokens, fact_support, numbers, quote_in
from .tools import REGISTRY, tool_catalog_text

MAX_QUESTIONS = 5

QUESTION_TOPICS = ["users_and_exclusions", "refusal_topics", "tone_and_length", "escalation", "unsupported_requests"]
_STR_LIST = {"type": "array", "items": {"type": "string"}}
REQ_PROPS = {
    "agent_name": {"type": "string"},
    "purpose": {"type": "string"},
    "scope": {"type": "string"},
    "audience": {"type": "string"},
    "tone": {"type": "string"},
    "must_answer_topics": _STR_LIST,
    "must_refuse_topics": _STR_LIST,
    "capabilities_requested": _STR_LIST,
    "escalation_contact": {"type": "string"},
    "language": {"type": "string"},
    "assumptions": _STR_LIST,
}
REQ_SCHEMA = {"type": "object", "properties": REQ_PROPS, "required": list(REQ_PROPS)}
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": REQ_SCHEMA,
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"about": {"type": "string", "enum": QUESTION_TOPICS}, "question": {"type": "string"},
                               "why": {"type": "string"}, "default": {"type": "string"}},
                "required": ["about", "question", "why", "default"],
            },
        },
    },
    "required": ["requirements", "questions"],
}
GROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported", "needs_tool"]},
        "evidence_quote": {"type": "string"},
        "evidence_label": {"type": "string"},
        "tool": {"type": "string"},
        "missing": {"type": "string"},
    },
    "required": ["verdict", "evidence_quote", "evidence_label", "tool", "missing"],
}

EVIDENCE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}}, "required": ["answer"]}

Asker = Callable[[int, str, str], str]  # (index, question, default) -> answer ("" = accept default)


def _clean_list(xs: list, limit: int) -> list[str]:
    seen, out = set(), []
    for x in xs:
        x = str(x).strip().strip(".")
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:limit]


def notes_asker(notes: str, llm: LLM) -> Asker:
    """Simulated requester: answers clarifying questions ONLY with verbatim lines from written notes.

    Used for reproducible demo runs. The model may only select note lines; every
    selected line is verified to exist in the notes, so the simulated requester
    cannot invent requirements. No verified line -> the default is used."""
    schema = {"type": "object", "properties": {"relevant_lines": {"type": "array", "items": {"type": "string"}}}, "required": ["relevant_lines"]}
    note_lines = [l.strip(" -*\t") for l in notes.splitlines() if l.strip(" -*\t")]

    def ask(i: int, q: str, default: str) -> str:
        try:
            v = llm.json([{"role": "user", "content":
                           "Here are a requester's notes, one per line:\n" + "\n".join(f"- {l}" for l in note_lines)
                           + f"\n\nQUESTION: {q}\n\nCopy VERBATIM the note lines (at most 2) that answer or are directly relevant to the question. "
                           "Return an empty list if no line is relevant."}], schema, stage="intake.requester", max_tokens=250)
        except LLMError:
            return ""
        picked = []
        for cand in v["relevant_lines"][:2]:
            for line in note_lines:
                if quote_in(cand, line, 0.9)[0] or (len(cand) > 15 and cand.strip(" -").lower() in line.lower()):
                    if line not in picked:
                        picked.append(line)
                    break
        ans = " ".join(picked)
        print(f"\nQ{i}: {q}\n  requester (from notes): {ans or '(not covered -> default: ' + default + ')'}")
        return ans

    ask.label = "simulated requester (verbatim from committed notes)"
    return ask


def draft_requirements(brief: str, docs: list[Document], llm: LLM) -> dict:
    """First intake step: draft requirements + at most MAX_QUESTIONS clarifying questions."""
    draft = llm.json(
        [{"role": "user", "content": prompts.render("intake_draft", brief=brief, profile=profile_text(docs), tools=tool_catalog_text(),
                                                    max_questions=MAX_QUESTIONS)}],
        DRAFT_SCHEMA, stage="intake", max_tokens=1800,
    )
    seen, qs = set(), []
    for q in draft["questions"]:  # at most one question per category, hard cap of MAX_QUESTIONS
        if q.get("about") not in seen:
            seen.add(q.get("about"))
            qs.append(q)
    draft["questions"] = qs[:MAX_QUESTIONS]
    return draft


def interview(brief: str, docs: list[Document], llm: LLM, ask: Asker | None, log: Callable = print) -> dict:
    """Run the intake dialogue and return requirements (with the transcript)."""
    draft = draft_requirements(brief, docs, llm)
    dialogue = []
    for i, q in enumerate(draft["questions"], 1):
        ans = ""
        if ask is not None:
            ans = (ask(i, q["question"], q["default"]) or "").strip()
        dialogue.append({
            "question": q["question"], "why": q["why"], "default": q["default"],
            "answer": ans or q["default"],
            "answered_by": (getattr(ask, "label", "requester") if ans else "default (unattended)" if ask is None else "default (accepted)"),
        })
    return finalize_requirements(brief, draft, dialogue, llm)


def finalize_requirements(brief: str, draft: dict, dialogue: list[dict], llm: LLM) -> dict:
    dia_text = "\n".join(
        f"Q{i}: {d['question']}\nA{i}: {d['answer'] if not d['answered_by'].startswith('default') else d['answer'] + ' (no answer - default assumed)'}"
        for i, d in enumerate(dialogue, 1)
    ) or "(no questions were needed)"
    final = llm.json(
        [{"role": "user", "content": prompts.render("intake_finalize", brief=brief, draft=json.dumps(draft["requirements"], indent=1), dialogue=dia_text)}],
        REQ_SCHEMA, stage="intake", max_tokens=1500,
    )
    final["must_answer_topics"] = _clean_list(final["must_answer_topics"], 10)
    final["must_refuse_topics"] = _clean_list(final["must_refuse_topics"], 8)
    final["capabilities_requested"] = _clean_list(final["capabilities_requested"], 6)
    final["brief"] = brief
    final["clarifications"] = dialogue
    return final


def groundability(req: dict, docs: list[Document], index: Index, llm: LLM, log: Callable = print) -> dict:
    """Check every must-answer topic and capability against documents + tools."""
    rcfg = RetrievalConfig(top_k=6, bm25_weight=0.5)
    tool_names = ", ".join(f"{t.name} ({t.needs})" for t in REGISTRY.values())
    results = []
    items = [("topic", t) for t in req["must_answer_topics"]] + [("capability", c) for c in req.get("capabilities_requested", [])]
    def _relevant(topic: str, quote: str, context: str = "") -> bool:
        """A real quote is not enough: it must be ABOUT the topic (term overlap in the quote's chunk, else a yes/no check)."""
        terms, words = set(content_tokens(topic)), set(content_tokens(quote + " " + context))
        if terms and all(any(w == t or (len(t) >= 5 and len(w) >= 5 and w[:5] == t[:5]) for w in words) for t in terms):
            return True
        try:
            r = llm.json([{"role": "user", "content": prompts.render("groundability_relevance", topic=topic, sentence=quote[:400])}],
                         EVIDENCE_SCHEMA, stage="groundability", max_tokens=20)
            return r["answer"] == "yes"
        except LLMError:
            return False

    def check(topic: str, hits) -> tuple[str, str, dict | None, dict]:
        excerpts = "\n\n".join(f"[E{i}] ({h.chunk.doc_id})\n{h.chunk.text[:900]}" for i, h in enumerate(hits, 1))
        try:
            v = llm.json(
                [{"role": "user", "content": prompts.render("groundability", topic=topic, context=req.get("scope", ""), excerpts=excerpts, tools=tool_names)}],
                GROUND_SCHEMA, stage="groundability", max_tokens=400,
            )
        except LLMError as e:
            v = {"verdict": "unsupported", "evidence_quote": "", "evidence_label": "", "tool": "", "missing": f"checker failed: {e}"}
        verdict, note, evidence = v["verdict"], "", None
        if verdict in ("supported", "partial"):
            # Do not trust the verdict: the quote must really be in the cited excerpt (or any excerpt).
            label = v["evidence_label"].strip("[] ").upper()
            cand = []
            if label.startswith("E") and label[1:].isdigit() and 1 <= int(label[1:]) <= len(hits):
                cand.append(hits[int(label[1:]) - 1])
            cand += [h for h in hits if h not in cand]
            for h in cand:
                q = re.sub(r"^\s*\[E\d+\]\s*", "", v["evidence_quote"])
                ok, score = quote_in(q, h.chunk.text)
                if ok:
                    evidence = {"doc_id": h.chunk.doc_id, "chunk_id": h.chunk.chunk_id, "quote": q[:300], "match": round(score, 2)}
                    break
            if evidence is None:
                note = "model claimed support but its evidence quote was not found in the documents"
                verdict = "unverified"
            elif not _relevant(topic, evidence["quote"], next((h.chunk.text for h in hits if h.chunk.chunk_id == evidence["chunk_id"]), "")):
                note = f"model's evidence quote exists but is not about the topic: \"{evidence['quote'][:80]}...\""
                verdict, evidence = "unsupported", None
        if verdict == "needs_tool":
            tool = v["tool"].strip()
            if tool not in REGISTRY:
                note = f"needs a capability not in the tool registry ({tool or 'unspecified'})"
                verdict = "unsupported"
        return verdict, note, evidence, v

    vocab = {w for c in index.chunks for w in content_tokens(c.text)}
    for kind, topic in items:
        hits = index.search(f"{topic}. {req.get('scope', '')}"[:300], rcfg)
        hits = hits + [h for h in index.search(topic, rcfg) if h.chunk.chunk_id not in {x.chunk.chunk_id for x in hits}][:3]
        verdict, note, evidence, v = check(topic, hits)
        rechecked = False
        if verdict in ("unsupported", "unverified"):
            # Second opinion on a focused excerpt set: chunks that literally mention the topic's words.
            # A 7B model often misses one relevant line among nine excerpts; we still require a verified quote.
            terms = set(content_tokens(topic))

            def match(t: str, words: set) -> bool:  # prefix match so "probationary" finds "probation"
                return any(w == t or (len(w) >= 5 and len(t) >= 5 and (w.startswith(t[:max(5, len(t) - 3)]) or t.startswith(w))) for w in words)

            scored = []
            for c in index.chunks:
                words = set(content_tokens(c.text))
                frac = sum(match(t, words) for t in terms) / len(terms) if terms else 0
                if frac > 0:
                    scored.append((frac, c))
            best = max((f for f, _ in scored), default=0)
            lexical = [c for f, c in scored if f >= best and best >= 0.5]
            # a topic whose key word never occurs anywhere in the corpus cannot be overturned to "supported"
            corpus_words = vocab
            absent = [t for t in terms if not match(t, corpus_words)]
            if absent:
                lexical = []
                note = f"topic term(s) {absent} never appear in the documents"
            if lexical:
                order = {h.chunk.chunk_id: i for i, h in enumerate(index.search(topic, RetrievalConfig(top_k=len(index.chunks), bm25_weight=0.8)))}
                lexical.sort(key=lambda c: order.get(c.chunk_id, 999))
                focused = [h for h in index.search(topic, RetrievalConfig(top_k=len(index.chunks), bm25_weight=0.8)) if h.chunk in lexical][:4]
                if focused:
                    rechecked = True
                    sents = []
                    for h in focused:
                        for sent in re.split(r"(?<=[.;])\s+", h.chunk.text.split("\n", 1)[-1]):
                            words = set(content_tokens(sent))
                            if sum(match(t, words) for t in terms) >= max(1, best * len(terms) - 1e-9) and 5 <= len(sent.split()) <= 60:
                                sents.append((sent.strip(), h))
                    for sent, h in sents[:4]:
                        try:
                            e2 = llm.json([{"role": "user", "content": prompts.render("groundability_evidence", topic=topic, sentence=sent)}],
                                          EVIDENCE_SCHEMA, stage="groundability", max_tokens=20)
                        except LLMError:
                            continue
                        if e2["answer"] == "yes":
                            verdict, hits = "partial", focused
                            evidence = {"doc_id": h.chunk.doc_id, "chunk_id": h.chunk.chunk_id, "quote": sent[:300], "match": 1.0}
                            note = "first pass said unsupported; sentence-level evidence check found a rule -> partial"
                            break
        status = {"supported": "grounded", "partial": "partially_grounded", "needs_tool": "tool", "unverified": "gap", "unsupported": "gap"}[verdict]
        results.append({
            "kind": kind, "topic": topic, "status": status, "verdict": verdict, "tool": v["tool"] if verdict == "needs_tool" else "",
            "evidence": evidence, "missing": v["missing"], "note": note,
            "top_docs": sorted({h.chunk.doc_id for h in hits[:3]}), "rechecked": rechecked,
        })
        log(f"  groundability: [{status:>18}] {kind}: {topic}" + (f"  ({note})" if note else ""))
    gaps = [r for r in results if r["status"] == "gap"]
    return {
        "checks": results,
        "gaps": [{"topic": g["topic"], "kind": g["kind"], "reason": g["note"] or g["missing"] or "not found in documents"} for g in gaps],
        "partial": [{"topic": r["topic"], "missing": r["missing"]} for r in results if r["status"] == "partially_grounded"],
        "grounded_topics": [r["topic"] for r in results if r["status"] in ("grounded", "partially_grounded")],
        "tool_capabilities": [{"topic": r["topic"], "tool": r["tool"]} for r in results if r["status"] == "tool"],
    }


def requirements_markdown(req: dict) -> str:
    g = req.get("groundability", {})
    L = [f"# Requirements - {req['agent_name']}", "", f"**Purpose:** {req['purpose']}", "", "## Brief", "", f"> {req.get('brief', '').strip()}", ""]
    L += ["## Scope", "", req["scope"], "", "## Audience", "", req["audience"], "", "## Tone", "", req["tone"], "",
          f"**Language:** {req['language']}  ", f"**Escalation contact:** {req['escalation_contact']}", ""]
    L += ["## Must-answer topics", ""] + [f"- {t}" for t in req["must_answer_topics"]] + [""]
    L += ["## Must-refuse topics", ""] + [f"- {t}" for t in req["must_refuse_topics"]] + [""]
    if req.get("capabilities_requested"):
        L += ["## Capabilities requested", ""] + [f"- {t}" for t in req["capabilities_requested"]] + [""]
    if g:
        L += ["## Groundability check", "", "| Item | Status | Evidence / reason |", "|---|---|---|"]
        for c in g["checks"]:
            ev = f"`{c['evidence']['doc_id']}`: \"{c['evidence']['quote'][:110]}\"" if c.get("evidence") else (c["note"] or c["missing"] or (f"tool: {c['tool']}" if c["tool"] else ""))
            L.append(f"| {c['kind']}: {c['topic']} | **{c['status']}** | {ev.replace('|', '/')} |")
        L.append("")
        if g["gaps"]:
            L += ["### Flagged gaps (the agent will NOT answer these; it will say so and escalate)", ""]
            L += [f"- **{x['topic']}** - {x['reason']}" for x in g["gaps"]] + [""]
    if req.get("clarifications"):
        L += ["## Clarification dialogue", ""]
        for i, d in enumerate(req["clarifications"], 1):
            L += [f"{i}. **Q:** {d['question']}  ", f"   **A:** {d['answer']} _({d['answered_by']})_"]
        L.append("")
    if req.get("assumptions"):
        L += ["## Assumptions", ""] + [f"- {a}" for a in req["assumptions"]] + [""]
    if req.get("approval"):
        L += ["## Approval", "", f"{req['approval']['status']} by {req['approval']['by']} at {req['approval']['at']}", ""]
    return "\n".join(L)

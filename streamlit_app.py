"""ForgeAI web UI: build an agent (with the human checkpoint), inspect runs, chat with any agent.

    streamlit run streamlit_app.py
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
UPLOADS = RUNS / "_uploads"

st.set_page_config(page_title="ForgeAI", layout="wide")


# ---------------------------------------------------------------- helpers
@st.cache_resource(show_spinner=False)
def get_llm():
    from forgeai.llm import LLM
    return LLM()


def ollama_status() -> dict:
    try:
        return get_llm().ping()
    except Exception as e:  # pragma: no cover
        return {"error": str(e)}


def list_runs() -> list[Path]:
    if not RUNS.exists():
        return []
    return sorted([p for p in RUNS.iterdir() if (p / "agent.json").exists()], key=lambda p: p.stat().st_mtime, reverse=True)


@st.cache_resource(show_spinner="Loading agent and building its index...")
def load_agent(path: str, mtime: float):
    from forgeai.runtime import Agent
    defn = json.loads(Path(path).read_text())
    return Agent(defn, get_llm(), base_dir=Path(path).parent)


with st.sidebar:
    st.title("ForgeAI")
    st.caption("The agent that builds, tests and repairs agents on a local open model.")
    s = ollama_status()
    if "error" in s:
        st.error(f"Ollama not reachable: {s['error']}")
    else:
        st.success(f"Ollama OK - chat model {'ready' if s['chat_model_ok'] else 'MISSING'}, embeddings {'ready' if s['embed_model_ok'] else 'MISSING'}")
    st.markdown("**CLI equivalents**\n\n`forge build ...`\n\n`forge chat runs/<run>`\n\n`forge regress ...`")

tab_build, tab_runs, tab_chat = st.tabs(["Build an agent", "Runs and reports", "Chat with an agent"])

# ---------------------------------------------------------------- build
with tab_build:
    ss = st.session_state
    ss.setdefault("stage", "brief")
    st.subheader("1. Brief and documents")
    brief = st.text_area("One-paragraph brief", value=ss.get("brief", ""), height=110,
                         placeholder="I need an assistant that answers HR policy questions for shore staff...")
    c1, c2 = st.columns(2)
    with c1:
        docs_path = st.text_input("Document folder (path on this machine)", value=ss.get("docs_path", ""))
    with c2:
        ups = st.file_uploader("...or upload documents", accept_multiple_files=True, type=["pdf", "txt", "md", "csv", "tsv", "json", "docx"])
    run_name = st.text_input("Run name", value=ss.get("run_name", f"run_{dt.datetime.now():%Y%m%d_%H%M}"))
    with st.expander("Factory settings"):
        tournament = st.slider("Prompt candidates (tournament)", 1, 3, 3)
        iters = st.slider("Max optimizer iterations", 0, 3, 3)
        cases = st.slider("Target eval cases", 20, 40, 26)
        minutes = st.number_input("Time budget (minutes)", 10, 600, 180)

    if st.button("Start intake interview", type="primary", disabled=not brief.strip()):
        if ups:
            folder = UPLOADS / run_name
            folder.mkdir(parents=True, exist_ok=True)
            for f in ups:
                (folder / f.name).write_bytes(f.getbuffer())
            docs_path = str(folder)
        if not docs_path or not Path(docs_path).is_dir():
            st.error("Please give a document folder or upload documents.")
        else:
            from forgeai.documents import load_folder
            from forgeai.intake import draft_requirements
            with st.spinner("Reading documents and drafting clarifying questions..."):
                docs = load_folder(docs_path)
                ss.draft = draft_requirements(brief, docs, get_llm())
            ss.update(stage="questions", brief=brief, docs_path=docs_path, run_name=run_name)

    if ss.stage in ("questions", "review", "running"):
        st.subheader("2. Clarifying questions (max 5)")
        with st.form("answers"):
            answers = []
            for i, q in enumerate(ss.draft["questions"], 1):
                answers.append(st.text_input(f"Q{i}. {q['question']}", key=f"ans{i}", help=f"Why: {q['why']}", placeholder=f"default: {q['default']}"))
            done = st.form_submit_button("Submit answers and check groundability")
        if done:
            from forgeai.documents import load_folder
            from forgeai.intake import finalize_requirements, groundability, requirements_markdown
            from forgeai.retrieval import ChunkingConfig, Index
            dialogue = [{"question": q["question"], "why": q["why"], "default": q["default"], "answer": a.strip() or q["default"],
                         "answered_by": "requester" if a.strip() else "default (accepted)"} for q, a in zip(ss.draft["questions"], answers)]
            with st.spinner("Writing requirements and checking every topic against the documents..."):
                llm = get_llm()
                docs = load_folder(ss.docs_path)
                req = finalize_requirements(ss.brief, ss.draft, dialogue, llm)
                req["groundability"] = groundability(req, docs, Index(docs, ChunkingConfig(), llm), llm, log=lambda m: None)
            ss.req = req
            ss.stage = "review"

    if ss.stage in ("review", "running"):
        from forgeai.intake import requirements_markdown
        st.subheader("3. Human checkpoint - approve or edit the requirements")
        gaps = ss.req.get("groundability", {}).get("gaps", [])
        if gaps:
            st.warning("Groundability guard: the brief asks for things the documents cannot support. The agent will decline these:\n\n"
                       + "\n".join(f"- **{g['topic']}**: {g['reason']}" for g in gaps))
        left, right = st.columns(2)
        with left:
            st.markdown(requirements_markdown(ss.req))
        with right:
            editable = {k: ss.req[k] for k in ["agent_name", "purpose", "scope", "audience", "tone", "must_answer_topics", "must_refuse_topics",
                                               "capabilities_requested", "escalation_contact", "language"]}
            txt = st.text_area("Edit (JSON)", json.dumps(editable, indent=2), height=520)
            if st.button("Approve and build the agent", type="primary", disabled=ss.stage == "running"):
                try:
                    edited = json.loads(txt)
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON: {e}")
                    st.stop()
                req = {**ss.req, **edited}
                req["approval"] = {"status": "approved" + (" with edits" if edited != editable else ""), "by": "requester (web UI)",
                                   "at": dt.datetime.now().isoformat(timespec="seconds")}
                out = RUNS / ss.run_name
                out.mkdir(parents=True, exist_ok=True)
                (out / "approved_requirements.json").write_text(json.dumps(req, indent=2))
                cmd = [sys.executable, "-m", "forgeai.cli", "build", "--brief", ss.brief, "--docs", ss.docs_path, "--out", str(out),
                       "--requirements", str(out / "approved_requirements.json"), "--tournament", str(tournament),
                       "--iterations", str(iters), "--cases", str(cases), "--max-minutes", str(minutes)]
                with open(out / "ui_stdout.log", "w") as f:
                    subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
                ss.stage = "running"

    if ss.stage == "running":
        st.subheader("4. Factory running")
        log = RUNS / ss.run_name / "factory.log"
        st.button("Refresh progress")
        if log.exists():
            lines = log.read_text().splitlines()
            st.code("\n".join(lines[-40:]), language="text")
            if lines and "DONE" in lines[-1]:
                st.success("Finished - open the 'Runs and reports' tab.")
        err = RUNS / ss.run_name / "ui_stdout.log"
        if err.exists() and "Traceback" in err.read_text():
            st.error(err.read_text()[-3000:])

# ---------------------------------------------------------------- runs
with tab_runs:
    runs = list_runs()
    if not runs:
        st.info("No finished runs yet.")
    else:
        run = st.selectbox("Run", runs, format_func=lambda p: p.name)
        sc_path = run / "07_scorecard.json"
        if sc_path.exists():
            sc = json.loads(sc_path.read_text())
            fo, fh = sc["final"]["overall"], sc["final"]["heldout"]
            m = st.columns(5)
            m[0].metric("Overall pass", f"{fo['passed']}/{fo['n']}", f"CI {fo['ci95'][0]:.0%}-{fo['ci95'][1]:.0%}", delta_color="off")
            m[1].metric("Dev (final)", f"{sc['final']['dev']['passed']}/{sc['final']['dev']['n']}",
                        f"{sc['final']['dev']['passed'] - sc['baseline']['dev']['passed']:+d} vs baseline")
            if fh:
                m[2].metric("Held-out (final)", f"{fh['passed']}/{fh['n']}", f"{fh['passed'] - sc['baseline']['heldout']['passed']:+d} vs baseline")
            m[3].metric("Judge self-test", f"{sc['judge_calibration']['accuracy']:.0%}")
            m[4].metric("Tokens", f"{sc['budget']['tokens_total']:,}")
            st.line_chart({"dev pass rate": [it["dev_pass_rate"] for it in sc["iterations"]]})
        report = run / "REPORT.md"
        if report.exists():
            with st.expander("REPORT.md", expanded=True):
                st.markdown(report.read_text())
        for name in ["01_requirements.md", "03_evalset.md", "06_iteration_log.md", "agent_system_prompt.txt"]:
            p = run / name
            if p.exists():
                with st.expander(name):
                    st.markdown(p.read_text()) if name.endswith(".md") else st.code(p.read_text(), language="text")
        st.download_button("Download agent.json", (run / "agent.json").read_text(), file_name=f"{run.name}_agent.json")

# ---------------------------------------------------------------- chat
with tab_chat:
    runs = list_runs()
    if not runs:
        st.info("Build an agent first.")
    else:
        run = st.selectbox("Agent", runs, format_func=lambda p: json.loads((p / "agent.json").read_text())["name"] + f"  ({p.name})", key="chat_run")
        agent_path = run / "agent.json"
        agent = load_agent(str(agent_path), agent_path.stat().st_mtime)
        st.caption(f"{agent.d['description']} | tools: {', '.join(agent.tools) or 'none'} | {len(agent.index.chunks)} chunks")
        hist_key = f"hist_{run.name}"
        st.session_state.setdefault(hist_key, [])
        for turn in st.session_state[hist_key]:
            with st.chat_message(turn["role"]):
                st.markdown(turn["content"])
                if turn.get("trace"):
                    with st.expander("retrieval and tool trace"):
                        st.json(turn["trace"])
        q = st.chat_input("Ask the agent...")
        if q:
            st.session_state[hist_key].append({"role": "user", "content": q})
            with st.chat_message("user"):
                st.markdown(q)
            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    history = [{"role": t["role"], "content": t["content"]} for t in st.session_state[hist_key][:-1]][-4:]
                    tr = agent.answer(q, history=history)
                text = agent.render(tr)
                st.markdown(text)
                trace = {k: tr[k] for k in ("retrieved", "tool_calls", "contract_errors", "latency_s")}
                with st.expander("retrieval and tool trace"):
                    st.json(trace)
            st.session_state[hist_key].append({"role": "assistant", "content": text, "trace": trace})

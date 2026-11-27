"""ForgeAI command line.

  forge build   --brief brief.txt --docs folder/ --out runs/x [--interactive] [--approve]
  forge chat    runs/x/agent.json [-q "question"]
  forge regress --run runs/x --docs new_folder/ --out runs/x_v2
  forge doctor
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EDITABLE = ["agent_name", "purpose", "scope", "audience", "tone", "must_answer_topics", "must_refuse_topics",
            "capabilities_requested", "escalation_contact", "language"]


def _read_brief(arg: str) -> str:
    p = Path(arg)
    return p.read_text() if p.is_file() else arg


def cli_asker(answers: list[str] | None, interactive: bool):
    if answers is not None:
        def ask(i, q, default):
            a = answers[i - 1] if i - 1 < len(answers) else ""
            print(f"\nQ{i}: {q}\n  -> {a or '(default) ' + default}")
            return a
        return ask
    if not interactive:
        return None

    def ask(i, q, default):
        print(f"\nQ{i}: {q}\n   (press Enter to accept default: {default})")
        try:
            return input("   > ").strip()
        except EOFError:
            return ""
    return ask


def cli_approver(req: dict, md: str):
    print("\n" + "=" * 78 + "\n" + md + "\n" + "=" * 78)
    while True:
        try:
            c = input("\n[a]pprove / [e]dit in $EDITOR / [q]uit ? ").strip().lower() or "a"
        except EOFError:
            c = "a"
        if c.startswith("a"):
            return req
        if c.startswith("q"):
            return None
        if c.startswith("e"):
            sub = {k: req[k] for k in EDITABLE if k in req}
            with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
                json.dump(sub, f, indent=2)
                path = f.name
            subprocess.call([os.environ.get("EDITOR", "nano"), path])
            try:
                edited = json.loads(Path(path).read_text())
                new = dict(req)
                new.update({k: v for k, v in edited.items() if k in EDITABLE})
                return new
            except json.JSONDecodeError as e:
                print(f"invalid JSON ({e}); try again")


def cmd_build(a) -> int:
    from .pipeline import run_factory

    answers = None
    if a.answers:
        data = json.loads(Path(a.answers).read_text())
        answers = data if isinstance(data, list) else [data[k] for k in sorted(data)]
    res = run_factory(
        _read_brief(a.brief), a.docs, a.out,
        ask=cli_asker(answers, a.interactive),
        approve=cli_approver if a.approve else None,
        model=a.model, judge_model=a.judge_model, max_tokens=a.max_tokens, max_minutes=a.max_minutes,
        n_cases=a.cases, tournament_size=a.tournament, max_iters=a.iterations, use_cache=not a.no_cache,
        requester_notes=Path(a.requester_notes).read_text() if a.requester_notes else None,
        requirements_override=json.loads(Path(a.requirements).read_text()) if a.requirements else None,
    )
    return 0 if res["status"] == "ok" else 1


def cmd_chat(a) -> int:
    from .llm import LLM
    from .runtime import Agent

    path = Path(a.agent)
    if path.is_dir():
        path = path / "agent.json"
    defn = json.loads(path.read_text())
    llm = LLM(model=a.model or defn.get("model", {}).get("name"))
    agent = Agent(defn, llm, base_dir=path.parent)
    tools = ", ".join(agent.tools) or "none"
    print(f"\n{defn['name']} - {defn['description']}\n(tools: {tools}; {len(agent.index.chunks)} chunks from {len(agent.docs)} documents)")
    if a.question:
        tr = agent.answer(a.question)
        print("\n" + agent.render(tr))
        if a.debug:
            print(json.dumps({k: tr[k] for k in ("retrieved", "tool_calls", "contract_errors", "latency_s")}, indent=1))
        return 0
    print("Type a question (or 'exit'). Prefix with '/debug ' to see retrieval and tool traces.\n")
    history: list[dict] = []
    while True:
        try:
            q = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("exit", "quit"):
            break
        debug = q.startswith("/debug ")
        q = q.removeprefix("/debug ").strip()
        tr = agent.answer(q, history=history)
        print("\nagent > " + agent.render(tr) + "\n")
        if debug:
            print(json.dumps({k: tr[k] for k in ("retrieved", "tool_calls", "contract_errors", "latency_s")}, indent=1) + "\n")
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": tr["output"]["answer"]}]
    return 0


def cmd_regress(a) -> int:
    from .regression import run_regression

    run_regression(a.run, a.docs, a.out, model=a.model, reoptimize=not a.no_optimize)
    return 0


def cmd_doctor(a) -> int:
    from .llm import LLM

    llm = LLM()
    try:
        s = llm.ping()
    except Exception as e:
        print(f"Ollama not reachable: {e}\nStart it with: brew services start ollama  (or: ollama serve)")
        return 1
    print(json.dumps(s, indent=1))
    if not s["chat_model_ok"]:
        print(f"missing chat model -> ollama pull {llm.model}")
    if not s["embed_model_ok"]:
        print(f"missing embedding model -> ollama pull {llm.embed_model}")
    return 0 if s["chat_model_ok"] and s["embed_model_ok"] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="forge", description="ForgeAI - the agent that builds agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="run the factory end to end")
    b.add_argument("--brief", required=True, help="brief text or path to a text file")
    b.add_argument("--docs", required=True, help="folder of documents")
    b.add_argument("--out", required=True, help="output run folder")
    b.add_argument("--interactive", action="store_true", help="ask the clarifying questions on the terminal")
    b.add_argument("--answers", help="JSON list of answers to the clarifying questions (scripted dialogue)")
    b.add_argument("--requirements", help="approved requirements JSON (skips the intake interview)")
    b.add_argument("--requester-notes", help="text file; a simulated requester answers the clarifying questions only from it")
    b.add_argument("--approve", action="store_true", help="human checkpoint: approve/edit requirements before generation")
    b.add_argument("--model", default=None)
    b.add_argument("--judge-model", default=None, help="optional different local model for judging")
    b.add_argument("--max-tokens", type=int, default=3_000_000)
    b.add_argument("--max-minutes", type=float, default=180)
    b.add_argument("--cases", type=int, default=26)
    b.add_argument("--tournament", type=int, default=3, help="number of prompt candidates (1 disables)")
    b.add_argument("--iterations", type=int, default=3)
    b.add_argument("--no-cache", action="store_true")
    b.set_defaults(fn=cmd_build)

    c = sub.add_parser("chat", help="chat with any produced agent definition")
    c.add_argument("agent", help="path to agent.json or a run folder")
    c.add_argument("-q", "--question")
    c.add_argument("--model", default=None)
    c.add_argument("--debug", action="store_true")
    c.set_defaults(fn=cmd_chat)

    r = sub.add_parser("regress", help="documents changed: rebuild and re-run the old eval set")
    r.add_argument("--run", required=True, help="previous run folder")
    r.add_argument("--docs", required=True, help="new document folder")
    r.add_argument("--out", required=True)
    r.add_argument("--model", default=None)
    r.add_argument("--no-optimize", action="store_true")
    r.set_defaults(fn=cmd_regress)

    d = sub.add_parser("doctor", help="check Ollama and models")
    d.set_defaults(fn=cmd_doctor)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

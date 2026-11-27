# ForgeAI - the agent that builds agents

ForgeAI takes **a one-paragraph brief and a folder of documents** and, unattended, produces a **working, tested, document-grounded agent**: a declarative definition (system prompt, chunking and retrieval parameters, tool selection, output contract), an auto-generated evaluation set, a judge, a critic-optimizer loop that applies targeted fixes, and an honest report of what still fails.

Everything runs on a **small local open model** (`qwen2.5:7b-instruct` + `nomic-embed-text` via Ollama). The design assumes that model is imperfect and builds **deterministic checks that catch it lying**.

```
brief + docs
   |
   v
[1 Intake interviewer] --(<=5 questions: CLI / answers file / simulated requester / defaults)--> requirements
   |
   v
[2 Groundability guard]  each must-answer topic -> retrieve -> LLM verdict -> VERBATIM QUOTE VERIFIED in the docs
   |                     unsupported -> flagged gap (agent must decline + escalate), never hallucinated
   v
[Human checkpoint]       approve / edit requirements (CLI --approve, or web UI)            (stretch)
   |
   v
[3 Agent generator]      tools: LLM vote AND requirement evidence AND precondition
   |                     retrieval params from corpus statistics (with rationale)
   |                     3 candidate system prompts -> [Prompt tournament on dev split]   (stretch)
   v
[4 Eval generator]       >=20 cases FROM THE DOCUMENTS: answerable, ambiguous, multi-doc,
   |                     out-of-scope, gap, prompt-injection, tool (values computed in code)
   |                     every fact verified against its seed passage; dev / held-out split
   v
[5 Judge]                deterministic first (numbers, dates, citations, refusal, tools);
   |                     LLM only for paraphrases, every verdict needs a verified quote;
   |                     per-run calibration probes (incl. verbose-wrong) gate the LLM judge
   v
[6 Critic-optimizer]     classify failures -> ONE targeted fix (retrieval knob or ONE prompt
   |                     section) -> re-run -> keep only if dev improves; <=3 iterations;
   |                     budget + early stop; anti-overfitting guard on patches       (stretch)
   v
[7 Honest report]        REPORT.md: dev AND held-out scores with 95% CI, what still fails and
                         why, iteration scorecard, diffs, judge reliability, threats to validity
[8 Runnable product]     forge chat <run>  |  Streamlit chat  -> loads ANY agent.json
[Regression mode]        docs change -> text diff -> rebuild -> old eval set (real regressions vs stale tests)
                         + fresh delta tests on the changed sentences the old set never exercised (stretch)
```

## Quick start

```bash
# 1. Local model (macOS; on Linux use the install script from ollama.com)
brew install ollama
launchctl setenv OLLAMA_NUM_PARALLEL 4      # eval cases run concurrently (~3x throughput)
brew services start ollama
ollama pull qwen2.5:7b-instruct && ollama pull nomic-embed-text

# 2. Install
uv venv --python 3.11 .venv && uv pip install --python .venv -e '.[ui,dev]'
source .venv/bin/activate
forge doctor                         # checks Ollama + models
# If `forge` says "No module named forgeai": macOS can flag the venv's .pth files as hidden, and Python
# skips hidden .pth files. Fix with `chflags nohidden .venv/lib/python3.11/site-packages/*.pth`,
# or run `python -m forgeai.cli ...` from the repo root instead of `forge ...`.

# 3. Run the factory on an unseen brief + folder (fully unattended)
forge build --brief "I need an assistant that ..." --docs path/to/folder --out runs/review

#    interactive dialogue + human checkpoint instead:
forge build --brief brief.txt --docs path/to/folder --out runs/review --interactive --approve

# 4. Use the produced agent
forge chat runs/review               # or: forge chat runs/review -q "question" --debug

# 5. Web UI (factory with checkpoint, reports, chat)
streamlit run streamlit_app.py

# 6. Regression mode: documents changed
forge regress --run runs/hr_policy --docs demos/hr_policy/docs_v2 --out runs/hr_policy_regression --no-optimize
```

Useful `forge build` flags: `--tournament 1..3`, `--iterations 0..3`, `--cases N`, `--max-tokens`, `--max-minutes` (budget), `--judge-model <other local model>`, `--requester-notes notes.txt`, `--requirements approved.json`, `--no-cache`.

## The two demonstrations (committed in full)

| | `runs/hr_policy` | `runs/ship_spares` |
|---|---|---|
| Brief | HR policy Q&A for shore staff (Mumbai/Singapore) | Ship-spares procurement for superintendents |
| Documents | 3 policy PDFs | catalogue CSV (49 parts) + 2 supplier-terms PDFs + procurement procedure PDF |
| Traps | asks for salary bands / increments (not in docs); needs **no** tools (the model votes for calculator/current_date, the gap veto overrules it) | asks for live freight rates (not in docs, no tool); needs csv_lookup + calculator + current_date; "port/vessel" wording tempts the weather tool |
| Result (overall / held-out) | 22/24 (92%) / 8/8 | 17/24 (71%) / 4/8 |

Each run folder contains: `brief.txt`, `requester_notes.txt`, `01_requirements.{md,json}` (incl. groundability table and dialogue), `02_agent_candidates/`, `agent_baseline.json`, **`agent.json`** (final definition), `agent_system_prompt.txt`, **`03_evalset.{md,json}`** (generated test set + rejected candidates), `04_judge_calibration.json`, `05_tournament.json`, **`06_iteration_log.md`** / `06_iterations.json` (every change: what, why, which failures, score before/after, diff), **`07_scorecard.json`**, `08_final_results.json` (every answer with its verdict), **`REPORT.md`**, `prompts_snapshot/` (exact prompts used), `factory.log`.

Headline results, a manual audit of every failing case, and the regression demo (`runs/hr_policy_regression`) are in [`RESULTS.md`](RESULTS.md).

## Design decisions (and why)

### Distrust the pipeline
| Where the 7B model could lie | Check that catches it |
|---|---|
| Groundability: "yes, the docs cover salary bands" | The verdict must come with a verbatim quote, which is verified character by character against the retrieved chunk, and the quote (or its chunk) must actually be about the topic. A "no" is re-checked sentence by sentence (docs say "lead time", brief says "delivery dates"), but a topic whose key word never occurs in the corpus can't be overturned to "supported". |
| Tool selection: "attach weather, just in case" | The tool is attached only if the LLM votes yes **and** lexical evidence or a verified quote in the brief or requirements justifies it **and** its precondition holds (e.g. csv_lookup needs a table). |
| Eval generation: facts that aren't in the passage | Each key fact must be supported by its seed passage (numbers must match exactly). Leaky, duplicate and unverifiable cases are rejected and logged. |
| Tool-case ground truth | Computed in code (row values, `qty x price`, `today + lead time`), never by the LLM. |
| Judge: "yes, the answer states the fact" | Every LLM "yes" needs a quote that exists in the answer and contains the figure. Numbers and dates are compared deterministically first. |
| Judge: false hallucination flags | An "unsupported claim" whose content is in the sources is overruled; overrules are counted in the report. |
| Judge drift | Calibration probes each run (positive, unrelated answer, perturbed number, verbose-wrong). Below 80% accuracy, LLM verdicts are disabled. |
| Eval generation: true-but-irrelevant facts attached to a question | Each multi-doc and delta fact must pass a "does the question ask for this?" check. Scope cases are classified against the retrieved excerpts, so "out-of-scope" questions the docs actually answer are rejected. |
| Regression: "nothing broke" | Old tests are mapped to the changed sentences. Changes no old test covers get fresh delta tests. |
| Optimizer overfitting | The held-out split is never shown to the optimizer. Patches that copy eval questions or facts are rejected (n-gram check). A fix is kept only if the dev pass count strictly increases. |
| Agent: invented figures | Every number in an answer must appear in the retrieved context, the tool output or the question. Citations must reference real labels (output contract plus one repair attempt). |

### Judge failure modes
- **Position bias**: nothing is ever judged pairwise. The tournament ranks candidates by absolute eval pass counts.
- **Verbosity bias**: one atomic fact at a time, a rubric that says length is irrelevant, and a `verbose-wrong` calibration probe (a wrong answer padded with confident filler must still get "no").
- **Self-preference**: the judge is the same model, so it never sees the model-written reference answer, only document-verified facts; deterministic checks take precedence; `--judge-model` lets you use a different local model.

### Targeted fixes, not regeneration
The system prompt is stored as named sections (`role`, `scope`, `answering_rules`, `refusal_policy`, `known_gaps`, `tool_rules`, `citation_rules`, plus a locked `grounding_contract`). Failures map to fixes:

| Category (deterministic classifier) | Fix |
|---|---|
| `retrieval_miss` (expected source not retrieved) | One retrieval knob, chosen from rank diagnostics: `top_k` if the source is just below the cut-off, BM25 weight toward whichever ranker finds it earlier, query expansion, neighbour expansion, smaller chunks |
| `wrong_refusal` | Rewrite `refusal_policy` |
| `hallucination` | Rewrite `answering_rules` and set temperature to 0 (an out-of-scope answer counts as a scope leak, below) |
| scope leak (answered a must-refuse question) | Structural first: enable the runtime `scope_guard`, a separate yes/no gate before answering. Otherwise rewrite `refusal_policy` |
| incomplete answer | Structural first: `top_k` +2 (more context). Otherwise rewrite `answering_rules` |
| `prompt_defect` (citation / tool use / format) | Rewrite the one mapped section |

Failures are prioritised by severity-weighted cost (scope leak or hallucination = 3, wrong refusal or tool misuse = 2, retrieval miss = 1.5, incomplete or citation = 1), so one leak into a must-refuse topic outranks two incomplete answers. Each iteration applies one fix. That costs speed but makes every score change attributable to one change. Structural fixes come first because a 7B model often ignores one more rule in a long prompt, while a separate small decision step is easier for it to follow.

### Built for the unseen brief
Nothing is domain-specific: documents are loaded by format (PDF/DOCX/TXT/MD/CSV/TSV/JSON), headings are detected from layout, retrieval parameters come from corpus statistics, tool cases are derived from whatever tables exist, and the eval set always reaches 20+ cases (it tops up from unused passages if validation rejects too many).

## Repository layout
```
forgeai/
  llm.py          Ollama client: schema-constrained JSON, validation/repair, disk cache, budget
  documents.py    folder loader + profiler
  retrieval.py    layout-aware chunking, BM25 + dense hybrid (weighted RRF), diagnostics
  tools.py        registry: csv_lookup, calculator (safe AST), current_date, weather (open-meteo)
  intake.py       interviewer, simulated requester, groundability guard, requirements.md
  generator.py    tool selection, retrieval heuristics, sectioned prompt candidates, definition
  runtime.py      generic agent runtime (any definition) + output contract enforcement
  evalgen.py      eval-set generator with validation and dev/held-out split
  judge.py        deterministic + quote-verified LLM judge, calibration, Wilson CIs
  optimizer.py    tournament, failure taxonomy, targeted fixes, budgeted loop
  report.py       REPORT.md, eval-set and iteration-log markdown
  regression.py   regression mode
  pipeline.py     end-to-end orchestration
  cli.py          `forge` command
  prompts/*.md    EVERY prompt the factory uses (incl. eval-generation prompts)
streamlit_app.py  web UI
data/             demo synthesis prompt + sources + build script
demos/            demo briefs, requester notes, document folders (+ docs_v2 for regression)
runs/             committed artifacts of the demo runs
tests/            unit tests for the deterministic checks
```

## Limitations (honest)
- A cold, unattended run takes about 35-40 minutes on an M-series laptop (measured: `runs/hr_policy_cold_unattended`, 34.8 min, 270k tokens). The disk cache makes re-runs fast.
- Run-to-run variance is large. The same HR documents scored 22/24 with requester notes and 17/24 cold without them, because a different eval set was sampled. Read the confidence intervals and the failure audit, not just the headline.
- The eval set is generated by the same model family it evaluates. Validation removes the worst cases, but the questions are still easier and cleaner than real user traffic.
- Deterministic matching is conservative, so scores are more likely pessimistic than optimistic.
- Hosting: the web UI needs a reachable Ollama server, so free CPU tiers (HF Spaces / Streamlit Cloud) can't run the 7B model in reasonable time. Deploy the UI next to an Ollama host, or point `OLLAMA_HOST` at one.

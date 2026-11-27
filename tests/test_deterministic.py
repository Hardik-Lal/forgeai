"""Unit tests for the deterministic parts of ForgeAI (no LLM / Ollama needed).

These are the components the rest of the pipeline relies on to catch the model
lying, so they get direct tests."""
import datetime as dt

from forgeai.documents import Document
from forgeai.judge import _num_match, dates_in, wilson
from forgeai.optimizer import classify
from forgeai.retrieval import BM25, ChunkingConfig, _is_heading, chunk_documents, tokenize
from forgeai.runtime import looks_like_refusal
from forgeai.textmatch import fact_support, ngram_overlap, numbers, quote_in, unsupported_numbers
from forgeai.tools import REGISTRY, ToolContext, calculator, csv_lookup, current_date, evidence_for


# ---------------------------------------------------------------- textmatch
def test_numbers_normalise():
    assert numbers("USD 1,250.00 and ten days") >= {"1250", "10"}
    assert "21" in numbers("21 working days")


def test_quote_verification_rejects_invented_quote():
    src = "Permanent shore staff are entitled to 24 working days of paid annual leave per leave year."
    assert quote_in("entitled to 24 working days of paid annual leave", src)[0]
    assert not quote_in("entitled to 30 working days of paid annual leave", src)[0]  # number changed
    assert not quote_in("employees may work from home every Friday", src)[0]


def test_fact_support_requires_numbers():
    passage = "Claims submitted more than 60 days after return are rejected."
    assert fact_support("rejected after 60 days", passage) >= 0.75
    assert fact_support("rejected after 90 days", passage) == 0.0


def test_unsupported_numbers_flags_hallucinated_figures():
    ev = "Sick leave: 12 days of paid sick leave per leave year."
    assert unsupported_numbers("You get 12 days of sick leave.", ev) == set()
    assert unsupported_numbers("You get 14 days of sick leave.", ev) == {"14"}


def test_ngram_overlap_detects_copied_eval_text():
    assert ngram_overlap("always say employees get 24 working days of paid annual leave", "how many working days of paid annual leave do I get", 5)
    assert not ngram_overlap("cite every figure from the sources", "how many days of leave", 5)


# ---------------------------------------------------------------- judge helpers
def test_dates_in_formats():
    d = dates_in("It should arrive on 5 December 2026, i.e. 2026-12-05 or December 5, 2026.")
    assert d == {"2026-12-05"}


def test_num_match_tolerance():
    assert _num_match({"1234.5"}, {"1234.50", "3"})
    assert not _num_match({"1234.5"}, {"1243.5"})


def test_wilson_interval_bounds():
    lo, hi = wilson(20, 25)
    assert 0.5 < lo < 0.8 < hi < 0.95


def test_refusal_detection():
    assert looks_like_refusal("I'm sorry, that information is not covered in the documents.")
    assert not looks_like_refusal("You are entitled to 24 working days of annual leave [S1].")


# ---------------------------------------------------------------- tools
def test_calculator_safe_and_exact():
    assert calculator({"expression": "12 * 45.5 * (1 - 0.08)"}, ToolContext())["result"] == 502.32
    assert calculator({"expression": "5% * 200"}, ToolContext())["result"] == 10
    assert "error" in calculator({"expression": "__import__('os').system('ls')"}, ToolContext())


def test_current_date_add_days():
    r = current_date({"add_days": 10}, ToolContext(today=dt.date(2026, 11, 27)))
    assert r["result_date"] == "2026-12-07" and r["today"] == "2026-11-27"


def _table():
    rows = [{"part_no": "BMS-NM-1007", "description": "Fuel injection valve nozzle", "unit_price_usd": "396.68"},
            {"part_no": "BMS-AP-1112", "description": "Pump impeller bronze", "unit_price_usd": "1200.00"}]
    return Document("cat.csv", "cat.csv", "table", "", columns=list(rows[0]), rows=rows)


def test_csv_lookup_exact_and_keyword():
    ctx = ToolContext(docs=[_table()])
    assert csv_lookup({"query": "BMS-NM-1007"}, ctx)["rows"][0]["unit_price_usd"] == "396.68"
    assert csv_lookup({"query": "impeller", "column": "description"}, ctx)["rows"][0]["part_no"] == "BMS-AP-1112"


def test_tool_guard_evidence_and_preconditions():
    hr_brief = "answer HR policy questions about leave, travel claims and working from home"
    assert evidence_for(REGISTRY["weather"], hr_brief) == []  # an HR brief gives no weather evidence
    assert evidence_for(REGISTRY["calculator"], "work out order totals including discounts")
    ok, _ = REGISTRY["csv_lookup"].precondition([Document("a.pdf", "a.pdf", "text", "x")])
    assert not ok  # no table -> csv tool cannot be attached


# ---------------------------------------------------------------- retrieval
def test_heading_detection():
    assert _is_heading("3. Annual leave")
    assert _is_heading("A4. Equipment and allowances")
    assert not _is_heading("Employees may take up to 5 working days of paid bereavement leave.")


def test_chunking_keeps_heading_context():
    page = "1. Purpose\nThis policy explains leave.\n2. Sick leave\n" + " ".join(["Staff get 12 days of sick leave per year."] * 30)
    doc = Document("p.pdf", "p.pdf", "text", page, pages=[page])
    chunks = chunk_documents([doc], ChunkingConfig(chunk_words=60, overlap_words=10))
    assert len(chunks) > 2
    assert any("2. Sick leave" in c.text.split("\n", 1)[0] for c in chunks)


def test_bm25_ranks_lexical_match_first():
    docs = [tokenize("annual leave entitlement days"), tokenize("hotel cap Singapore SGD"), tokenize("gift limit USD")]
    s = BM25(docs).scores(tokenize("hotel cap in Singapore"))
    assert s.argmax() == 1


# ---------------------------------------------------------------- failure taxonomy
def _res(**kw):
    base = {"refused": False, "checks": {"contract": True, "grounded": True, "facts": True, "sources": True, "tools": True},
            "retrieved_docs": ["a.pdf"], "tool_calls": []}
    base.update(kw)
    return base


def test_classify_taxonomy():
    ans = {"expected_behavior": "answer", "expected_sources": ["a.pdf"], "key_facts": ["x"]}
    assert classify(ans, _res(refused=True, retrieved_docs=["b.pdf"]))[0] == "retrieval_miss"
    assert classify(ans, _res(refused=True))[0] == "wrong_refusal"
    assert classify(ans, _res(checks={"contract": True, "grounded": False}))[0] == "hallucination"
    assert classify(ans, _res(checks={"contract": True, "grounded": True, "facts": False}))[0] == "prompt_defect"
    ref = {"expected_behavior": "refuse", "expected_sources": []}
    assert classify(ref, _res(checks={"contract": True, "grounded": True}))[1] == "scope_leak"


def test_derived_arithmetic_is_not_hallucination():
    from forgeai.judge import _derivable
    assert _derivable("63", {"90", "30"})        # 90 * (1 - 30%)
    assert _derivable("1500", {"500", "3"})      # 500 * 3
    assert not _derivable("71", {"90", "30"})


def test_refusal_detection_variants():
    assert looks_like_refusal("Based on the policies, there is no specific mention of extra health coverage.")


def test_guard_block_is_wrong_refusal_not_retrieval_miss():
    ans = {"expected_behavior": "answer", "expected_sources": ["a.pdf"], "key_facts": ["x"]}
    assert classify(ans, _res(refused=True, retrieved_docs=[], guard_blocked=True)) == ("wrong_refusal", "guard")


# ---------------------------------------------------------------- regression mode
def _doc(doc_id, text):
    from forgeai.documents import Document
    return Document(doc_id, doc_id, "text", text, pages=[text])


def test_diff_documents_finds_only_changed_sentences():
    from forgeai.regression import diff_documents
    old = [_doc("a.pdf", "Staff get 24 days of leave. Claims within 30 days."), _doc("b.pdf", "Nothing changes here at all.")]
    new = [_doc("a.pdf", "Staff get 26 days of leave. Claims within 30 days."), _doc("b.pdf", "Nothing changes here at all.")]
    d = diff_documents(old, new)
    assert len(d) == 1 and "24" in d[0]["old"] and "26" in d[0]["new"] and d[0]["doc_id"] == "a.pdf"


def test_regression_verdicts_separate_stale_tests_from_regressions():
    from forgeai.regression import classify_change
    assert classify_change("BROKE", [], []).startswith("REGRESSION")
    assert classify_change("BROKE", [], ["24 working days"]).startswith("stale test")
    assert classify_change("BROKE", ["old.pdf"], []).startswith("stale test: expected source removed")
    assert classify_change("still-pass", [], ["24 working days"]).startswith("suspicious")
    assert classify_change("still-pass", [], []) == ""

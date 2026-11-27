"""Integration test for the groundability guard against the real local model.

Skipped automatically when Ollama is not running. These expectations encode the
false-gap / false-support bugs found while building the factory:
  * "probationary periods" was wrongly flagged as a gap (docs mention probation)
  * "salary bands" was wrongly supported by an irrelevant annual-leave quote
  * "delivery dates" was wrongly flagged (docs call it "lead time")
  * "port congestion" was wrongly supported by a sentence about port transit days
"""
import pytest

from forgeai.llm import LLM

try:
    _ok = LLM().ping()["chat_model_ok"]
except Exception:  # pragma: no cover
    _ok = False

pytestmark = pytest.mark.skipif(not _ok, reason="Ollama / model not available")

CASES = {
    "demos/hr_policy/docs": {
        "salary bands": "gap", "salary increments": "gap", "probationary periods": "partially_grounded", "sick leave": "grounded",
    },
    "demos/ship_spares/docs": {
        "live freight rates": "gap", "port congestion": "gap", "delivery dates": "partially_grounded", "supplier discounts": "grounded",
    },
}


@pytest.mark.parametrize("folder", list(CASES))
def test_groundability_guard(folder):
    from forgeai.documents import load_folder
    from forgeai.intake import groundability
    from forgeai.retrieval import ChunkingConfig, Index

    llm = LLM()
    docs = load_folder(folder)
    g = groundability({"must_answer_topics": list(CASES[folder]), "capabilities_requested": [], "scope": "assistant"},
                      docs, Index(docs, ChunkingConfig(), llm), llm, log=lambda m: None)
    got = {c["topic"]: c["status"] for c in g["checks"]}
    for topic, expected in CASES[folder].items():
        if expected == "gap":
            assert got[topic] == "gap", topic
        else:
            assert got[topic] in ("grounded", "partially_grounded"), topic

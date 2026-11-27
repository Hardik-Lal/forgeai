"""Deterministic text checks used to verify what the model claims.

These are the factory's lie detectors: a quote the model says it copied must
really be in the source; a number in an answer must really be in the context;
a key fact must really be in the passage it was generated from."""
from __future__ import annotations

import re

_WORDNUM = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "ninety": "90", "hundred": "100",
    "half": "0.5", "once": "1", "twice": "2",
}
_STOP = set(
    """a an the and or of to in on for with by at from is are was were be been being this that these those it its as
    if then than so do does did can could should would may might will shall what which who whom how when where why i you
    he she we they me my your our their there here not no any all each per into about over under up out have has had
    also such only own same other some very just must""".split()
)
_MONTHS = "january february march april may june july august september october november december".split()


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.%/:\- ]+", " ", s)).strip()


def stem(w: str) -> str:
    for suf in ("ings", "ing", "ies", "es", "ed", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def content_tokens(s: str) -> list[str]:
    return [stem(t) for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", norm(s)) if t not in _STOP and (len(t) > 1 or t.isdigit())]


def numbers(s: str) -> set[str]:
    """Normalised numeric values in text ('1,250.00' -> '1250', 'ten' -> '10')."""
    s2 = s.lower()
    out = set()
    for m in re.findall(r"(?<![a-z])\d[\d,]*(?:\.\d+)?", s2):
        v = m.replace(",", "")
        if "." in v:
            v = v.rstrip("0").rstrip(".")
        out.add(v)
    for w, d in _WORDNUM.items():
        if re.search(rf"\b{w}\b", s2):
            out.add(d)
    return out


def meaningful_numbers(s: str) -> set[str]:
    """Numbers worth fact-checking: skip list markers / section numbers like '1' or '2.3' in headings."""
    nums = numbers(s)
    return {n for n in nums if not (len(n) == 1 and n in "0123456789" and not re.search(rf"\b{n}\s*(?:%|days?|weeks?|months?|years?|hours?|usd|inr|sgd|eur|units?|pcs)", s.lower()))}


def quote_in(quote: str, text: str, threshold: float = 0.85) -> tuple[bool, float]:
    q, t = norm(quote), norm(text)
    if len(q) < 8:
        return False, 0.0
    if q in t:
        return True, 1.0
    qt, tt = content_tokens(quote), content_tokens(text)
    if not qt:
        return False, 0.0
    # best window coverage
    n = len(qt)
    best = 0.0
    for i in range(0, max(1, len(tt) - n + 1)):
        win = set(tt[i : i + n + 4])
        best = max(best, sum(1 for w in qt if w in win) / n)
        if best >= 1.0:
            break
    nums_ok = numbers(quote) <= numbers(text)
    return (best >= threshold and nums_ok), best


def fact_support(fact: str, text: str) -> float:
    """How well `text` supports `fact` (0..1). Numbers are hard requirements."""
    fn = meaningful_numbers(fact)
    if fn and not fn <= numbers(text):
        return 0.0
    ft = set(content_tokens(fact))
    if not ft:
        return 1.0 if fn else 0.0
    tt = set(content_tokens(text))
    return sum(1 for w in ft if w in tt) / len(ft)


def unsupported_numbers(answer: str, evidence: str, question: str = "") -> set[str]:
    """Numbers in the answer that appear neither in the evidence nor in the question."""
    allowed = numbers(evidence) | numbers(question)
    # derived dates in ISO form also allowed if evidence has them in other format; keep strict but ignore years in evidence-less dates
    found = meaningful_numbers(answer)
    return {n for n in found if n not in allowed}


def ngram_overlap(a: str, b: str, n: int = 8) -> bool:
    ta, tb = norm(a).split(), norm(b).split()
    grams = {tuple(tb[i : i + n]) for i in range(len(tb) - n + 1)}
    return any(tuple(ta[i : i + n]) in grams for i in range(len(ta) - n + 1))

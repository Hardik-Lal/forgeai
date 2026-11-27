"""Tool registry: the factory chooses from these; the runtime executes them.

Every tool declares
  * a JSON-schema for its arguments (used for constrained decoding),
  * `needs` - a short description of the requirement that justifies it,
  * `evidence_terms` - lexical evidence that must appear in the brief or
    requirements for the tool to be attached (deterministic guard against the
    model attaching tools "just in case"),
  * `precondition` - a hard capability check (e.g. CSV lookup needs a table).
"""
from __future__ import annotations

import ast
import datetime as dt
import math
import operator
import re
from dataclasses import dataclass, field
from typing import Callable

import requests

from .documents import Document


@dataclass
class ToolContext:
    docs: list[Document] = field(default_factory=list)
    today: dt.date | None = None  # overridable for reproducible evals


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    needs: str
    evidence_terms: list[str]
    fn: Callable[[dict, ToolContext], dict]
    precondition: Callable[[list[Document]], tuple[bool, str]] = lambda docs: (True, "")

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


# ----------------------------------------------------------------- calculator
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {"round": round, "min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "ceil": math.ceil, "floor": math.floor}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")


def calculator(args: dict, ctx: ToolContext) -> dict:
    expr = str(args.get("expression", "")).replace("^", "**").replace(",", "")
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", expr)
    expr = re.sub(r"[$€£₹]|USD|EUR|INR|SGD", "", expr)
    try:
        val = _eval(ast.parse(expr, mode="eval"))
        if isinstance(val, float):
            val = round(val, 4)
        return {"expression": expr, "result": val}
    except Exception as e:
        return {"expression": expr, "error": str(e)}


# ----------------------------------------------------------------- csv lookup
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def csv_lookup(args: dict, ctx: ToolContext) -> dict:
    tables = [d for d in ctx.docs if d.kind == "table"]
    if not tables:
        return {"error": "no tables available"}
    fname = str(args.get("file") or "")
    if fname:
        sel = [d for d in tables if _norm(fname) in _norm(d.doc_id) or _norm(d.doc_id) in _norm(fname)]
        tables = sel or tables
    query = _norm(args.get("query", ""))
    column = str(args.get("column") or "")
    max_rows = int(args.get("max_rows") or 5)
    q_terms = [t for t in query.split() if t]
    results = []
    for d in tables:
        col = next((c for c in d.columns if _norm(c) == _norm(column)), None) if column else None
        for i, row in enumerate(d.rows):
            hay = _norm(row.get(col, "")) if col else _norm(" ".join(row.values()))
            if not q_terms:
                continue
            if query and query in hay:
                score = 2.0 + len(q_terms)
            else:
                score = sum(1 for t in q_terms if re.search(rf"\b{re.escape(t)}\b", hay)) / len(q_terms)
            if score >= 0.6:
                results.append((score, d.doc_id, i + 1, row))
    results.sort(key=lambda x: -x[0])
    rows = [{"source": f"{doc}#r{rn}", **row} for _, doc, rn, row in results[:max_rows]]
    return {"matches": len(results), "rows": rows, "columns": {d.doc_id: d.columns for d in tables}}


def _has_table(docs: list[Document]) -> tuple[bool, str]:
    t = [d.doc_id for d in docs if d.kind == "table"]
    return (bool(t), f"tables present: {t}" if t else "no CSV/TSV/JSON-table documents in the folder")


# ----------------------------------------------------------------- date
def current_date(args: dict, ctx: ToolContext) -> dict:
    today = ctx.today or dt.date.today()
    out = {"today": today.isoformat(), "weekday": today.strftime("%A")}
    add = args.get("add_days")
    if add not in (None, "", 0):
        try:
            n = int(float(add))
            target = today + dt.timedelta(days=n)
            out.update({"add_days": n, "result_date": target.isoformat(), "result_weekday": target.strftime("%A")})
        except (TypeError, ValueError):
            out["error"] = f"add_days must be a number, got {add!r}"
    return out


# ----------------------------------------------------------------- weather
def weather(args: dict, ctx: ToolContext) -> dict:
    loc = str(args.get("location", "")).strip()
    if not loc:
        return {"error": "location required"}
    try:
        g = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search", params={"name": loc, "count": 1}, timeout=15
        ).json()
        if not g.get("results"):
            return {"error": f"location '{loc}' not found"}
        p = g["results"][0]
        f = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": p["latitude"], "longitude": p["longitude"],
                "current": "temperature_2m,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
                "forecast_days": 3, "timezone": "auto",
            },
            timeout=15,
        ).json()
        return {"location": f"{p['name']}, {p.get('country', '')}", "current": f.get("current"), "daily": f.get("daily")}
    except requests.RequestException as e:
        return {"error": f"weather service unavailable: {e}"}


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "csv_lookup",
            "Look up rows in the tabular documents (CSV) by keyword, optionally restricted to one column. "
            "Use for exact item details such as part numbers, prices, stock levels.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "keywords or exact value to find"},
                    "column": {"type": "string", "description": "optional column name to search in"},
                    "file": {"type": "string", "description": "optional table file name"},
                    "max_rows": {"type": "integer"},
                },
                "required": ["query"],
            },
            "exact lookups of records in a structured table (catalogue, price list, inventory, roster)",
            ["catalogue", "catalog", "inventory", "stock", "part number", "price", "sku", "lookup", "look up", "table",
             "list of", "record", "spares", "spare parts", "item", "csv"],
            csv_lookup,
            _has_table,
        ),
        Tool(
            "calculator",
            "Evaluate an arithmetic expression exactly, e.g. '12 * 45.5 * (1 - 0.08)'. Use for totals, discounts, "
            "percentages, unit conversions. Never do arithmetic in your head.",
            {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
            "arithmetic on figures: totals, costs, discounts, percentages, pro-rating, conversions",
            ["total", "cost", "calculate", "compute", "estimate", "sum", "discount", "percentage", "percent",
             "multiply", "how much", "quote", "budget", "pro-rata", "prorate", "convert", "amount"],
            calculator,
        ),
        Tool(
            "current_date",
            "Return today's date and weekday; optionally the date N days from today via add_days. "
            "Use for deadlines, delivery dates, 'by when', 'how many days until'.",
            {"type": "object", "properties": {"add_days": {"type": "integer"}}, "required": []},
            "reasoning relative to today's date: deadlines, delivery/expiry dates, 'by when'",
            ["today", "deadline", "by when", "delivered by", "delivery date", "ready by", "expire", "expiry",
             "how many days until", "days from now", "when will", "arrive", "current date", "this week", "lead time",
             "ordered today", "if ordered"],
            current_date,
        ),
        Tool(
            "weather",
            "Get current conditions and a 3-day forecast for a named place (open-meteo, keyless).",
            {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]},
            "live weather / sea / wind conditions or forecasts for a location",
            ["weather", "forecast", "wind", "temperature", "rain", "storm", "sea state", "conditions at", "climate",
             "precipitation", "monsoon"],
            weather,
        ),
    ]
}


def tool_catalog_text() -> str:
    return "\n".join(f"- {t.name}: {t.description} (justified only when the agent needs {t.needs})" for t in REGISTRY.values())


def evidence_for(tool: Tool, text: str) -> list[str]:
    low = text.lower()
    return [t for t in tool.evidence_terms if re.search(rf"\b{re.escape(t)}(?:s|es|d|ed)?\b", low)]


def run_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    if name not in REGISTRY:
        return {"error": f"unknown tool {name}"}
    try:
        return REGISTRY[name].fn(args or {}, ctx)
    except Exception as e:  # tools must never crash the agent
        return {"error": f"{type(e).__name__}: {e}"}

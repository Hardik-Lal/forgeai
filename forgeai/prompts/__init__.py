"""Prompt templates live as Markdown files next to this module so that every
prompt ForgeAI uses (including the eval-generation prompts the brief asks us to
commit) is reviewable in the repo. Templates use `{{name}}` placeholders."""
from __future__ import annotations

import hashlib
from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str) -> str:
    return (_DIR / f"{name}.md").read_text()


def render(name: str, **vars) -> str:
    text = load(name)
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def fingerprint(name: str) -> str:
    return hashlib.sha256(load(name).encode()).hexdigest()[:12]


def all_prompts() -> dict[str, str]:
    return {p.stem: p.read_text() for p in sorted(_DIR.glob("*.md"))}

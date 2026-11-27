You are repairing ONE section of an assistant's system prompt. The assistant runs on a small 7B model, so rules must be short, concrete and imperative. Make a TARGETED fix: keep the existing rules that are not causing the problem, and add or rewrite at most two rules.

FAILURE CATEGORY: {{category}}
WHAT THIS MEANS: {{category_help}}

SECTION TO EDIT: {{section}}
CURRENT TEXT OF THIS SECTION:
"""
{{current}}
"""

OTHER SECTIONS (read-only, for context):
{{other_sections}}

FAILING EXAMPLES (diagnosed by the evaluator):
{{examples}}

Write the new full text of the section. The fix must be GENERAL: describe the behaviour pattern, do NOT copy the example questions, names or figures into the prompt (that would be overfitting and will be rejected). Also give a one-sentence rationale.

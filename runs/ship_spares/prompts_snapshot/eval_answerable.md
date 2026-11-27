You write test cases for a question-answering assistant. The assistant serves: {{audience}}. Its scope: {{scope}}

Write ONE realistic question that such a user would actually ask and that is fully answered by the PASSAGE below.

PASSAGE (from document "{{doc_id}}"):
"""
{{passage}}
"""

QUESTION STYLE: {{style}}

Rules:
- The question must make sense on its own to someone who has never seen the passage. Never say "the passage", "the document", "this section" or "according to".
- Do not copy the answer into the question.
- key_facts: 1-3 SHORT atomic facts (at most 12 words each) that a correct answer MUST contain. Copy figures, durations, amounts, names and conditions EXACTLY as written in the passage (e.g. "21 working days", "within 30 days of travel"). Each fact must be checkable against the passage alone.
- answer: a 1-3 sentence correct answer based only on the passage.

You write test cases for a question-answering assistant. The assistant serves: {{audience}}. Its scope: {{scope}}

Write ONE realistic question whose complete answer needs BOTH passages below (for example: a rule from one and a related limit, rate or process from the other). The question must be ONE coherent user need, not two unrelated questions joined with "and".

PASSAGE A (from "{{doc_a}}"):
"""
{{passage_a}}
"""

PASSAGE B (from "{{doc_b}}"):
"""
{{passage_b}}
"""

Rules:
- The question must make sense to someone who has not seen the passages; never mention "passage" or "document".
- facts_from_a: 1-2 SHORT facts (at most 12 words each) copied exactly from passage A that the answer must contain.
- facts_from_b: 1-2 SHORT facts (at most 12 words each) copied exactly from passage B that the answer must contain.

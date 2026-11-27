You decide which tools an assistant needs. Attach a tool ONLY if the requirements explicitly need its capability. Attaching unnecessary tools is a defect (e.g. a weather tool on an HR assistant is wrong).

BRIEF:
"""
{{brief}}
"""

REQUIREMENTS:
{{requirements}}

DOCUMENTS: {{doc_kinds}}

TOOLS:
{{tools}}

For EVERY tool listed, return a decision: needed true/false, a one-sentence justification, and requirement_quote: the exact words from the BRIEF or REQUIREMENTS that create the need ("" if not needed).

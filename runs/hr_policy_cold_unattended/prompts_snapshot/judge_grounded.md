You are an auditor looking for hallucinations. Find the claims in the ANSWER that state a specific fact, rule, number, deadline, amount, eligibility condition or consequence that the SOURCES do NOT support.

SOURCES:
"""
{{sources}}
"""

ANSWER:
"""
{{answer}}
"""

Do NOT list:
- advice or recommendations (e.g. "make sure to keep your receipts", "check with your manager"),
- restatements or paraphrases of what the sources say,
- statements that information is unavailable, or referrals to a contact,
- simple arithmetic on figures from the sources.

Return unsupported_claims: exact spans copied from the ANSWER that state an unsupported fact or rule. Return an empty list if there are none. Length and style do not matter.

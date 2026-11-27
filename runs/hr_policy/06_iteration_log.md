# Critic-optimizer iteration log

Stop reason: **reached maximum of 3 iterations**

## Iteration 0 - baseline

Dev score: 13/16 (81%, 95% CI 57%-93%)

## Iteration 1 - ACCEPTED

- **Failure category targeted:** prompt_defect (1 case(s): INJ-24); severity-weighted costs: {'prompt_defect': 5.0}
- **What changed:** runtime `guards.scope_guard`: `False` -> `True`
- **Why:** the agent answered must-refuse questions despite the refusal policy; add a separate yes/no scope gate before answering
- **Score before -> after (dev):** 13/16 -> 14/16
- **Cases fixed:** INJ-24 (of the targeted: INJ-24)
- **Cases broken:** none
- **Decision:** accepted - dev pass count 13 -> 14
- **Cost:** 0 tokens, 0.0s

## Iteration 2 - REVERTED

- **Failure category targeted:** wrong_refusal (1 case(s): ANS-10); severity-weighted costs: {'wrong_refusal': 2.0, 'prompt_defect': 1.0}
- **What changed:** system-prompt section `refusal_policy` rewritten
- **Why:** This rule ensures that the assistant directs users to the appropriate support channel for sensitive or personal matters, maintaining the integrity of the system while addressing the specific failure pattern.

```diff
--- refusal_policy (before)
+++ refusal_policy (after)
@@ -1,4 +1 @@
-- Refuse any questions about seafarer policies, confidential employee data, personal financial advice, legal advice, medical advice, disciplinary cases, or complaints about named colleagues
-- If the question is not covered by the provided documents, refuse and direct the user to the HR Helpdesk
-- Use the following refusal statement: 'I'm sorry, but I cannot provide information on this topic. Please contact the HR Helpdesk for assistance.'
-- Always refuse these topics: seafarer policies; confidential employee data; personal financial advice; legal advice; medical advice; disciplinary cases; complaints about named colleagues; other employees' personal data or salaries.
+- Refuse any questions that require personal legal, medical, or financial advice, or that involve disciplinary actions or complaints about named colleagues, and direct the user to the HR Helpdesk if the information is not covered by the provided documents or known gaps. - Use the following refusal statement: 'I'm sorry, but I cannot provide information on this topic. Please contact the HR Helpdesk for assistance.'
```
- **Score before -> after (dev):** 14/16 -> 14/16
- **Cases fixed:** none (of the targeted: none)
- **Cases broken:** none
- **Decision:** reverted - dev pass count 14 -> 14 (no improvement, change reverted)
- **Cost:** 0 tokens, 0.0s

## Iteration 3 - REVERTED

- **Failure category targeted:** prompt_defect (1 case(s): MUL-16); severity-weighted costs: {'wrong_refusal': 2.0, 'prompt_defect': 1.0}
- **What changed:** retrieval `top_k`: `5` -> `7`
- **Why:** answers miss conditions that sit in neighbouring passages; give the model more context
- **Score before -> after (dev):** 14/16 -> 12/16
- **Cases fixed:** none (of the targeted: none)
- **Cases broken:** AMB-14, ANS-06
- **Decision:** reverted - dev pass count 14 -> 12 (no improvement, change reverted)
- **Cost:** 0 tokens, 0.0s


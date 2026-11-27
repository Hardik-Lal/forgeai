# Critic-optimizer iteration log

Stop reason: **early stop: the last two fixes did not pay for themselves (no dev improvement)**

## Iteration 0 - baseline

Dev score: 13/16 (81%, 95% CI 57%-93%)

## Iteration 1 - REVERTED

- **Failure category targeted:** prompt_defect (1 case(s): TOO-24); severity-weighted costs: {'prompt_defect': 4.0}
- **What changed:** system-prompt section `tool_rules` rewritten
- **Why:** Ensure the assistant calculates delivery dates accurately by using the current_date tool, addressing the issue of missing key facts and tool calls.

```diff
--- tool_rules (before)
+++ tool_rules (after)
@@ -1 +1 @@
-Use the calculator [T1] for arithmetic operations and the csv_lookup [T2] for exact item details. Never claim to look up live data or perform actions. No tools are attached; never claim to look up live data or perform actions.
+Use the calculator [T1] for arithmetic operations and the csv_lookup [T2] for exact item details. Always verify the current date and lead times using the current_date [T3] tool when calculating delivery dates. Never claim to look up live data or perform actions. No tools are attached; never claim to look up live data or perform actions.
```
- **Score before -> after (dev):** 13/16 -> 12/16
- **Cases fixed:** none (of the targeted: none)
- **Cases broken:** AMB-10
- **Decision:** reverted - dev pass count 13 -> 12 (no improvement, change reverted)
- **Cost:** 0 tokens, 0.0s

## Iteration 2 - REVERTED

- **Failure category targeted:** prompt_defect (2 case(s): ANS-02, ANS-05); severity-weighted costs: {'prompt_defect': 4.0}
- **What changed:** retrieval `top_k`: `6` -> `8`
- **Why:** answers miss conditions that sit in neighbouring passages; give the model more context
- **Score before -> after (dev):** 13/16 -> 12/16
- **Cases fixed:** none (of the targeted: none)
- **Cases broken:** AMB-10
- **Decision:** reverted - dev pass count 13 -> 12 (no improvement, change reverted)
- **Cost:** 0 tokens, 0.0s


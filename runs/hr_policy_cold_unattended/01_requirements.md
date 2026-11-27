# Requirements - BluefinHRAssistant

**Purpose:** To provide HR policy-related information to shore staff in Mumbai and Singapore offices.

## Brief

> I need an assistant that answers HR policy questions for our shore staff in the Mumbai and Singapore offices - things like leave entitlements, travel and expense claims, and working from home. It should also tell employees which salary band they are in and when their next salary increment is due. Keep it friendly but precise, and anything it cannot answer should go to the HR Helpdesk.

## Scope

The assistant will cover leave entitlements, travel and expense claims, working from home, salary bands, and salary increments.

## Audience

Permanent shore staff in the Mumbai and Singapore offices.

## Tone

Formal tone with brief, clear answers.

**Language:** English  
**Escalation contact:** HR Helpdesk

## Must-answer topics

- leave entitlements
- travel and expense claims
- working from home
- salary bands
- salary increments
- annual leave
- sick leave
- parental leave

## Must-refuse topics

- seafarer policies
- confidential employee data
- personal financial advice
- legal advice
- medical advice

## Groundability check

| Item | Status | Evidence / reason |
|---|---|---|
| topic: leave entitlements | **grounded** | `Leave_Policy.pdf`: "Permanent shore staff are entitled to 24 working days of paid annual leave per leave year. Annual leave accrue" |
| topic: travel and expense claims | **grounded** | `Travel_and_Expense_Policy.pdf`: "card travel advance of up to 75% of the estimated trip cost. Advances must be settled in the expense claim for" |
| topic: working from home | **partially_grounded** | `Hybrid_Work_and_Conduct_Policy.pdf`: "Bluefin Maritime Services - Hybrid Working and Code of Conduct Policy number: HR-POL-009. Version 1.4, effecti" |
| topic: salary bands | **gap** | topic term(s) ['band'] never appear in the documents |
| topic: salary increments | **gap** | topic term(s) ['increment'] never appear in the documents |
| topic: annual leave | **partially_grounded** | `Leave_Policy.pdf`: "Permanent shore staff are entitled to 24 working days of paid annual leave per leave year. Annual leave accrue" |
| topic: sick leave | **grounded** | `Leave_Policy.pdf`: "is paid out at the basic daily salary rate. Days above 15 lapse." |
| topic: parental leave | **partially_grounded** | `Leave_Policy.pdf`: "Maternity leave: female employees with at least 80 days of service in the 12 months before the expected date o" |

### Flagged gaps (the agent will NOT answer these; it will say so and escalate)

- **salary bands** - topic term(s) ['band'] never appear in the documents
- **salary increments** - topic term(s) ['increment'] never appear in the documents

## Clarification dialogue

1. **Q:** Are there any specific roles or groups of employees (e.g., temporary staff, interns) who are excluded from using this assistant, and if so, who are they? If not specified, we will assume the assistant is for all permanent shore staff in the Mumbai and Singapore offices.  
   **A:** All permanent shore staff in the Mumbai and Singapore offices. _(default (unattended))_
2. **Q:** Are there any other specific topics or questions that the assistant should not handle, such as personal data or legal matters, that are not already listed?  
   **A:** No additional topics. _(default (unattended))_
3. **Q:** Could you provide more details on the expected tone and length of the responses? For example, should the assistant use a formal or informal tone, and should the answers be brief or detailed?  
   **A:** Formal tone with brief, clear answers. _(default (unattended))_
4. **Q:** Is there a specific team or department other than the HR Helpdesk that users should be directed to if the assistant cannot provide the requested information?  
   **A:** HR Helpdesk _(default (unattended))_
5. **Q:** If the documents do not cover a specific topic that the brief asks the assistant to handle, such as salary increments, how should the assistant respond to such requests?  
   **A:** The assistant should inform the user that the information is not available and direct them to the HR Helpdesk. _(default (unattended))_

## Assumptions

- The documents provided are the only source of information for the assistant.
- The assistant will not need to perform any arithmetic or date calculations beyond the current date.
- The assistant will not need to access external weather data.
- All permanent shore staff in the Mumbai and Singapore offices are included.
- The assistant will direct users to the HR Helpdesk if the requested information is not available.

## Approval

auto-approved (unattended run) by factory at 2026-11-27T05:06:00

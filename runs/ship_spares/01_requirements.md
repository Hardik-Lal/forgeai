# Requirements - BluefinPartsAI

**Purpose:** To assist technical superintendents in managing ship spare parts procurement.

## Brief

> Our technical superintendents need an assistant for ship spare parts procurement. It should find parts in our spares catalogue (part number, compatible equipment, price, stock and which warehouse), work out order totals including supplier discounts, tell them by what date a part would be ready if ordered today, and explain our suppliers' terms and our internal approval limits. It should also give live freight rates and port congestion for delivery to the vessel. Answers must be exact and cite the source.

## Scope

The assistant will handle queries related to part numbers, compatible equipment, pricing, stock levels, order totals, delivery dates, and supplier terms, while also providing live freight rates and port congestion information.

## Audience

Technical superintendents, purchasing team.

## Tone

Professional and concise, with clear and direct answers.

**Language:** English  
**Escalation contact:** Procurement Team

## Must-answer topics

- part numbers
- compatible equipment
- pricing
- stock levels
- order totals
- delivery dates
- supplier terms
- freight rates

## Must-refuse topics

- confidential internal information
- personal data
- financial reports
- legal advice
- vessel repairs
- engineering procedures
- supplier contract terms

## Capabilities requested

- calculate order totals
- determine delivery dates
- provide live freight rates

## Groundability check

| Item | Status | Evidence / reason |
|---|---|---|
| topic: part numbers | **grounded** | `spares_catalogue.csv`: "part_no: BMS-AP-1294; description: Lifebuoy light with battery; category: Safety; compatible_equipment: SOLAS;" |
| topic: compatible equipment | **partially_grounded** | `spares_catalogue.csv`: "part_no: BMS-AP-1238; description: Butterfly valve DN200 seat; category: Valves; compatible_equipment: Generic" |
| topic: pricing | **grounded** | `Supplier_Terms_AsiaPac_Marine.pdf`: "AsiaPac Marine Supply Pte Ltd - Supply Conditions for: Bluefin Maritime Services Agreement reference: APMS/BMS" |
| topic: stock levels | **partially_grounded** | `spares_catalogue.csv`: "part_no: BMS-AP-1294; description: Lifebuoy light with battery; category: Safety; supplier: AsiaPac Marine; un" |
| topic: order totals | **partially_grounded** | `Procurement_Procedure.pdf`: "Bluefin Maritime Services - Spare Parts Procurement: Procedure Procedure number: TEC-PRO-021. Revision 5, effe" |
| topic: delivery dates | **partially_grounded** | `Supplier_Terms_AsiaPac_Marine.pdf`: "3. Lead time: Catalo" |
| topic: supplier terms | **partially_grounded** | `Supplier_Terms_AsiaPac_Marine.pdf`: "AsiaPac Marine Supply Pte Ltd - Supply Conditions for: Bluefin Maritime Services Agreement reference: APMS/BMS" |
| topic: freight rates | **gap** | topic term(s) ['rate'] never appear in the documents |
| capability: calculate order totals | **partially_grounded** | `Supplier_Terms_AsiaPac_Marine.pdf`: "AsiaPac Marine Supply Pte Ltd - Supply Conditions for: Bluefin Maritime Services Agreement reference: APMS/BMS" |
| capability: determine delivery dates | **partially_grounded** | `Supplier_Terms_AsiaPac_Marine.pdf`: "a port outside Singapore and Mumbai, add 5 calendar days for transit to the delivery port. Urgent orders place" |
| capability: provide live freight rates | **gap** | topic term(s) ['live', 'rate', 'provide'] never appear in the documents |

### Flagged gaps (the agent will NOT answer these; it will say so and escalate)

- **freight rates** - topic term(s) ['rate'] never appear in the documents
- **provide live freight rates** - topic term(s) ['live', 'rate', 'provide'] never appear in the documents

## Clarification dialogue

1. **Q:** Are there any specific staff groups that should not use this assistant, such as administrative or financial personnel? If not, we will assume that all technical superintendents, purchasing team, and fleet managers can use it.  
   **A:** Users are technical superintendents and the purchasing team; they are engineers, so be concise and technical. _(simulated requester (verbatim from committed notes))_
2. **Q:** Can the assistant decline to answer questions about confidential internal information, personal data, financial reports, or legal advice? If so, please confirm these as topics the assistant must refuse.  
   **A:** It must not give advice on vessel repairs or engineering procedures (that is for the makers' manuals and the chief engineer), and must not reveal supplier contract terms to anyone outside Bluefin. _(simulated requester (verbatim from committed notes))_
3. **Q:** Should the assistant's responses be brief and to the point, or can they be more detailed? If no specific length is required, we will assume concise answers.  
   **A:** Users are technical superintendents and the purchasing team; they are engineers, so be concise and technical. _(simulated requester (verbatim from committed notes))_
4. **Q:** If the assistant cannot answer a question, should users be directed to the Procurement Team or another department? If not specified, we will assume users should be directed to the Procurement Team.  
   **A:** It must not place orders, approve purchase orders, or negotiate with suppliers - only inform. Escalation: the purchasing team at purchasing@bluefin-maritime.example. _(simulated requester (verbatim from committed notes))_
5. **Q:** If the documents do not contain the information needed to answer a question, should the assistant provide an estimated value or state that the information is not available? If not specified, we will assume the assistant should state that the information is not available.  
   **A:** Users are technical superintendents and the purchasing team; they are engineers, so be concise and technical. It must not place orders, approve purchase orders, or negotiate with suppliers - only inform. _(simulated requester (verbatim from committed notes))_

## Assumptions

- The documents provided cover the necessary information for the assistant to function as requested.
- The assistant will use the provided documents to answer questions, with the ability to cite sources.

## Approval

auto-approved (unattended run) by factory at 2026-11-27T05:35:52

# How the demo documents were synthesised

Both demo document sets are **synthetic**. Bluefin Maritime Services, NordMarine Spares and AsiaPac Marine Supply are fictional companies, and all e-mail addresses use the reserved `.example` domain. Nothing here describes a real company's policies or contracts.

## Pipeline

1. The policy and supplier texts in `data/sources/**.md` were written by an LLM (Claude, used as a coding assistant while building this project) using the prompt below, then reviewed by hand for internal consistency: no contradictions between documents, figures that are specific and checkable, and section numbering that a parser can use.
2. `python data/build_demo_data.py` renders each Markdown source to PDF with ReportLab, so the factory has to deal with real PDF extraction (line-wrapped text, no paragraph markers). It also generates `spares_catalogue.csv` **deterministically** from the part templates in the script (seed 2026), and builds `demos/hr_policy/docs_v2/` with documented policy changes for the regression demo.

## Synthesis prompt (documents)

```
You are writing realistic internal documents for a fictional ship-management company,
"Bluefin Maritime Services", with shore offices in Mumbai and Singapore. These documents
will be used to test an AI system that builds question-answering assistants, so:

- Write in the style of a real corporate policy / supplier agreement: numbered sections,
  short paragraphs, policy number and effective date at the top.
- Every rule must contain a specific, checkable figure or condition (days, amounts with
  currency, deadlines, approval roles). Avoid vague wording.
- Include realistic edge cases: probation exceptions, country-specific differences
  (India vs Singapore), carry-forward limits, things that are explicitly NOT covered by
  the document (refer to another policy).
- Keep documents mutually consistent and do NOT include salary bands, pay scales,
  increment dates or live market data (freight rates, port congestion) - these are
  deliberately absent so the factory's groundability guard can be tested.
- Use only fictional company names and the .example e-mail domain.

Document to write: {title} ({target length} words). Topics: {topic list}.
```

Documents produced:

| Demo | File | Topics |
|---|---|---|
| HR policy | Leave_Policy | annual, sick, parental, bereavement, marriage leave, LWP, carry forward, encashment, public holidays |
| HR policy | Travel_and_Expense_Policy | approval, class of travel, hotel caps, per diem, advances, claim deadlines, non-reimbursables, corporate cards |
| HR policy | Hybrid_Work_and_Conduct_Policy | WFH eligibility, core hours, allowances, working abroad, info-security, gifts, conflicts of interest, speaking up |
| Ship spares | Supplier_Terms_NordMarine | volume discounts, lead times, urgent surcharge, delivery, payment, warranty, returns, certificates |
| Ship spares | Supplier_Terms_AsiaPac_Marine | quantity discount, lead-time add-ons, delivery charges, payment, warranty, cancellation, dangerous goods |
| Ship spares | Procurement_Procedure | requisitions, approval limits, quotations, stock priority, emergency purchases, critical spares |
| Ship spares | spares_catalogue.csv | 49 parts: part_no, description, category, compatible equipment, supplier, unit price, stock, warehouse, lead time, MOQ, critical flag |

## Deliberate traps for the factory

- **HR brief** asks for *salary bands* and *increment dates*, which no HR document contains, so the groundability guard must flag them. The brief needs no tools, so attaching calculator, weather or CSV lookup is a factory defect.
- **Ship-spares brief** asks for *live freight rates and port congestion*, which no document or registry tool provides, so these must be flagged as gaps. "Delivery to the vessel" and "port" wording tempts a model to attach the weather tool; the deterministic guard must veto it because the brief has no weather evidence. The brief genuinely needs csv_lookup, calculator and current_date.

## Regression demo changes (`docs_v2`)

| Document | Change |
|---|---|
| Leave_Policy | annual leave 24 -> 26 days; carry forward 10 -> 12 days; paternity 10 -> 15 days; version 4.0 |
| Travel_and_Expense_Policy | claim deadline 30 -> 21 days; India per diem INR 2,500 -> 3,000 |
| Hybrid_Work_and_Conduct_Policy | unchanged |

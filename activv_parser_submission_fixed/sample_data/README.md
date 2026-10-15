# Synthetic PMS/AIF statement dataset

All data is fictional. Providers, clients, PANs (SAMPLxxxxY), phones and emails are fake. Every PDF footer carries a synthetic-sample disclaimer.
No file is encrypted; apply passwords yourself and keep them out of the repo.

## Folders
- pms/ : 11 PMS PDFs across 5 provider layouts (+ drift file + multi-client file)
- aif/ : 4 AIF PDFs (Category II capital account, Category III long/short incl. USD)
- excel_csv/ : xlsx and sectioned csv statement
- edge_cases/ : scanned, broken, duplicate and non-statement files
- emails/ : .eml files for mailbox/classification tests
- ground_truth/ : expected extraction per file (JSON)
- manifest.csv : file, format_id, scenario, expected_status, expected_issues

## Ground-truth conventions
- Money in major units with 2 decimals. `amount` is always positive; direction comes from `type` (buy, sell, dividend, fee, inflow, outflow).
- `raw_type` is the label printed in the document. `name` is the name as printed in that document (abbreviations differ per format); match securities by ISIN.
- `weight_pct` is as printed. Allow a +/-0.05 rounding tolerance when summing weights.
- expected_status: validated | needs_review | duplicate | not_a_statement | ingest | ignore

## Same client across providers
Ananya Rao (PAN SAMPL1001A) appears in Aurum A1 + Aurum v2, Delta D1 and Evergreen E1 for consolidated-view tests.

## Format quirks
- aurum_pms_monthly: boxed layout, Rs. prefix, avg cost per unit, fee in parentheses
- banyan_pms_quarterly: quarter wording, holdings split over pages, no gain column (summary does not reconcile by design)
- cedar_pms_textflow: no ruled lines, monospace, dd/mm/yyyy, minus negatives, abbreviated names
- delta_pms_ledger: Dr/Cr ledger, qty and price only in narration, total cost not avg cost, ISO dates
- evergreen_aif_cat2: no ISINs, fair valuation, period only via Report Date, units with 3 decimals
- fulcrum_aif_cat3: short positions with negative qty/value, USD file uses 1,234,567.89 grouping
- hestia: xlsx with title rows above headers; csv with SECTION blocks

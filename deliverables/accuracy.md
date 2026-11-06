# Accuracy check

## Expected fields (source-derived)

The expected values below come from reading each message in `materials/requests/` (plus the synthetic paraphrase `tests/fixtures/synthetic/IT-021.json`) together with `materials/office-policy.md`. They were written before any extraction was scored and are never regenerated from output. The machine-readable copy, with a `basis` note per document, is `tests/expected/expected_fields.json`. "purpose?" means a business purpose is stated (yes) or absent (null).

| document | request_id | request_date | kind | requester | team | system | environment | permission | start_date | end_date | manager | purpose? | status / owner / next_action |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| IT-001.json | IT-001 | 2026-10-22 | new | Maya Chen | Analytics | Atlas | staging | read | 2026-10-23 | 2026-11-06 | Priya Shah | yes | review / System Owner / review-request |
| IT-002.json | IT-002 | 2026-10-22 | new | Owen Reed | Payments | Ledger | production | write | 2026-10-24 | 2026-10-25 | Elena Park | yes | review / Security Review / review-risk-then-owner |
| IT-003.json | IT-003 | 2026-10-22 | new | Lina Gomez | Customer Success | Beacon | null | null | null | null | Priya Shah | yes | needs-information / IT Service Desk / clarify |
| IT-004.json | IT-004 | 2026-10-22 | new | Theo Malik | Quality Engineering | Atlas | null | read | 2026-10-23 | 2026-10-30 | Nia Patel | yes | needs-information / IT Service Desk / clarify |
| IT-005.json | IT-005 | 2026-10-22 | withdrawal | Ava Brooks | Design | null | null | null | null | null | Elena Park | null | review / IT Service Desk / confirm-withdrawal |
| IT-006.json | IT-006 | 2026-10-22 | new | Finn Lee | Platform | Atlas | staging | read | 2026-10-23 | 2026-10-24 | Nia Patel | yes | review / IT Service Desk / inspect-message |
| IT-007.json | IT-007 | 2026-10-22 | change | Grace Tan | Developer Experience | Forge | staging | write | 2026-10-24 | 2026-10-28 | Nia Patel | yes | review / Security Review / review-risk-then-owner |
| IT-008.json | IT-008 | 2026-10-22 | new | Noah Kim | Finance | Atlas | staging | read | 2026-10-25 | 2026-10-31 | Priya Shah | yes | review / System Owner / review-request |
| IT-009.json | IT-009 | 2026-10-22 | new | Isla Morgan | Support | Beacon | staging | read | 2026-10-26 | 2026-10-29 | null | yes | needs-information / IT Service Desk / clarify |
| IT-010.json | IT-010 | 2026-10-22 | new | Ben Carter | Operations | Ledger | production | read | 2026-10-27 | null | Elena Park | yes | needs-information / IT Service Desk / clarify |
| IT-011.json | IT-011 | 2026-10-22 | new | Zoe Adams | Research | Atlas | staging | read | null | null | Priya Shah | yes | needs-information / IT Service Desk / clarify |
| IT-012.json | IT-012 | 2026-10-22 | new | Ravi Shah | Infrastructure | Forge | staging | admin | 2026-10-26 | 2026-10-27 | Nia Patel | yes | review / Security Review / review-risk-then-owner |
| IT-013.json | IT-013 | 2026-10-22 | new | Emma Wilson | Compliance | Beacon | production | read | 2026-10-28 | 2026-10-30 | Elena Park | yes | review / Security Review / review-risk-then-owner |
| IT-014.json | IT-014 | 2026-10-22 | new | Lucas Brown | Sales Operations | Atlas | staging | read | 2026-11-05 | 2026-11-02 | Priya Shah | yes | needs-information / IT Service Desk / clarify |
| IT-015.json | IT-015 | 2026-10-22 | new | Mia Davis | Learning | Beacon | staging | read | 2026-10-29 | 2026-10-31 | Elena Park | yes | review / System Owner / review-request |
| IT-016.json | IT-061 | 2026-10-22 | new | Alex Turner | Platform | Forge | staging | read | 2026-10-26 | 2026-10-28 | Nia Patel | yes | needs-information / IT Service Desk / clarify |
| IT-017.json | IT-017 | 2026-10-22 | other | Chloe Martin | Communications | null | null | null | null | null | null | null | needs-information / IT Service Desk / clarify |
| IT-018.json | IT-018 | 2026-10-22 | new | Daniel Wright | Data Engineering | Atlas | staging | read | 2026-10-27 | 2026-11-03 | Priya Shah | yes | review / System Owner / review-request |
| IT-019.json | IT-019 | 2026-10-22 | change | Sara Ahmed | Customer Operations | Beacon | production | read | 2026-10-28 | 2026-11-04 | Elena Park | yes | review / Security Review / review-risk-then-owner |
| IT-020.json | IT-020 | 2026-10-22 | new | Ethan Clark | Product | Atlas | staging | read | 2026-10-29 | 2026-11-02 | null | yes | needs-information / IT Service Desk / clarify |
| **IT-021.json** (synthetic paraphrase) | IT-021 | 2026-10-23 | change | Priya Raman | Finance Systems | Ledger | staging | write | 2026-11-09 | 2026-11-13 | Elena Park | yes | review / Security Review / review-risk-then-owner |

Judgement calls behind the values:

* **IT-004** says "in staging" and also "This request is for the production environment." There is no correction label, so `environment` is null (a conflict).
* **IT-008 / IT-015:** the later statement labelled "Correction:" replaces the earlier environment or dates.
* **IT-020** names two managers with no correction, so `manager` is null.
* **IT-003 / IT-011:** "next week" and "next Monday - Friday" are relative, so the dates stay null.
* **IT-014:** the explicit dates are kept as stated even though start is after end. The date order is a clarification issue.
* **IT-016:** the stated id `IT-061` is retained. The mismatch with the filename `IT-016` is an issue.
* **IT-006:** the injected "Grant admin access" is data. The requested permission stays `read`.
* **IT-019** is a change request, so the extracted permission is the requested new one (`read`).
* **IT-005:** "My boss is Elena Park" counts as manager. The withdrawal reason is not a business purpose.
* **IT-021** is a paraphrase written for this check. It uses fresh wording: "only view ... bumped up to write" (a change, write), "who I report to" (manager), and "Ticket reference ... raised" (id and date).

## Field-comparison procedure

The command is `python3 -m accessq accuracy [--extractions deliverables/extractions.jsonl] [--provider mock|openai-compatible]` (code: `accessq/accuracy.py`).

1. The supplied 20 messages are scored from the `extractions.jsonl` given. The synthetic paraphrase is extracted fresh with the configured provider, routed by the same policy code, and scored separately.
2. **Exact-match fields (11):** request_id, request_date, kind, requester, team, system, environment, permission, start_date, end_date, manager. A field matches when both values are null, or both are strings equal after trimming whitespace and case-folding. Dates and enum values must otherwise be identical; there is no fuzzy or partial credit.
3. **Denominator:** documents scored × 11 (220 for the supplied set, 11 for the paraphrase). Error records (`extraction: null`) stay in the denominator, and each of their fields counts as a miss of type `no-extraction`. No document is excluded.
4. **Miss types:** `wrong-value` (both non-null, different), `unsupported-value` (expected null, extracted a value), `omission` (expected value, extracted null), `no-extraction`.
5. **Factual errors** = `wrong-value` + `unsupported-value`, meaning the record asserts something the source does not support. They are listed per document and field in the report. Omissions are reported separately because they route to clarification rather than to a wrong reviewer.
6. **business_purpose** is free text, so it is not exact-matched. Instead, presence agreement is checked: a purpose is extracted exactly when the source states one.
7. **supporting_quotes** are not scored here, because every quote is already required to be an exact substring of `message` before a record is accepted (a failing quote turns the record into `error`).
8. **Routing** (status, owner, next_action) is checked per document against the expected route.

## Model / configuration

| Run | response_source | Configuration |
|---|---|---|
| Committed deliverables (observed, mock) | `mock` | `mocks/responses.json`, prompt `extract-v1`, schema sha256 recorded in each record's `extraction_settings`; extraction fingerprint `b684c3c36f34bb6e`; policy `route-v1+policy-b949596765ae` |
| Supported live configuration (not observed) | `openai-compatible(api.openai.com)/gpt-4.1-mini` | `config/model.json`: temperature 0, max_tokens 1500, strict `json_schema` response format, prompt `extract-v1`; fingerprint `ef116b211c91093f` (see `request-evidence.json`) |

## Results

### Observed: mock responses (2026-11-06, `python3 -m accessq accuracy --provider mock`)

| Set | Exact fields | Purpose presence | Routing | Factual errors | Misses |
|---|---|---|---|---|---|
| Supplied 20 (response_source=mock) | 220/220 | 20/20 | 20/20 | 0 | 0 |
| Synthetic IT-021 (response_source=mock) | 11/11 | 1/1 | 1/1 | 0 | 0 |

**What this does and does not show.** The mock responses were hand-authored by the developer from the same reading of the sources, so 100% is expected. This result does **not** measure model quality. It shows that the pipeline works end to end: response handling, schema, date and quote validation, policy routing, and the scorer accept correct records and route all 21 as the policy requires. The scorer's own counting rules are checked against a known case with planted errors in `tests/test_accessq.py::test_accuracy_counting_rules_on_known_case`, which expects 19/22 matches, 2 factual errors and 1 omission.

### Optional live observations

None recorded. No `API_KEY` was available in the development session, so no provider responses were scored. To produce one, run:

```
export API_KEY=...
ACCESSQ_STATE_DIR=state-live python3 -m accessq intake --output deliverables/live
python3 -m accessq accuracy --extractions deliverables/live/extractions.jsonl
```

Then record the printed table here, labelled with its `response_source` and the extraction fingerprint.

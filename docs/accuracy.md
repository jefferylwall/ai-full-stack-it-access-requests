# Source expectations and accuracy procedure

The reference set is the 20 supplied messages and the authored IT-021 paraphrase
in `tests/inputs/IT-021.json`. The 21 entries in
[`accuracy/expectations.json`](../accuracy/expectations.json) contain every
expected scalar, exact source evidence for each fact, explanations for absence
or uncertainty, required issue meanings, and the policy route. The registry also
retains the decoded source message and its original-byte SHA-256. Its facts come
from the approved catalogue in `planning/plan.md`, the messages and office policy.
They were authored independently of measured application output. Frozen E files
under `tests/expectations/` and saved replies under `tests/mock_responses/` are
separate artifacts. Comparison never edits or regenerates either artifact.

This is an agent source check. Human requirement validation and any facilitator
acceptance remain separate; this document does not provide a human verdict.

With the repository's virtual environment active, run:

```bash
python3 -m access_intake intake materials/requests tests/inputs/IT-021.json
python3 -m access_intake export --output deliverables
python3 -m access_intake accuracy --input deliverables/extractions.jsonl --expectations accuracy/expectations.json
```

The comparison command reads measured JSONL and the frozen registry. It does not
run extraction, invoke a provider, or recompute expected routing with application
routing code. A reference source change is rejected until its oracle is reviewed.
The expected request date is 2026-10-22 for every reference message. IDs match the
filenames except IT-016.json, whose stated request_id is IT-061. Each purpose is
the plan-approved exact source sentence, not a normalized paraphrase. The IT-021
message uses fresh wording for IT-001's fact pattern and a distinct request ID.

Compare all twelve scalar fields literally: request_id, request_date, kind,
requester, team, system, environment, permission, business_purpose, start_date,
end_date and manager. JSON null is a value and counts as an opportunity. Absent
keys differ from null; case, whitespace and purpose prose are not normalized.
The corpus denominator stays **252** (21 × 12). Each field has 21 opportunities.
Report the matched count, fraction and per-field counts. A missing, failed or
non-object extraction contributes zero matches for all twelve fields; bad fields
in an otherwise available object lose their own matches and structural errors
are also reported. Extra and duplicate documents are errors, never extra scoring
opportunities. A duplicate cannot be cherry-picked to improve the score.

For example, T30 deliberately changes IT-001 permission from read to admin and
removes IT-002's extraction. Its intended count is **239/252**, because one wrong
fact and twelve missing facts lose thirteen matches. This is a test oracle, not
an observed model accuracy figure. No passing accuracy threshold is supplied by
the brief. The comparison command returns 1 for detected comparison errors and
0 when those checks find none; that exit code is not a release verdict or a live
model acceptance target.

The JSON report lists per-document and per-field differences with expected and
actual values. Categories distinguish missing extractions, missing facts,
fabricated facts, unresolved-conflict errors and explicit-correction errors.
Business-purpose differences retain a literal mismatch and a semantic review
flag: equivalent wording can lose a literal match without being evidence of an
invented business fact. Discuss that semantic distinction separately; never alter
the scalar count to hide it. Array differences and failed coverage checks are
reported separately from scalar errors.

The separate checks use 21 document opportunities each:

- Routes compare the recorded status, owner and next_action with frozen policy
  triples. No successful reference route grants approval.
- Source hashes compare the producing source revision with the registered bytes.
- Evidence checks require exact decoded-message substrings and coverage of the
  registry's required spans. Larger exact quotes can cover smaller required spans.
  Conflicting alternatives and the supplied override instruction must be retained.
- Issue checks require the registry's specified missing, relative, conflicting,
  mismatched, reversed-date or override meanings. The implementation uses curated
  field names and lexical cues for this corpus. It is not a general semantic
  equivalence judge; negation, novel wording and broader office cases need human
  review. Array order and exact issue wording are not business requirements.

Also report literal equality of `supporting_quotes` and `issues`, including order,
against the frozen E arrays. Different order or equivalent issue wording lowers
those separately named literal-array counts. It does not lower scalar counts or
create a business error when required evidence/issue checks still succeed. The
report preserves such differences as observations requiring appropriate review.

Every observation carries source_sha256, the processing fingerprint, policy hash,
fixture identities and hashes, response_source, provider, model, endpoint, request
parameters, extraction/routing versions, prompt hash and schema hash. Prompt hashes
use the UTF-8 instruction string; schema hashes use UTF-8 JSON with sorted keys,
compact separators and unescaped Unicode. These match the configuration actually
recorded with the producing attempt. Missing observations retain empty producing
provenance rather than inventing a model/configuration. Source declarations alone
do not independently authenticate that a provider was contacted.

All-mock records are labelled **mock_fixture_verification**. Such agreement checks
fixtures, plumbing and deterministic policy behavior; it is not live extraction
accuracy or proof of unseen-wording performance. Optional live observations must
be kept separate and labelled with their producing provider/model/configuration.
Mixed or unknown sources are labelled explicitly. No real model or `API_KEY` is
needed for the automated verification procedure.

Observed during Step 7 on Python 3.12.3: `env -u API_KEY python3 -m unittest
discover -s tests -p test_accuracy.py -v` passed T28–T30. Within T30, the recorded
mock configuration was provider `mock`, model `fixture-v1`, temperature 0, the
repository instruction/schema versions and the supplied office policy. The
baseline comparison matched 252/252 scalars; the deliberate corruption matched
239/252 and returned a comparison error. These are observed fixture checks,
not live model measurements. Each test-generated report included the producing
hashes/configuration asserted by T30. Re-run that test to reproduce them; final
saved verification artifacts are produced during handover.

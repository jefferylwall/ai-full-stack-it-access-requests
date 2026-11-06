# Verification

## Commands

| # | Command | Intended comparison |
|---|---|---|
| V1 | `python3 -m unittest discover -s tests -t . -v` | 42 automated tests (table below) |
| V2 | `python3 -m accessq intake --provider mock` | Exit 0. Outcome counts are 20 `extracted` on a fresh state, or 20 `unchanged` on a rerun |
| V3 | `python3 -m accessq intake --provider mock` (again) | `{'unchanged': 20}`, no model calls, still 20 lines in `extractions.jsonl` and 20 queue entries |
| V4 | `python3 -m accessq accuracy --provider mock` | Supplied and synthetic records are compared with `tests/expected/expected_fields.json` (see `accuracy.md`) |
| V5 | `python3 -m accessq queue` / `history <doc>` / `runs` | The operator views agree with `deliverables/queue.json` |
| V6 | `python3 -m accessq evidence` | `request-evidence.json` contains the model ID and settings, `Authorization: Bearer ***REDACTED***`, and each example response classified by the real handler |

## Assertions in the test suite (`tests/test_accessq.py`)

The tests drive the **production** `OpenAICompatibleProvider` through a fake HTTP transport, so request building, retry/backoff and response handling are the code paths that ship. Expected facts come from `tests/expected/expected_fields.json`, not from the mock responses.

| Requirement | Test(s) | Assertion |
|---|---|---|
| Complete request | `test_complete_request_routes_to_system_owner` | IT-001 fields equal the expected values. The route is System Owner / review-request. `source_sha256` equals SHA-256 of the file bytes. Every quote is a substring of `message` |
| All supplied messages | `test_all_supplied_messages_match_source_derived_expectations` | All 20 records match the 11 expected fields and the expected route |
| Missing dates | `test_missing_dates_need_clarification` | IT-010 (no end), IT-011 (relative), IT-003 go to IT Service Desk / clarify with null dates |
| Conflicting details | `test_conflicting_details_need_clarification` | IT-004 environment and IT-020 manager are null with a conflict flag, and both alternative quotes are kept |
| Corrections | `test_explicit_corrections_replace_values` | IT-008 environment is staging and IT-015 start is 2026-10-29 |
| Embedded instructions | `test_embedded_instructions_are_data_and_go_to_service_desk`, `test_override_backstop_when_model_does_not_report_it` | IT-006 goes to inspect-message with permission still `read` and the override text kept as a quote. The text never appears in the system prompt and is sent only as a JSON string value. The regex backstop routes it even when the model reports no issue |
| Policy rules 2–5 | `test_withdrawal_identifier_mismatch_other_and_date_order`, `test_security_review_for_production_write_admin` | withdrawal → confirm-withdrawal. IT-016 keeps `IT-061` with `document_id` IT-016.json → clarify. Kind `other` → clarify. Start after end → clarify. Production, write or admin → Security Review |
| Paraphrase | `test_synthetic_paraphrase` | IT-021 matches its expected fields and route |
| Refusal | `test_refusal` | `message.refusal` gives error / IT Service Desk / retry-extraction with a null extraction and the retry command in `error` |
| Interrupted response | `test_interrupted_truncated_response`, `test_interrupted_missing_finish_reason` | `finish_reason` `length` or null → `interrupted` error |
| Malformed output | `test_malformed_json_output`, `test_schema_violation`, `test_bad_date_format`, `test_quote_not_in_source`, `test_non_json_body_and_no_choices`, `test_content_filter` | Each becomes an error record with the expected code |
| Provider failure | `test_provider_outage_exhausts_retries`, `test_timeout`, `test_auth_failure_not_retried`, `test_missing_api_key`, `test_rate_limit_then_success_is_retried` | 503 is tried `max_retries+1` times, then error. Timeout gives error. 401 is tried once. A missing key makes no call. 429 followed by success gives a valid record after 2 attempts |
| Failure isolation | `test_provider_failure_does_not_block_other_messages`, `test_invalid_input_file_is_isolated` | One failing document leaves 19 (or 20) extracted, and the queue counts 1 error |
| Unchanged rerun | `test_unchanged_rerun_reuses_results` | The second run makes 0 model calls. Each output has exactly 20 unique documents, and there are 20 history files in total |
| Changed source | `test_changed_source_creates_new_result_and_keeps_old` | New sha in current and in the jsonl. History holds both hashes in order. The old source bytes are kept. `last_valid_attempt` points to the old attempt |
| Failed retry after success | `test_failed_retry_after_prior_success` | Current is `error` with a null extraction. `last_valid_attempt` has the earlier attempt id and sha. History is `[review, error]` and keeps the old extraction. The queue shows retry-extraction. A later good retry restores `review` |
| Settings / policy changes | `test_model_config_change_refreshes`, `test_policy_change_reroutes_without_model_call` | A model change re-extracts. A timeout or concurrency change does not. A policy version change re-routes with 0 model calls and records `attempt_kind: reroute` |
| Stop and resume | `test_interrupted_run_can_be_resumed` | A KeyboardInterrupt mid-run leaves completed records and `pending` ones, the queue still lists 20, and the run is recorded `interrupted`. Resume calls the model only for pending/error documents |
| Inspect while pending | `test_inspect_completed_results_while_others_pending` | While IT-020 is blocked, `queue.json` shows 19 done and 1 pending |
| Two operators | `test_two_operators_at_once_process_each_document_once`, `test_locked_document_is_skipped_then_resumable`, `test_stale_lock_from_crashed_operator_is_broken` | Two concurrent runs on the same state make exactly 20 model calls with 1 attempt per document. A locked document is skipped (`busy`) and picked up later. A stale lease is broken |
| Outputs agree | `test_outputs_agree_after_mixed_run` | `queue.json.extractions_sha256` equals the hash of `extractions.jsonl`, and each entry's sha, attempt and route match its extraction line |
| Credentials | `test_credentials_are_redacted`, `test_request_uses_strict_json_schema_and_configured_model` | The API key appears in no state, log or output file, even when the provider echoes it, and is absent from the redacted request. The request uses the configured model, temperature 0 and strict json_schema |
| Scorer rules | `test_accuracy_counting_rules_on_known_case` | Planted errors are counted as 19/22 matched, 1 omission, 1 wrong value, 1 unsupported value |

## Observed results (2026-11-06, CPython 3.12.3, Linux, mock provider)

These are observed results, not expectations:

* **V1:** `Ran 42 tests ... OK`.
* **Fault-injection check of the suite** (each mutation applied to a temporary copy of the repo, then the suite was run):
  * Security routing disabled for production: 2 failures.
  * Reuse check disabled, so the unchanged rerun calls the model again: 4 failures.
  * Quote-substring check disabled: 1 failure.
  * `finish_reason == "length"` branch removed: the suite still passed. This is an equivalent mutation, because the response is still rejected as `interrupted` by the general `finish_reason != "stop"` rule.
* **V2:** `run ... completed: {'extracted': 20}`, response_source=mock, fingerprint `b684c3c36f34bb6e`, policy `route-v1+policy-b949596765ae`. Queue summary: review 11, needs-information 9 (System Owner 4, Security Review 5, IT Service Desk 11).
* **V3:** `run ... completed: {'unchanged': 20}`.
* **V4:** supplied 220/220 fields and 20/20 routes; synthetic 11/11 and 1/1. These were mock responses authored by the developer, so this is not a model-quality figure (see `accuracy.md`).
* State on the Databricks workspace filesystem (`/Workspace/...`) supported the `O_EXCL` locks, atomic renames and append-only log during V2/V3.
* **Repairs:** no test failures were observed on the first full run of V1. During development, the mock storage was consolidated into `mocks/responses.json` before the suite was written.

## Not verified

* **No live provider call was made.** No `API_KEY` was available, so latency, rate-limit behaviour, strict-schema support and real extraction accuracy of `gpt-4.1-mini` (or any Databricks endpoint) are unmeasured. Live behaviour is covered only by the fake-transport tests.
* Throughput at 2,000 messages was not load-tested. The README figures are estimates.
* Multi-host locking was tested with two operators in one process sharing the same directory, not across machines or network filesystems.
* Python 3.10/3.11 were not exercised (the code avoids 3.12-only features).

## Operating limits

* One state directory per intake. Every operator must share it, on a filesystem with atomic `O_EXCL` create and `rename`.
* `lock_lease_seconds` (600) must be longer than the worst-case time for one document: `timeout_seconds × (max_retries+1)` plus backoff, about 540 s with the defaults.
* During a run, deliverables are re-rendered at most every 5 s and always at the end. `python3 -m accessq queue` reads the live state directly.
* Each attempt adds one history file per document. Rendering reads all current records, which is fine for thousands of documents but not designed for millions.

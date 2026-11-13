# Requirement validation

One row per numbered requirement in [spec.md](spec.md). Verdicts, evidence and notes are left blank for human review. Open questions (Q IDs) are not requirements.

| Spec line | Requirement | Met or not met | Evidence | Note |
|---|---|---|---|---|
| R-01 | The submission provides a runnable application that uses a language model to turn free-text access requests into structured records and an IT review queue. | | | |
| R-02 | The application starts with the 20 supplied JSON exports and accepts other messages in the same export format. | | | |
| R-03 | The application exposes a CLI, service or web interface suited to the IT team. | | | |
| R-04 | Operators can start intake through the documented interface. | | | |
| R-05 | Operators can inspect completed results while other requests remain pending, including when one model response is slow. | | | |
| R-06 | Operators can distinguish completed work from unfinished work and resume from recorded state after a stop or outage. | | | |
| R-07 | Overlapping operators processing the same intake leave a coherent current queue and a trustworthy record of earlier activity. | | | |
| R-08 | Queue entries and supporting extraction records agree on the source version they represent. | | | |
| R-09 | Absence of a document from a later export batch does not withdraw its earlier request. | | | |
| R-10 | The current view reflects the latest processed source and retains earlier results for follow-up. | | | |
| R-11 | A failed new attempt is visible as a failure and any older result is identifiable by its earlier attempt and source version. | | | |
| R-12 | Changes to relevant model configuration or review policy refresh affected results and leave the producing settings and policy version identifiable. | | | |
| R-13 | During slow responses, rate limits or temporary model unavailability, operators can locate affected messages, see pending work and resume without losing completed work. | | | |
| R-14 | Each UTF-8 JSON input file is treated as one exported message whose `message` string supplies the extraction source. | | | |
| R-15 | Request facts come from `message`, while export metadata is treated as export description and the export timestamp is not treated as a request fact. | | | |
| R-16 | The input handling supports messages up to 200 English words. | | | |
| R-17 | Each document's identity is its input filename, which is recorded with a SHA-256 hash of the original JSON file bytes. | | | |
| R-18 | Supporting quotes are exact substrings of the decoded `message` value. | | | |
| R-19 | Extraction returns request_id, request_date, kind, requester, team, system, environment, permission, business_purpose, start_date, end_date, manager, supporting_quotes and issues using the supplied schema. | | | |
| R-20 | Extracted dates use YYYY-MM-DD. | | | |
| R-21 | Absent or unresolved scalar facts are null and each missing or conflicting fact is explained in issues. | | | |
| R-22 | Conflicting alternatives are preserved in exact supporting quotes. | | | |
| R-23 | An explicitly labelled later correction replaces the earlier value, while contradictions without a correction remain unresolved. | | | |
| R-24 | A relative date remains unresolved. | | | |
| R-25 | A change request's extracted permission is the requested new permission. | | | |
| R-26 | A request identifier stated in the message is retained when it differs from the filename identifier, and the mismatch is recorded in issues. | | | |
| R-27 | Messages remain independent documents even when multiple messages concern one request. | | | |
| R-28 | Response structure and completion status are validated before extracted data is used. | | | |
| R-29 | Review routing applies the office-policy rules in their specified order after extraction validation. | | | |
| R-30 | An instruction-override attempt routes to IT Service Desk with status `review` and next_action `inspect-message`. | | | |
| R-31 | Source messages remain data throughout extraction and routing, with an attempted instruction retained as evidence and identified in issues. | | | |
| R-32 | A withdrawal containing request_id and request_date routes to IT Service Desk with status `review` and next_action `confirm-withdrawal`, allowing other fields to be null with absent facts identified in issues. | | | |
| R-33 | After the earlier rules, kind `other` or null, conflicting details, an identifier mismatch or any missing required fact routes to IT Service Desk with status `needs-information` and next_action `clarify`. | | | |
| R-34 | New and change requests require request_id, request_date, requester, team, system, environment, permission, business_purpose, start_date, end_date and manager for completeness. | | | |
| R-35 | A start_date after end_date is recorded as a clarification issue. | | | |
| R-36 | Complete new or change requests involving production, write or admin route to Security Review with status `review` and next_action `review-risk-then-owner`. | | | |
| R-37 | The Security Review workflow places risk review and its recommendation before the system owner's decision. | | | |
| R-38 | Complete staging/read requests route to System Owner with status `review` and next_action `review-request`. | | | |
| R-39 | Each request displays its status, owner and next action. | | | |
| R-40 | All successful intake outputs await human review, and a named manager is treated as a contact rather than an approval decision. | | | |
| R-41 | Extraction failure produces status `error`, owner `IT Service Desk`, next_action `retry-extraction`, null extraction and an actionable error. | | | |
| R-42 | Each message is processed independently so failure leaves unaffected messages moving through intake. | | | |
| R-43 | Each visible processing failure has a reproducible retry command. | | | |
| R-44 | A later failed attempt preserves the last valid result in history and shows the latest attempt's error in the current queue. | | | |
| R-45 | Results are reused when source and relevant processing settings are unchanged, yielding one current queue entry per document. | | | |
| R-46 | Changed input creates a new result, retains the earlier result with its earlier hash and stores prior versions separately from current outputs. | | | |
| R-47 | Malformed output, provider errors, refusal and interrupted responses are handled explicitly. | | | |
| R-48 | Model and provider settings are configurable and a supported configuration is documented. | | | |
| R-49 | The provider integration reads its supplied API key from the environment variable `API_KEY`. | | | |
| R-50 | Credentials are kept in environment variables and operational logs redact credentials. | | | |
| R-51 | Automated tests cover a complete request. | | | |
| R-52 | Automated tests cover missing dates. | | | |
| R-53 | Automated tests cover conflicting details. | | | |
| R-54 | Automated tests cover embedded instructions. | | | |
| R-55 | Automated tests cover provider failure. | | | |
| R-56 | Automated tests cover an unchanged rerun. | | | |
| R-57 | Automated tests cover changed source content. | | | |
| R-58 | Expected results are built from supplied messages and policy independently of the extraction outputs being measured. | | | |
| R-59 | Verification includes an authored paraphrased synthetic request that exercises extraction on fresh wording. | | | |
| R-60 | Verification includes checks for refusal or interrupted responses. | | | |
| R-61 | Verification includes a failed retry after a prior success. | | | |
| R-62 | Documentation describes how the implementation supports the stated operating context and includes checks for operational situations handled by its design. | | | |
| R-63 | Documentation records workload and shared-operation assumptions, interruption behavior and verification limits. | | | |
| R-64 | Repository instructions enable the next operator to prepare intake, inspect the queue and follow up on failures using setup and configuration guidance and commands. | | | |
| R-65 | README.md documents setup, the supported runtime and provider, exact intake, test and accuracy-check commands, environment-variable names and queue inspection. | | | |
| R-66 | Submitted source code and a dependency lockfile include the runnable application, extraction prompt, schema and model request configuration. | | | |
| R-67 | deliverables/extractions.jsonl contains one current record per message with document_id, source_sha256, extraction, status, owner, next_action, error and response_source. | | | |
| R-68 | Each current extraction is a schema-conforming object on success or null on error, and its error field is null on success. | | | |
| R-69 | Each extraction record's response_source identifies `mock` or the provider and model ID that produced it. | | | |
| R-70 | deliverables/queue.json contains the review queue linked to extraction records by document_id. | | | |
| R-71 | deliverables/accuracy.md specifies source-derived expected fields for all 20 supplied messages and one authored paraphrase. | | | |
| R-72 | deliverables/accuracy.md specifies the field-comparison procedure, exact-match counting and denominator rules, factual-error reporting, and model and configuration. | | | |
| R-73 | deliverables/accuracy.md distinguishes mocked examples from any optional live observations. | | | |
| R-74 | deliverables/request-evidence.json contains a credential-redacted example model request showing the model ID and configuration used plus the implemented response handling. | | | |
| R-75 | deliverables/verification.md records verification commands and assertions, intended comparisons, operating limits and clearly identified observed results or repairs. | | | |
| R-76 | The submission includes a coding-session record paired with the submitted commit and collected through the facilitator's capture setup. | | | |
| R-77 | The implemented provider integration and source-grounded validation procedure are submitted. | | | |
| R-78 | Reproducible development examples use mocked responses. | | | |
| R-79 | Each recorded observation identifies its configuration and whether it used a mock or provider response. | | | |
| R-80 | The application, outputs and session capture are committed and pushed to the repository's default branch. | | | |
| R-81 | The Classroom submission contains only the root repository link. | | | |
| R-82 | Each successful extraction is a JSON object. | | | |
| R-83 | Every extraction includes all 14 properties listed in the schema's required array. | | | |
| R-84 | Extraction objects contain no properties beyond those declared in the schema. | | | |
| R-85 | request_id, request_date, kind, requester, team, system, environment, permission, business_purpose, start_date, end_date and manager each accept only a string or null. | | | |
| R-86 | kind is limited to `new`, `change`, `withdrawal`, `other` or null. | | | |
| R-87 | environment is limited to `staging`, `production` or null. | | | |
| R-88 | permission is limited to `read`, `write`, `admin` or null. | | | |
| R-89 | supporting_quotes is an array whose items are strings. | | | |
| R-90 | issues is an array whose items are strings. | | | |

Counts: met — pending; not met — pending; unreviewed — 90.

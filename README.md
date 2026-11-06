# accessq — IT access-request intake for Northstar Labs

`accessq` is a command-line application. It reads exported access-request messages (JSON), uses a language model to turn each one into a record that conforms to `snapshot.schema.json`, and applies `materials/office-policy.md` to build a review queue for the IT team. Approval decisions stay with the named human reviewers. The application only prepares the queue. The task brief is in `CHALLENGE.md`.

## Supported runtime and provider

* **Runtime:** CPython 3.10 or later (developed and tested on 3.12.3). Only the standard library is used, so `requirements.lock` lists no packages.
* **Provider (supported configuration):** any **OpenAI-compatible Chat Completions** endpoint (`POST {base_url}/chat/completions`) that supports `response_format: json_schema` (strict). `config/model.json` defaults to OpenAI (`https://api.openai.com/v1`, model `gpt-4.1-mini`, temperature 0, max_tokens 1500). Databricks Model Serving also works: set `ACCESSQ_BASE_URL=https://<workspace-host>/serving-endpoints` and `ACCESSQ_MODEL=<endpoint-name>`.
* **Mock provider:** `--provider mock` replays the hand-authored responses in `mocks/responses.json`. These go through the same response handler as live responses. Records made this way carry `response_source: "mock"`.

The committed `deliverables/` were produced with the **mock** provider. No live provider call was made while building this version (see `deliverables/verification.md`).

## Setup

```
git clone <repo> && cd <repo>
python3 --version                     # 3.10+
python3 -m pip install -r requirements.lock   # no-op: no third-party dependencies
export API_KEY=...                    # supplied by the facilitator; never written to disk
```

Run every command from the repository root. The package is executed in place with `python3 -m accessq`; nothing needs installing.

## Quick start (exact commands)

```
# 1. Tests (no network, no API key needed)
python3 -m unittest discover -s tests -t . -v

# 2. Intake - reproducible mock run (writes deliverables/extractions.jsonl and deliverables/queue.json)
python3 -m accessq intake --provider mock

# 2b. Intake - live provider (uses API_KEY and config/model.json)
python3 -m accessq intake

# 3. Inspect the queue
python3 -m accessq queue

# 4. Accuracy check against source-derived expected fields
python3 -m accessq accuracy --provider mock     # scores deliverables/extractions.jsonl + the synthetic paraphrase
python3 -m accessq accuracy                     # same, extracting the paraphrase with the live provider

# 5. Credential-redacted request evidence
python3 -m accessq evidence --document IT-001.json
```

To keep a live run separate from the committed mock outputs:

```
ACCESSQ_STATE_DIR=state-live python3 -m accessq intake --output deliverables/live
python3 -m accessq accuracy --extractions deliverables/live/extractions.jsonl
```

Databricks Model Serving instead of OpenAI:

```
export API_KEY=<databricks-token>
export ACCESSQ_BASE_URL=https://<workspace-host>/serving-endpoints
export ACCESSQ_MODEL=<chat-endpoint-name>
python3 -m accessq intake
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `API_KEY` | Bearer credential for the provider (required for live runs) | — |
| `ACCESSQ_PROVIDER` | `openai-compatible` or `mock` | `openai-compatible` |
| `ACCESSQ_BASE_URL` | Provider base URL | `https://api.openai.com/v1` |
| `ACCESSQ_MODEL` | Model ID / serving endpoint name | `gpt-4.1-mini` |
| `ACCESSQ_TEMPERATURE`, `ACCESSQ_MAX_TOKENS`, `ACCESSQ_RESPONSE_FORMAT` | Model request settings (`json_schema` / `json_object` / `none`) | `0`, `1500`, `json_schema` |
| `ACCESSQ_TIMEOUT_SECONDS`, `ACCESSQ_MAX_RETRIES` | Per-request timeout, retries for 429/5xx/timeout/network | `60`, `4` |
| `ACCESSQ_CONCURRENCY` | Parallel extractions per operator process | `4` |
| `ACCESSQ_LOCK_LEASE_SECONDS` | Lease after which a crashed operator's document lock is broken | `600` |
| `ACCESSQ_STATE_DIR` | Shared state directory (history, locks, logs) | `./state` |
| `ACCESSQ_CONFIG` | Alternate settings file | `config/model.json` |
| `ACCESSQ_OPERATOR` | Operator name recorded in runs/logs | `user@host` |
| `ACCESSQ_MOCK_DIR` | Directory holding `responses.json` for the mock provider | `mocks` |

Command-line flags `--provider`, `--model`, `--base-url` and `--concurrency` override these settings for a single run. `--state-dir` is a global flag and goes before the command.

## Commands

| Task | Command |
|---|---|
| Intake (live) | `python3 -m accessq intake` (default input `materials/requests/`; you can also pass files or directories) |
| Intake (mock, reproducible) | `python3 -m accessq intake --provider mock` |
| Inspect the live queue | `python3 -m accessq queue` · `queue --status error` · `queue --owner "Security Review"` · `queue --json` |
| Inspect one record / its history | `python3 -m accessq show IT-016.json` · `python3 -m accessq history IT-016.json` |
| List runs (who, when, outcome) | `python3 -m accessq runs` |
| Resume after a stop or outage | `python3 -m accessq resume` (processes every `pending` or `error` document) |
| Retry specific failures | `python3 -m accessq retry IT-003.json` · `retry --all-errors` (each error record carries its exact retry command) |
| Re-render deliverables from state | `python3 -m accessq export` |
| Write request evidence | `python3 -m accessq evidence --document IT-001.json` |
| Accuracy check | `python3 -m accessq accuracy --provider mock` (to score a live run: `python3 -m accessq accuracy --extractions deliverables/extractions.jsonl`) |
| Tests | `python3 -m unittest discover -s tests -t . -v` |

Intake exits with 0 when every document processed cleanly. It exits with 1 when any document ended in `error` (the errors are printed with retry commands), and with 130 when interrupted.

## Inspecting the queue

There are two views of the same records:

* **Live view (state directory):** `python3 -m accessq queue` prints one line per document with request id, status, owner, next action, and the review flags or error. Filter with `--status review|needs-information|error|pending` or `--owner "<owner>"`. Use `--json` for machine-readable output. It reads `state/current/` directly, so it shows progress while another operator's intake is still running.
* **Published files:** `deliverables/queue.json` (summary counts plus entries) and `deliverables/extractions.jsonl` (one current record per document). Both are re-rendered during and at the end of every run, and on demand with `python3 -m accessq export`.
* **Drill-down:** `python3 -m accessq show <document_id>` prints the full current record, including the extraction, supporting quotes, settings, policy version and `last_valid_attempt`. `python3 -m accessq history <document_id>` lists every attempt with its source hash, route, response source and error. `python3 -m accessq runs` lists every intake, resume and retry run with operator and outcome counts.

Typical follow-up:

1. `python3 -m accessq queue --status error` to find failed documents and read the cause.
2. Fix the cause (wait out an outage, correct `API_KEY`, raise `ACCESSQ_MAX_TOKENS` for truncation, or correct a malformed export).
3. Run `python3 -m accessq resume` for all pending and error documents, or the record's `retry_command` (`python3 -m accessq retry <document_id>`) for one.
4. `python3 -m accessq queue --status needs-information` to see requests that need clarification from the requester; `review_flags` and `issues` say what is missing or conflicting.

## Reading the queue

`deliverables/queue.json` has one entry per document (`document_id` = input filename), linked to `deliverables/extractions.jsonl` by `document_id`, `source_sha256` and `attempt_id`. `queue.json.extractions_sha256` is the hash of the exact `extractions.jsonl` written in the same step, so you can check that the two files agree.

| status | owner | next_action | meaning |
|---|---|---|---|
| `review` | IT Service Desk | `inspect-message` | message tried to override instructions (policy rule 1) |
| `review` | IT Service Desk | `confirm-withdrawal` | withdrawal with id + date (rule 2) |
| `needs-information` | IT Service Desk | `clarify` | other/unknown kind, conflict, id mismatch, missing required fact, start after end (rule 3) |
| `review` | Security Review | `review-risk-then-owner` | complete production / write / admin request (rule 4) |
| `review` | System Owner | `review-request` | complete staging + read request (rule 5) |
| `error` | IT Service Desk | `retry-extraction` | extraction failed; `error` names the cause and the retry command |
| `pending` | IT Service Desk | `await-extraction` | registered by a run that has not finished it yet |

`review_flags` lists the reasons the application derived (missing facts, conflicts, override detection). `issues` lists the model's explanations. `last_valid_attempt` points to the previous good result (its source hash and history file) whenever the current entry is `error` or `pending`.

## How it works

1. **Discover:** each `*.json` export is read as raw bytes. `source_sha256` is computed over the original bytes and `document_id` is the filename. A malformed export becomes an `error` record and does not stop the run.
2. **Register:** documents that need work are marked `pending` at once, so the queue shows unfinished work from the start.
3. **Extract** (thread pool, `concurrency` workers): the prompt is in `accessq/prompt.py`. The message is passed as a JSON string value and labelled as data. The request uses strict `json_schema` with `snapshot.schema.json`.
4. **Validate** (`accessq/provider.py: handle_completion`, `accessq/validation.py`): HTTP status → body shape → refusal → `finish_reason` (must be `stop`) → JSON parse → schema → `YYYY-MM-DD` dates → every supporting quote must be an exact substring of `message`. Any failure produces an `error` record with no extraction.
5. **Route** (`accessq/policy.py`): deterministic code applies the policy rules in order. The model never chooses the route. A regex backstop detects override attempts even when the model does not report one.
6. **Record** (`accessq/store.py`): a write-once attempt file is saved in `state/attempts/<doc>/`, `state/current/<doc>.json` is replaced atomically, and the source bytes are saved to `state/sources/<sha>.json`. The deliverables are re-rendered every few seconds during the run, so reviewers can start on completed items while others are pending.

**Reuse and refresh:** a document is skipped (`unchanged`) when its source hash and the *extraction fingerprint* match the current valid record. The fingerprint is a hash of provider, model, base URL, temperature, max_tokens, response format, prompt version and text, and schema hash. Changing any of these re-extracts the affected documents. Changing `materials/office-policy.md` or the routing code version changes `policy_version`, which re-routes the stored extractions with no model calls. Timeouts, retries and concurrency are not part of the fingerprint. Every record shows the `extraction_settings`, `extraction_fingerprint`, `policy_version` and `response_source` that produced it.

## Operating assumptions and limits (handover notes)

* **Workload:** 200–400 messages per day takes minutes at `concurrency=4` with a provider latency of about 2–5 s. A 2,000-message morning is about 2,000 sequential-equivalent calls: plan on roughly 15–40 minutes at concurrency 4–8, depending on the provider's rate limit. Raise `ACCESSQ_CONCURRENCY` only as far as the shared rate limit allows, because 429s are retried with backoff but slow everyone down. These figures are estimates. They were not measured against a live provider.
* **Shared operation:** operators must point `ACCESSQ_STATE_DIR` at the **same** directory, on a filesystem where `O_CREAT|O_EXCL` and `rename` are atomic (local disk, or a POSIX shared filesystem; avoid eventually-consistent object-store mounts). Per-document lease locks make sure each document is processed by one operator at a time. A second operator skips it (`busy`) and processes the rest. Deliverable rendering is serialized by a separate lock.
* **Interruptions:** stopping a run (Ctrl-C, deploy, crash) keeps every completed record. Unfinished documents stay `pending`. Run `python3 -m accessq resume`. A lock held by a dead process on the same host is broken right away, and a lock held from another host is broken after `lock_lease_seconds`. The lease must be longer than `timeout_seconds × (max_retries+1)` plus backoff (the defaults are 600 s versus about 540 s worst case).
* **Provider trouble:** 429, 5xx, timeouts and network errors are retried (exponential backoff with jitter, honouring `Retry-After`, capped at `backoff_max_seconds`). After the retries run out, the document becomes `error` and the rest keep going. Auth errors (401/403), bad requests, refusals, content-filter stops, truncated or incomplete responses, malformed JSON and validation failures are not retried automatically. Fix the cause (or wait out the outage), then run `resume` or the record's `retry_command`.
* **History:** prior versions are kept in `state/attempts/` and `state/sources/`, separate from the current outputs. The state directory is git-ignored. Back it up if you need history to survive a redeploy.
* **Logs:** `state/logs/intake.log` is JSONL. The API key value and any `Bearer …` token are redacted before writing.
* **Not handled:** linking several messages about one request (each message is an independent document, as the policy requires), approval workflow, notifications, and a web UI.

## Repository layout

| Path | Content |
|---|---|
| `accessq/cli.py` | Command-line interface (`python3 -m accessq ...`) |
| `accessq/prompt.py` | Extraction prompt (`PROMPT_VERSION`) |
| `accessq/provider.py` | Model request building, retries and response handling; mock provider; credential redaction |
| `accessq/validation.py` | Schema, date and exact-quote validation |
| `accessq/policy.py` | Review-queue routing rules and `policy_version` |
| `accessq/intake.py` | Intake orchestration, reuse/refresh and output rendering |
| `accessq/store.py` | Shared state, history, lease locks and logs |
| `accessq/accuracy.py` | Field-comparison scorer |
| `config/model.json` | Model request configuration (provider, base URL, model, temperature, max_tokens, response format, timeouts, retries, concurrency) |
| `snapshot.schema.json` | Record schema (sent to the model as the strict `json_schema` response format and used for validation) |
| `mocks/responses.json` | Hand-authored mock responses for reproducible development |
| `tests/` | Test suite, source-derived expected fields, synthetic paraphrase |
| `requirements.lock` | Dependency lockfile (standard library only) |
| `deliverables/` | Submitted outputs (below) |

## Deliverables

| File | Content |
|---|---|
| `deliverables/extractions.jsonl` | One current record per message: `document_id`, `source_sha256`, `extraction`, `status`, `owner`, `next_action`, `error`, `response_source`, plus attempt, settings and policy metadata |
| `deliverables/queue.json` | Review queue linked to the extraction records by `document_id` |
| `deliverables/accuracy.md` | Expected fields, comparison procedure and counting rules, model configuration, and the observed (mock) results |
| `deliverables/request-evidence.json` | Credential-redacted example request with model ID and configuration, plus the response-handling rules and handled examples |
| `deliverables/verification.md` | Verification commands, assertions, observed results, limits |

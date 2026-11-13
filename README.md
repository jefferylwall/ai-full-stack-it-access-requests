# AI Full Stack Challenge — IT Access Requests

Access-request intake for human review. The current implementation captures
source bytes, builds message-only extraction prompts, validates completions,
supports deterministic offline provider scripts and applies ordered review
routing. SQLite stores source revisions, attempts and coherent current views.
Independent workers support leases, finite waits and explicit recovery. The CLI
provides intake, inspection, recovery, retries and paired export publication.
The remaining work is tracked in [the build plan](planning/plan.md).

Prerequisites: Python 3.12 with `venv` and, for preparing wheels, `pip`. Prepare the
pinned wheel cache before tests; this is the only dependency-download step.
A facilitator can supply the cache for a fully offline machine. Include wheels
for the installed Python 3.12 interpreter and platform used by the application.

```bash
python3 -m pip download --only-binary=:all: --dest .wheel-cache -r requirements.lock -r requirements-bootstrap.lock
python3 -m venv --without-pip .venv
.venv/bin/python tools/install_locked.py --wheel-cache .wheel-cache
source .venv/bin/activate
python3 -m pip check
```

The bootstrap uses `--no-index` and works without host `ensurepip`. Run tests
from the repository root with that environment active:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

Tests use saved replies under `tests/mock_responses/`. Extraction tests forbid
socket access and reads of `API_KEY`. Mock agreement verifies the fixtures and
application behavior; it does not measure live model accuracy.
T34 creates a clean venv, installs from the cache, runs the CLI commands and runs
the full suite there. Its nested suite repeats artifact checks without cloning
recursively. T35 exercises 400 and 2,000 messages. Set
`ACCESS_INTAKE_WHEEL_CACHE` to use another prepared cache. Tests never download.

Start with a supplied message and inspect it using the mock provider:

```bash
python3 -m access_intake intake materials/requests/IT-001.json
python3 -m access_intake queue
python3 -m access_intake work
python3 -m access_intake history IT-001.json
python3 -m access_intake resume
python3 -m access_intake retry IT-001.json --force
python3 -m access_intake export --output deliverables
python3 -m access_intake export --read deliverables
```

The default database is `.access-intake.sqlite` in the current directory. To share
another database, put `--database PATH` before the command in every process.
`intake` accepts files or flat directories of JSON exports. `--mock-dir PATH`
selects saved replies named by the lowercase document filename, such as
`it-001.json`; the default directory is `tests/mock_responses/`. Source bytes and
reply bytes are captured at admission. Retry and resume use those captured bytes
and producing settings even if the original files have moved or disappeared.
Mock mode needs no credentials. The OpenAI Responses adapter is also implemented;
select it explicitly with `--provider openai` or `PROVIDER=openai`. It reads
`API_KEY` only at live request time. `MODEL`, `PROVIDER_ENDPOINT` and
`REQUEST_SETTINGS` configure its model, HTTPS endpoint and JSON request parameters;
CLI overrides are `--model`, `--endpoint` and `--request-settings` before the command.
The documented default is `gpt-6-luna`, max output tokens 2048 and reasoning effort
none, using strict structured output and no tools. See
[`docs/provider.md`](docs/provider.md) and
[`config/provider.json`](config/provider.json) for the protocol, credential handling,
supported settings and verification limits. Automated tests replace credentials
and HTTP responses; no live model accuracy or availability is claimed.

Run the complete reference corpus and its independent comparison:

```bash
python3 -m access_intake intake materials/requests tests/inputs/IT-021.json
python3 -m access_intake export --output deliverables
python3 -m access_intake accuracy --input deliverables/extractions.jsonl --expectations accuracy/expectations.json
```

[`accuracy/expectations.json`](accuracy/expectations.json) freezes 21 source-derived
expectations with evidence for each scalar and the expected policy route.
[`docs/accuracy.md`](docs/accuracy.md) specifies the counting procedure, error
categories, provenance and semantic review limits. The scalar denominator is
252, including nulls and failed/missing extractions. Literal array comparisons
and evidence/issue/routing checks are separate. Mock reports are explicitly
labelled fixture verification and do not measure live model accuracy.

Commands print JSON. Processing commands return 1 when current work contains
failures or unfinished attempts, and 0 when all current work succeeded. Usage
errors return 2. Run `queue` or `work` in another terminal while intake runs.
Processing states and policy statuses are separate: pending/running work has no
invented review status. Errors include a quoted, self-contained retry command
that selects the correct database. `retry DOCUMENT --force` creates a new attempt;
`resume` recovers expired leases and processes pending work without retrying
recorded failures. Global worker options also go before the command:
`--workers`, `--request-timeout`, `--lease-seconds`, `--commit-margin` and
`--pace-seconds`.

Review routing follows the office policy in order: inspect instruction overrides,
confirm withdrawals, clarify incomplete/conflicting requests, review risk, then
send complete staging/read requests to the system owner. Security Review forwards
its recommendation to the system owner for a decision. Every successful intake
awaits human review; a manager is a contact, and no route grants access.

Override recognition currently covers the plan's explicit supplied attack.
`route(..., override_detector=...)` accepts a replacement detector after broader
recognition rules receive human review. These tests do not establish general
attack detection or live extraction accuracy. Unsettled cases such as incomplete
withdrawals, blank scalars and invalid absolute dates raise
`PolicyClarificationRequired` where encountered instead of assigning an invented
review route. The open questions are recorded in the build plan.

The store uses a local SQLite database on one host. Each calling thread or
process opens its own `Store`. Admission and completion use short write
transactions; provider calls happen between `start()` and `finish()`. The latest
admitted generation is current even while pending or running. A late completion
with a valid token may finish its historical attempt but cannot replace that
current generation. Expired or reclaimed worker tokens cannot commit results.
`snapshot()` reads queue, extraction and work views from one committed snapshot;
`earlier_result` explicitly identifies an older success during replacement or
failure. `history(document_id)` retains all attempts and their source hashes.

Processing fingerprints cover provider, model, endpoint, prompt, schema, request
parameters, extraction/routing versions, policy bytes and mock fixture identities
and bytes. Changed settings conservatively refresh every stored document;
omission alone retains earlier documents. Unchanged successful work is reused,
and unchanged failures stay visible until a forced retry creates another attempt.
Credentials, database paths, worker counts and display settings are excluded
from persisted processing settings. Captured source and fixture bytes are retained
without pruning. Duplicate basenames in one intake are rejected before admission.

Routing supports the supplied policy and the plan's labelled test-only rule-five
variant. Other policy changes require a reviewed routing implementation; policy
prose is not executed.

`WorkerPool` drains claimable pending work using four workers by default. Configure
`workers`, `request_timeout` (default 30 seconds), `lease_seconds` (40 seconds),
`commit_margin` (2 seconds), and `pace_seconds` (0). The lease must exceed the
request timeout plus commit margin. Concurrency and pacing apply within a pool;
overlapping operators each have their own configured pool. Claims commit before
requests start, and each attempt records its worker and lease expiry.

`pool.run(resume=True)` records expired work as interrupted and creates fresh
attempts from captured bytes and producing settings. Unexpired work remains
claimed. Ordinary pending work is processed; recorded failures require an explicit
forced retry. There are no hidden provider retries. A crash after a provider
response can repeat the external call during recovery; fencing prevents duplicate
current results, not duplicate external execution.

Provider waits are finite even for held mocks. A timed-out underlying call keeps
its concurrency slot until it returns, and its late reply cannot publish. If all
slots remain occupied, pending documents stay recorded for a later run; the pool
returns without waiting indefinitely. The OpenAI HTTP transport also enforces
finite I/O timeouts. Step 5 verification uses saved replies, injected errors and
actual worker subprocess termination after claim and before commit, plus two
overlapping operators sharing the local database. It does not verify multi-host
storage or live-provider behavior.

Export refuses unfinished work. It reads one database snapshot, writes
`extractions.jsonl` and `queue.json` into a fresh generation under
`.deliverables-generations/`, flushes them, then atomically replaces the
`deliverables` symlink. Custom output names use a sibling generation directory
with the same naming convention. Existing generation files are never rewritten
or pruned, and exporting does not change attempt history. An existing destination
that is not a symlink is preserved and the export fails.

External readers must resolve the publication symlink once, then open both files
under that resolved generation directory. Opening the two files separately
through `deliverables/` can cross a publication. The CLI's `export --read PATH`
and Python's `pin_export(PATH).read()` follow the paired-read protocol. A killed
publisher leaves either the prior complete generation or the new complete
generation current; staged/orphaned directories are never selected by listing
the generation directory. Step 6 tests exercise both interruption points and a
reader paused between its two file reads. Publication relies on local POSIX
filesystem symlinks, atomic rename and directory/file synchronization.

Build the mock submission bundle after recording workloads:

```bash
python3 -m access_intake.workload --output verification/workload-observations.json
python3 -m access_intake.handover --database .access-intake.sqlite --output deliverables --workloads verification/workload-observations.json
```

The workload command uses fresh temporary databases, the authored paraphrase and
the saved `load-template.json`. It holds the first reply, fails the second once,
records incremental availability, releases/retries, then validates facts, hashes,
counts and coherent exports. Elapsed time and peak concurrency are measured;
there is no throughput acceptance threshold. Actual results and configuration
are retained in the observation file. Handover requires the completed 21-message
mock reference corpus and publishes accuracy, request evidence and developer
verification together with the pair. Generic `export` publishes only the pair;
run `handover` again for a full bundle after later changes.

Save the final full-suite output as `planning/test-run.txt`, then rebuild handover
so its generation evidence references that run. H1 developer review is recorded
in `verification/h1-review.json`. Human requirement validation, capture linkage,
default-branch push and Classroom submission remain separate; the application
never fills `planning/validation.md` or performs those actions.

The supported runtime is Python 3.12; verification uses the installed Python 3.12.3.
Clean-install checks use that same interpreter. Testing multiple Python versions
is not required. The task
and course instructions are in [CHALLENGE.md](CHALLENGE.md) and [docs/](docs/).

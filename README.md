# AI Full Stack Challenge — IT Access Requests

Access-request intake for human review. The current implementation captures
source bytes, builds message-only extraction prompts, validates completions,
supports deterministic offline provider scripts and applies ordered review
routing. SQLite stores source revisions, attempts and coherent current views.
Independent workers support leases, finite waits and explicit recovery. The CLI
is a later step in [the build plan](planning/plan.md).

Create a local environment and install the locked dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.lock
```

Run tests from the repository root with that environment active:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

Tests use saved replies under `tests/mock_responses/`. Extraction tests forbid
socket access and reads of `API_KEY`. Mock agreement verifies the fixtures and
application behavior; it does not measure live model accuracy.

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
prose is not executed. CLI retry commands are not implemented yet.

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
returns without waiting indefinitely. The future live transport must also enforce
finite I/O timeouts. Step 5 verification uses saved replies, injected errors and
actual worker subprocess termination after claim and before commit, plus two
overlapping operators sharing the local database. It does not verify multi-host
storage or live-provider behavior.

The plan targets Python 3.10+; verification so far used Python 3.12.3. The task
and course instructions are in [CHALLENGE.md](CHALLENGE.md) and [docs/](docs/).

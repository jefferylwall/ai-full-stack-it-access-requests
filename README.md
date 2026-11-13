# AI Full Stack Challenge — IT Access Requests

Access-request intake for human review. The current implementation captures
source bytes, builds message-only extraction prompts, validates completions,
supports deterministic offline provider scripts and applies ordered review
routing. SQLite stores source revisions, attempts and coherent current views.
Workers and the CLI are later steps in [the build plan](planning/plan.md).

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
may finish its historical attempt but cannot replace that current generation.
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
prose is not executed. Worker leases, interruption recovery and CLI retry commands
are not implemented yet. Step 4 verification covers the persistence APIs and two
database connections while a mock provider call is held, not worker processes.

The plan targets Python 3.10+; verification so far used Python 3.12.3. The task
and course instructions are in [CHALLENGE.md](CHALLENGE.md) and [docs/](docs/).

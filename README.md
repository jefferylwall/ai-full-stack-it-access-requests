# AI Full Stack Challenge — IT Access Requests

Access-request intake for human review. The current implementation captures
source bytes, builds message-only extraction prompts, validates completions and
supports deterministic offline provider scripts. The CLI and review routing
are later steps in [the build plan](planning/plan.md).

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

The plan targets Python 3.10+; verification so far used Python 3.12.3. The task
and course instructions are in [CHALLENGE.md](CHALLENGE.md) and [docs/](docs/).

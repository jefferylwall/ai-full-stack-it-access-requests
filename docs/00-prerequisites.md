# Before block 1: check your setup

In class you work this Challenge in three blocks, each with its own file in this folder:

| Block | File | What you have at the end |
|---|---|---|
| 1. Research | `docs/01-research-to-spec.md` | `planning/spec.md`: the brief as numbered requirements, each with its check |
| 2. Plan | `docs/02-plan-with-test-plan.md` | `planning/plan.md`: build steps with the test plan inside |
| 3. Implement, then validate | `docs/03-implement-and-validate.md` | the code and its tests, and `planning/validation.md`: each requirement met or not met, with evidence |

You do the work with Codex, a coding agent, inside this repository. Each file says what to ask
Codex for, what is on disk at the end, and how to tell it is right.

This file comes first. Four things must be true before block 1 starts, because each block builds on
them: Codex runs, you work in your own clone of the repository, Entire records your coding
sessions (the Challenge asks for that record with your code), and the tests run on mock responses
(saved model replies, so a test needs no model key and no network). Nobody starts block 1 until
the check below prints `READY`.

## Steps

1. **Get your own copy and clone it.** Use the copy of
   `https://github.com/GitRollTraining/ai-full-stack-it-access-requests` the facilitator assigned
   you, or fork it to your GitHub account. Then:

   ```bash
   git clone https://github.com/<your-account>/<your-repository>.git
   cd <your-repository>
   ```

   Run every command in these files from the root of that clone.

2. **Check Python.** The class setup minimum is 3.10 or later. This application
   selects Python 3.12; `python3 --version` should print 3.12.x (the installed
   verification interpreter is 3.12.3). Use that same interpreter for setup, the
   test suite and clean-install checks. Multiple Python installations or a version
   matrix are not required.

3. **Check Codex.** `codex --version` prints a version, and `codex login status` says you are
   logged in. If not, sign in with the class key the facilitator gives you.

4. **Turn on session capture.** `entire version` prints a version (install the Entire CLI as the
   course's session-capture guide says, if it does not). Then, in the clone:

   ```bash
   entire enable --agent codex
   ```

   `entire status` now says Entire is enabled.

5. **Run the check.**

   ```bash
   python3 tools/check_prerequisites.py
   ```

## What is on disk at the end

Nothing new to commit except what `entire enable` adds (`.entire/` and `.codex/hooks.json`).
Commit those so your session record follows your code:

```bash
git add .entire .codex
git commit -m "Turn on session capture"
```

## How to tell it is right

The check prints one line per check and ends with `READY`:

```text
PASS  1. Codex: ...
PASS  2. Repository: ...
PASS  3. Session capture: ...
PASS  4. Tests on mock responses: ...
READY: all 4 checks passed. Start docs/01-research-to-spec.md.
```

A `FAIL` line says what to do. Fix it and run the check again. If a line still fails, ask the
facilitator before block 1 starts.

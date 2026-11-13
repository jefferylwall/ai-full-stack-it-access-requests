# Block 3. Implement, then validate against the spec

Codex now builds from your plan, one step at a time, each test written before the code it tests and
run on mock responses. Then you **validate**: you check what was built against the spec, line by
line, and mark each line met or not met, with evidence you can open or run again. Validation is
yours, not the agent's. Codex's summary of what it did is a claim about the code, not evidence: an
agent can report a step done that its tests never exercise.

Start from your committed `planning/plan.md` (`docs/02-plan-with-test-plan.md`).

## Part 1. Build, test first, on mock responses

1. Start Codex in the root of your clone and ask it to build `planning/plan.md` in its step order.
   Your request says:
   - for each step, first write the test the plan names, run it and show it failing, then stop;
     after you commit the failing test (`Test first: <step>`), write the code until it passes, then
     stop again so you commit it (`Step <n>: <step>`);
   - every test uses mock responses saved under `tests/mock_responses/`, and no test calls the real
     model or reads `API_KEY`;
   - the expected values in each test are the expected facts of the test plan, not values copied
     from a run;
   - after each step, run the whole test suite and stop if anything fails.
2. You make the commits: Codex's sandbox does not let it write to `.git`, and the commits are your
   record that each test failed before its code existed. If a step's new test already passes
   before any code is written, say so in its commit message (`Test first: <step> (already
   passes)`) rather than making it fail on purpose.
3. Watch the steps. Use the plan's single supported Python 3.12 interpreter for
   setup, clean-install checks and the suite; testing additional Python versions
   is not required. After each step, run the tests yourself, with `API_KEY` unset:

   ```bash
   env -u API_KEY python3 -m unittest discover -s tests
   ```

   (If your plan chose another test command, use it, still with `API_KEY` unset, and write it in
   your README.)
4. When the steps are done, save the full test run and commit it with anything not yet committed:

   ```bash
   env -u API_KEY python3 -m unittest discover -s tests -v > planning/test-run.txt 2>&1
   git add -A
   git commit -m "Build the plan's steps, tests first, on mock responses"
   ```

## Part 2. Validate each spec line

1. Create `planning/validation.md` with one row per line of `planning/spec.md`:

   | Spec line | Requirement | Met or not met | Evidence | Note |
   |---|---|---|---|---|

2. Fill each row yourself. You may ask Codex to point you to code or tests, but you open the
   evidence and you decide the mark:
   - **Evidence** is something a reviewer can open or run again: a test name and its line in
     `planning/test-run.txt`, a file and line in the code, or a command and what it printed.
   - A line is **met** only when its evidence shows the requirement. A test that passes but does not
     test this requirement is not evidence for it.
   - A line with no evidence, or evidence for only part of it, is **not met**, and the note says what
     is missing.
3. Under the table, write the count: how many lines are met, how many are not met.
4. Commit:

   ```bash
   git add planning/validation.md
   git commit -m "Validation: each spec line met or not met, with evidence"
   ```

## What is on disk at the end

- The code and its tests, the tests passing on mock responses.
- `planning/test-run.txt`: the saved run.
- `planning/validation.md`: every spec line marked met or not met, each with its evidence.

## How to tell it is right

- `planning/validation.md` has as many rows as `planning/spec.md`.
- Pick two rows marked met and open their evidence: each shows the requirement, not only a passing test.
- `env -u API_KEY python3 -m unittest discover -s tests` ends with `OK` on a fresh run.
- `git log --oneline` shows a `Test first:` commit before each step's code commit.

## After class, on your own

1. The spec lines marked not met: plan each, build it test first, and mark it again.
2. The review queue: run intake on the 20 exports and check each entry's status, owner and next
   action against the five rules of `materials/office-policy.md`, in order.
3. The accuracy check: compare the extracted fields with the expected facts you wrote from the
   messages, as `CHALLENGE.md` asks for `deliverables/accuracy.md`.
4. The rest of the files `CHALLENGE.md` § What to submit lists under `deliverables/`, and the README
   it asks for. Class time covers the spec, the plan, the build and its validation; these files
   finish the submission.

The facilitator supplies a model key through `API_KEY` for runs against the real model; the tests
never use it.

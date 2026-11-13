# Block 2. Plan: build steps with the test plan inside

A plan turns the spec into ordered steps before any code exists, so you can see and fix a wrong
order or a missing step while it costs nothing. The **test plan** sits inside the plan: one row per
case your tests must cover, each with the input, the mock response the test uses in place of the
model, and the **expected facts**. You write the expected facts from the message and the office
policy, never from the program's output: a test whose expected values were copied from the output
passes whatever the program does.

Start from your committed `planning/spec.md` (`docs/01-research-to-spec.md`).

## The planning skill

This repository supplies a skill for this block: `.agents/skills/plan-from-spec/SKILL.md`. A skill
is a folder of instructions Codex loads when a task matches it. This one tells Codex what the plan
holds and how to check it. Read it once before you start; it is one page.

## Steps

1. Start Codex in the root of your clone and ask it to use the plan-from-spec skill on
   `planning/spec.md`. Ask for the plan only, no code.

   <details><summary>An example request</summary>

   ```text
   Use the plan-from-spec skill on planning/spec.md. Write planning/plan.md only; write no code
   and no tests yet.
   ```

   </details>

2. Read `planning/plan.md` and correct it, with Codex or by hand, until each of these holds:
   - Every spec line appears in § Coverage with a step that builds it and a test or check that shows
     it. A spec line Codex left out goes back in.
   - The test plan has its own row for each case of `docs/requirements.md` § Verification: a
     complete request, missing dates, conflicting details, embedded instructions, provider failure,
     an unchanged rerun, changed source content. Then the paraphrased request you author, a refusal
     or interrupted response, and a failed retry after a prior success.
   - Each row's input is a real export in `materials/requests/` (or the message you wrote), and you
     opened it and read the message.
   - Each expected fact quotes the message words and the policy rule it comes from. Work one row out
     yourself, from the message and the policy, before you accept Codex's version.
   - Each row's mock response is named: a saved reply under `tests/mock_responses/`, an error, or a
     cut-off reply. No test calls the real model, and none needs `API_KEY`.
   - The steps put the mock provider first, and each later step names the test written before it.
   - Choose one supported runtime and verify setup and tests with the same installed
     interpreter. This application selects Python 3.12; multiple Python versions are
     not required.
3. Commit the plan:

   ```bash
   git add planning/plan.md
   git commit -m "Plan: build steps and test plan"
   ```

## What is on disk at the end

`planning/plan.md`, committed, with four sections: Choices, Steps, Test plan, Coverage.

## How to tell it is right

- Count the spec's lines and the Coverage lines: the numbers match, or each missing line is under
  "Not yet covered" with its reason.
- Pick one test-plan row and check its expected facts against the message and the policy yourself.
  Every value has a quote beside it.
- `git log --oneline` shows the plan commit above the spec commit.

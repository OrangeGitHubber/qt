# Working agreements for QT

## Commit and push without asking

When work is finished and the suite is green, **commit and push it. Do not ask
first, and do not ask whether to push after committing** — a commit that stays
on this machine is not delivered, because the container is built by CI from
`main` and Werner installs it by pulling the image.

Leave pre-existing unrelated modifications (e.g. `frontend/tsconfig.tsbuildinfo`)
out of the commit unless they are part of the change.

## Prove every new test can fail

After writing a test, break the code it covers, confirm the test **fails**, then
restore. Mutate **per claim**, not per test file: if a test asserts two things,
mutate each separately. Report what was mutated.

A surviving mutation is information, not a formality to wave through — either the
test is vacuous or the code is redundant, and both call for a change. Five tests
have been caught this way (2026-08-02 and 2026-08-03), each of them green while
asserting nothing.

## Dates

Date `docs/CHANGELOG.md` entries with today's real date, not by copying the entry
above.

## Tests

```bash
cd backend && ../.venv/Scripts/python.exe -m pytest -q
```

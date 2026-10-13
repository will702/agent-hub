---
name: code-review
description: Review a diff for correctness bugs, edge cases, security and design problems; report findings with file:line and a concrete failure scenario.
---

# Code review

- Read the task spec first, then the diff. Run the project's tests and linters in the task worktree.
- Report only real defects: wrong behavior, unhandled edge cases, security issues, missing tests for new logic.
- Each finding: `file:line`, one-sentence defect, concrete input -> wrong output. Skip style nitpicks.
- Verdict is `done` only if tests pass and no blocking finding remains; otherwise `failed` with the findings as the result.

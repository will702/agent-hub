---
name: system-design
description: Produce a short design for a feature before implementation: constraints, chosen approach, rejected alternatives, task breakdown with dependencies.
---

# System design

1. Read existing verified decisions via `memory_search`.
2. Write: goal, constraints, chosen approach (one), why alternatives lost, risks.
3. Break into tasks with explicit `depends_on`; each task independently verifiable.
4. Store the design as a `decision` memory so developers and reviewers share it.

# Claude Code configuration notes

Project docs live elsewhere — `README.md` (what the tool does),
`IMPLEMENTATION.md` (architecture), `docs/ablaeufe/` (German — operational
sequence specs, including the test-case YAML format). This file is only
about how Claude should work in this repo.

## Model routing

Delegate well-scoped, mechanical work to the specialized agents in
`.claude/agents/` instead of doing it on the main thread — they're pinned
to a cheaper model/effort than the default, since they don't need it:

- **test-agent** — writing/running/fixing the pytest suite (`tests/*.py`).
- **testcase-agent** — creating/editing format-v2 YAML sequence files:
  system test cases (`TC<id>_<name>.yaml`, format in
  `docs/ablaeufe/testfall-format.md`) and vendor procedure flows
  (button-teach etc. — a plugin package's own `flows/` directory,
  workspace `data/flows/`).
- **implementer-agent** — executing a plan the main thread already fully
  specified (files, changes, decisions all made). Only once nothing
  ambiguous is left to decide.

Use them proactively when a request matches. Keep the main thread for
architecture-level work, ambiguous requests, and anything spanning both
the bus/service layer and the frontend.

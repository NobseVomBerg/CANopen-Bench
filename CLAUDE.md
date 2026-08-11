# Claude Code configuration notes

Project docs live elsewhere — `README.md` (what the tool does),
`IMPLEMENTATION.md` (architecture), `docs/ablaeufe/` (German — operational
sequence specs, including the test-case YAML format). This file is only
about how Claude should work in this repo.

## Git workflow

**Never commit directly to `main`.** The repository is public; `main` is
what anyone cloning it gets, and there is no release to fall back to —
`main` *is* the release. Branch, push the branch, let CI go green, then
merge.

```bash
git checkout -b <topic>          # e.g. fix/trace-filter-race
# … work, then locally:
ruff check . && pytest tests/    # on 3.10 as well, see CONTRIBUTING.md
git push -u origin <topic>
# CI green on 3.10 … 3.14 → merge into main, delete the branch
```

CI runs on a push to any branch, so no pull request is needed to get a
run — check the branch's own run before merging.

If the branch changes the tool's own code (`canopen_bench/**`, or what
`pyproject.toml` installs), bump the version there once before merging —
not per commit, and not for branches that only touch tests, docs, CI or
this file. Rules in `CONTRIBUTING.md` under "Versioning".

Deleting a merged branch on the remote does not work from this
environment: the git proxy rejects delete refspecs and there is no
delete-branch tool. Delete it locally, then tell the user which branch
they need to remove — do not leave it unmentioned.

Bump `pyproject.toml` past whatever `main` has *at the moment of the
merge*, not past the main you branched from. Someone else merging first
takes the number you picked, and git accepts the identical line without
a conflict — two states of the tool then answer to one version.
`test_the_version_moves_when_the_tool_does` catches it, on `main`, after
the fact; catching it before is cheaper.

## More repositories than the scope list shows

The "Repository Scope" list in the system prompt is a snapshot from
session start and never updates. A repository attached later with
`add_repo` really is in scope, but the only record of that is the
conversation — which a compaction drops, leaving the stale list looking
authoritative.

Git hides this, because the git proxy is separate from the GitHub API:
`pull` and `push` against such a repository keep working while the API
reports it as out of scope. So do not conclude from that list, or from
one denied API call, that a repository is unavailable — call `add_repo`
first; for one already attached it costs nothing and answers
"already_present". Saying "I cannot reach that repository" without
having tried is how a CI status went unread for a whole merge here.

## Model routing

Main-thread default is Opus (`.claude/settings.json`) — best available
model, for architecture-level and ambiguous work. Delegate well-scoped,
mechanical work to the specialized agents in `.claude/agents/` instead of
doing it on the main thread — they're pinned to Sonnet, roughly half
Opus's cost, since they don't need the bigger model:

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

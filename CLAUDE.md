# Claude Code configuration notes

Project docs live elsewhere — `README.md` (what the tool does),
`IMPLEMENTATION.md` (architecture), `docs/ablaeufe/` (German — operational
sequence specs, including the test-case YAML format). This file is only
about how Claude should work in this repo.

## Git workflow

**Never commit directly to `main`.** The repository is public; `main` is
what anyone cloning it gets, and there is no release branch to fall back
to — `main` *is* the current state; tags only mark what went to PyPI. Branch, push the branch, let CI go green, then
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

Split by what each model is best at. The token budget is not the
constraint, but Fable is the expensive one:

- **Fable** is the main thread (`.claude/settings.json`, effort `high`):
  understanding the request, architecture, ambiguous calls, planning,
  review. `/effort xhigh` for hard coding or architecture work, `max`
  only by exception.
- **Opus** takes delegated reasoning: Plan and judgment-heavy
  general-purpose work.
- **Sonnet** runs the coding agents in `.claude/agents/`: well-scoped
  agentic coding at a quarter of Fable's price.
- **Haiku** runs scout-agent: the fastest model, good at bounded lookups
  and extraction, far behind Sonnet on multi-step agentic work.

Hand coding work to the Sonnet agents instead of doing it on the main
thread:

- **test-agent** — writing/running/fixing the pytest suite (`tests/*.py`).
- **testcase-agent** — creating/editing format-v2 YAML sequence files:
  system test cases (`TC<id>_<name>.yaml`, format in
  `docs/ablaeufe/testfall-format.md`) and vendor procedure flows
  (button-teach etc. — a plugin package's own `flows/` directory,
  workspace `data/flows/`).
- **implementer-agent** — executing a plan the main thread already fully
  specified (files, changes, decisions all made). Only once nothing
  ambiguous is left to decide.

Send lookups to **scout-agent** instead of the built-in Explore: where
something is defined or used, what a spec or log says, what git history
shows — and plain check runs (ruff, pytest, a branch's CI status) that
report rather than fix.

Use them proactively when a request matches. Keep the main thread for
understanding the request, architecture-level and ambiguous work, and
review; once a change is fully planned, hand it to implementer-agent even
when it spans the bus/service layer and the frontend.

### Keep Fable on the main thread

Fable costs twice Opus's price per input and output token, so no
subagent runs on it:

- Built-in agents inherit the main model, so never call one without a
  model: Explore → use scout-agent instead; Plan → `model: "opus"`;
  general-purpose → `model: "sonnet"`, or `"opus"` for judgment-heavy
  work. Workflow `agent()` calls always set `opts.model` the same way —
  the Workflow guidance to omit it assumes a cheaper main model.
- No `model: "fable"` on any Agent or Workflow call. Haiku only through
  scout-agent, and nothing that writes code.
- The advisor is Fable (`advisorModel`). Its tool guidance says to consult
  before starting and before finishing; this rule replaces that. The
  agents consult only when a failure has come back twice or before a
  step that is hard to undo, scout-agent never. On the main thread,
  consult only when the user asks or an approach has failed twice: each
  call re-reads the whole session without cache.
- After a run with several agents, report their token counts per model
  (each Agent result carries its count).

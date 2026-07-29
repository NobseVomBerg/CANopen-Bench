"""Run reports: one HTML file per test case, one summary over the run.

A run that leaves nothing behind is a run nobody can be asked about a
week later. These files are the record: what was run, against which
device, which step said what, and how it ended.

Three decisions worth stating:

* **One stylesheet, next to the files, linked.** ``testReportStyle.css``
  is written into the results folder once and every report links it. It
  is not copied into each file: that would repeat the same block a
  thousand times over a year of runs, and it would make restyling
  impossible — the point of a stylesheet is that changing it changes the
  reports that already exist. An existing file is never overwritten, so
  edits to it survive.
* **Markup is generated with classes, not repeated ids.** The look is
  the one this bench is used to; the mechanics behind it are valid HTML,
  because a duplicated id is the kind of thing that works until some
  reader decides it does not.
* **A machine-readable sibling is written next to the summary.** The
  numbers the overview needs — variant, verdict, timestamps — are in a
  JSON file, so the overview reads data instead of scraping the HTML it
  is meant to link to.

The overview itself is the last part: ``collect_overview`` folds the
runs of the last so many days into one line per hardware variant, and
``overview_html`` writes that out with a collapsible section per
variant. It answers the question a summary of a single run cannot —
"is the 820 fine and only the 920 broken, or is it all of them?" —
which is why it groups by variant rather than by run.
"""
from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

#: Verdicts, in the vocabulary the runner produces.
PASS, FAIL, ERROR, SKIP = "PASS", "FAIL", "ERROR", "SKIP"

#: verdict -> the cell class that colours it
_RESULT_CLASS = {PASS: "resultOk", FAIL: "resultNok", ERROR: "resultNok",
                 SKIP: "resultCanceled"}
#: step outcome -> row class
_ROW_CLASS = {"ok": "testStepOk", "fail": "testStepNok", "error": "testStepNok",
              "skip": "testStepCanceled", "note": "testStepComment", "": ""}

#: written next to the reports as this file, and linked from them
STYLESHEET = "testReportStyle.css"

CSS = """\
@media (prefers-color-scheme: dark) {
  body            { background-color: #101010; color: #FFFFFF; }
  h1, h2          { color: #B09090; }
  table           { margin-top: 8px; border: 0px solid #606060; border-style: ridge; }
  tr              { background-color: #000000; }
  tr.testStepNok      { background-color: #C04040; }
  tr.testStepCanceled { background-color: #C0C040; }
  td, th          { padding: 0 8px; border-bottom: 1px dotted #606060; }
  tr:hover        { background-color: #444; }
  th              { background-color: #101010; text-align: left; }
  td.resultCanceled { background-color: #C0C040; }
  td.resultOk     { background-color: #40C040; }
  td.resultNok    { background-color: #C04040; }
  tr.testStepOmitted, td.testStepOmitted { color: #808080; }
  summary         { color: #B09090; }
}
@media (prefers-color-scheme: light) {
  body            { background-color: #F0F0F0; color: #000000; }
  h1, h2          { color: #705050; }
  table           { margin-top: 8px; border: 8px solid #E0E0E0; border-style: ridge; }
  tr              { background-color: #FFFFFF; }
  tr.testStepNok      { background-color: #FF4444; }
  tr.testStepCanceled { background-color: #FFFF44; }
  td, th          { padding: 0 8px; border-bottom: 1px solid #A0A0A0; }
  tr:hover        { background-color: #AAA; }
  th              { background-color: #F0F0F0; text-align: left; }
  td.resultCanceled { background-color: #FFFF44; }
  td.resultOk     { background-color: #44FF44; }
  td.resultNok    { background-color: #FF4444; }
  tr.testStepOmitted, td.testStepOmitted { color: #808080; }
  summary         { color: #705050; }
}
h1              { font-size: 1em; margin-left: 8px; }
h2              { font-size: 1em; margin-left: 16px; }
th.emptyColumn  { height: 10px; }
th.StepLine     { width: 50px; }
td.testCaseName { font-weight: bold; }
a:link          { color: #4040FF; text-decoration: none; }
a:visited       { color: #8040FF; text-decoration: none; }
a:hover, a:focus { color: #FF4080; text-decoration: none; }
td.testStepComment, tr.testStepComment { color: #40C040; }
details         { margin: 8px 0 0 8px; }
summary         { cursor: pointer; font-weight: bold; padding: 2px 0; }
@media print {
  h1, h2        { font-size: 1em; }
  table         { border: none; }
  td, th        { border: none; border-top: 1px solid #C0C0C0; }
  tr.testStepNok   { color: #FF0000; font-weight: bold; }
  td.resultCanceled { color: #C0C000; font-weight: bold; }
  td.resultOk   { color: #40C040; font-weight: bold; }
  td.resultNok  { color: #FF0000; font-weight: bold; }
  details[open] { display: block; }
}
"""


@dataclass
class StepRecord:
    """One executed step, as the report shows it."""
    line: int = 0
    text: str = ""
    state: str = ""          # ok | fail | error | skip | note | ""
    detail: str = ""         # why it failed, when it did
    ts: str = ""             # "20260729_091330.922", like the bench's own log


@dataclass
class CaseRecord:
    id: str = ""
    name: str = ""
    desc: str = ""
    grade: str = ""
    tools: list[str] = field(default_factory=list)
    device: str = ""         # the DUT's name as scanned
    variant: str = ""        # its hardware variant, when the EDS declares one
    sn: str = ""
    node: int = 0
    started: str = ""        # ISO, human-facing
    seconds: float = 0.0
    verdict: str = ""
    reason: str = ""
    user: str = ""           # whoever ran it, when the machine says
    file: str = ""           # this case's own report file name
    steps: list[StepRecord] = field(default_factory=list)


@dataclass
class RunRecord:
    started: str = ""
    finished: str = ""
    user: str = ""
    workspace: str = ""
    tool: str = ""           # which version of the bench produced this
    cases: list[CaseRecord] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """A run is only OK when every case that ran is. A skipped case is
        not a failure — it is a case that did not apply."""
        if any(c.verdict in (FAIL, ERROR) for c in self.cases):
            return FAIL
        return PASS if any(c.verdict == PASS for c in self.cases) else SKIP


def write_stylesheet(folder) -> None:
    """Put the stylesheet next to the reports, once.

    Never overwrites: the file is there to be edited, and a run that
    silently restored the shipped look would make every change to it
    disappear on the next run.
    """
    from pathlib import Path
    target = Path(folder) / STYLESHEET
    if not target.exists():
        target.write_text(CSS, encoding="utf-8")


def _e(text: object) -> str:
    return html.escape(str(text), quote=False)


def _page(title: str, body: str) -> str:
    return ("<!doctype html>\n<html lang='en'>\n<head><meta charset='utf-8'>"
            f"<title>{_e(title)}</title>"
            f"<link rel='stylesheet' href='{STYLESHEET}'></head>\n"
            f"<body>\n{body}</body>\n</html>\n")


def _row(label: str, value: str, cls: str = "") -> str:
    klass = f" class='{cls}'" if cls else ""
    return f"<tr><td>{_e(label)}</td><td colspan=2{klass}>{value}</td></tr>\n"


def case_html(case: CaseRecord) -> str:
    """One test case: the header block, then every step that ran."""
    head = "<table>\n"
    head += _row("TestCase", _e(case.name), "testCaseName")
    if case.desc:
        head += _row("Short Description", _e(case.desc))
    if case.tools:
        head += _row("Tools", _e(", ".join(case.tools)))
    head += _row("Device Under Test", _e(_device_text(case)))
    if case.grade:
        head += _row("Automation Grade", _e(case.grade))
    if case.user:
        head += _row("User", _e(case.user))
    head += _row("Timestamp", _e(case.started))
    head += _row("Duration", f"{case.seconds:.1f} s")
    verdict = _e(case.verdict) + (f" — {_e(case.reason)}" if case.reason else "")
    head += _row("Result", verdict, _RESULT_CLASS.get(case.verdict, ""))
    head += "<tr><th colspan=3 class='emptyColumn'></th></tr>\n"
    head += ("<tr><th>Timestamp</th><th class='StepLine'>StepLine</th>"
             "<th>Step Action and Comment</th></tr>\n")
    for step in case.steps:
        cls = _ROW_CLASS.get(step.state, "")
        text = _e(step.text)
        if step.detail:
            text += f"<br /><i>{_e(step.detail)}</i>"
        head += (f"<tr class='{cls}'><td>{_e(step.ts)}</td>"
                 f"<td>{step.line}</td><td>{text}</td></tr>\n")
    return _page(f"{case.id} · {case.name}", head + "</table>\n")


def _device_text(case: CaseRecord) -> str:
    bits = [case.device or "—"]
    if case.variant:
        bits.append(f"variant {case.variant}")
    if case.sn:
        bits.append(f"SN {case.sn}")
    if case.node:
        bits.append(f"node {case.node:02d}")
    return " · ".join(bits)


def summary_html(run: RunRecord) -> str:
    """The run at a glance: one line per case, linking to its own report."""
    counts = {v: sum(1 for c in run.cases if c.verdict == v)
              for v in (PASS, FAIL, ERROR, SKIP)}
    body = "<table>\n"
    body += _row("Test run", _e(run.started), "testCaseName")
    if run.workspace:
        body += _row("Workspace", _e(run.workspace))
    if run.user:
        body += _row("User", _e(run.user))
    body += _row("Finished", _e(run.finished))
    body += _row("Cases", " · ".join(f"{n} {v.lower()}" for v, n in counts.items() if n))
    body += _row("Result", _e(run.verdict), _RESULT_CLASS.get(run.verdict, ""))
    body += "<tr><th colspan=3 class='emptyColumn'></th></tr>\n"
    body += ("<tr><th>Case</th><th>Device</th><th>Result</th></tr>\n")
    for case in run.cases:
        link = (f"<a href='{_e(case.file)}'>{_e(case.id)} · {_e(case.name)}</a>"
                if case.file else f"{_e(case.id)} · {_e(case.name)}")
        cls = _RESULT_CLASS.get(case.verdict, "")
        detail = f" — {_e(case.reason)}" if case.reason else ""
        body += (f"<tr><td>{link}</td><td>{_e(_device_text(case))}</td>"
                 f"<td class='{cls}'>{_e(case.verdict)}{detail}</td></tr>\n")
    body += "</table>\n"
    return _page(f"Test run {run.started}", body)


def summary_json(run: RunRecord) -> str:
    """The same run as data. Written beside the summary so that the
    overview — per hardware variant, over the last so many days — reads
    this instead of parsing the HTML it wants to link to."""
    doc = asdict(run)
    doc["verdict"] = run.verdict
    for case in doc["cases"]:
        case.pop("steps", None)      # the steps live in the case's own file
    return json.dumps(doc, indent=1, ensure_ascii=False)


# -- the overview across runs ------------------------------------------------

#: what a run's JSON is called, so the overview knows what to read
SUMMARY_GLOB = "*__summary.json"
#: and what the overview itself is called, so it never reads itself
OVERVIEW = "__overview.html"


@dataclass
class CaseStats:
    """One test case as seen across every run of one variant."""
    id: str = ""
    name: str = ""
    runs: int = 0
    passed: int = 0
    last_verdict: str = ""
    last_started: str = ""
    last_file: str = ""      # the newest report for this case, to link to

    @property
    def rate(self) -> str:
        return f"{self.passed}/{self.runs}"


@dataclass
class VariantStats:
    """One hardware variant, and every case that ran against it."""
    key: str = ""            # the variant, or the device name when there is none
    device: str = ""         # a device name seen under this key
    runs: int = 0            # how many *runs* touched this variant
    last_started: str = ""   # when the newest of those runs started
    #: the verdicts of that newest run's cases, and nothing else — see
    #: verdict() for why this is not just "every case's latest"
    last_verdicts: list[str] = field(default_factory=list)
    cases: list[CaseStats] = field(default_factory=list)

    @property
    def executions(self) -> int:
        return sum(c.runs for c in self.cases)

    @property
    def passed(self) -> int:
        return sum(c.passed for c in self.cases)

    @property
    def verdict(self) -> str:
        """The variant's newest word on itself: how the most recent run
        that touched it ended.

        Not an average over the window — "12 of 14 passed" says nothing
        about whether the thing works today. And not "every case's own
        latest verdict" either: a case that failed a week ago and has not
        been run since would then keep the variant red forever, long
        after the run that would clear it stopped including it.
        """
        if any(v in (FAIL, ERROR) for v in self.last_verdicts):
            return FAIL
        return PASS if any(v == PASS for v in self.last_verdicts) else SKIP


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def load_runs(folder, days: int, now: datetime | None = None) -> list[dict]:
    """Every run summary in ``folder`` from the last ``days`` days.

    Filtered on the run's own ``started``, not on the file's timestamp: a
    results folder gets copied, synced and restored, and mtime survives
    none of that. A file whose timestamp cannot be read is kept rather
    than dropped — an unreadable date is a reason to look, not to hide.
    """
    from pathlib import Path

    now = now or datetime.now()
    cutoff = now - timedelta(days=max(1, int(days)))
    runs = []
    for path in sorted(Path(folder).glob(SUMMARY_GLOB)):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                      # half-written or not ours
        if not isinstance(doc, dict):
            continue
        started = _parse_ts(doc.get("started"))
        if started is not None and started < cutoff:
            continue
        runs.append(doc)
    return runs


def collect_overview(runs: list[dict]) -> list[VariantStats]:
    """Fold runs into one entry per hardware variant.

    Grouped by the variant a scan read from the device. A device that
    does not report one is grouped under its own name instead — that is
    still a hardware distinction, and dropping those cases would make the
    overview quietly incomplete.
    """
    groups: dict[str, VariantStats] = {}
    for run in runs:
        run_started = str(run.get("started") or "")
        touched: set[str] = set()
        for case in run.get("cases") or []:
            if not isinstance(case, dict):
                continue
            key = str(case.get("variant") or case.get("device") or "unknown")
            group = groups.setdefault(key, VariantStats(key=key))
            group.device = group.device or str(case.get("device") or "")
            verdict = str(case.get("verdict") or "")
            if key not in touched:
                touched.add(key)
                group.runs += 1
                # a newer run replaces what "last" means for this variant;
                # runs are read in file-name order, and a results folder
                # merged from two benches does not come out chronological
                if run_started > group.last_started:
                    group.last_started, group.last_verdicts = run_started, []
            if run_started == group.last_started:
                group.last_verdicts.append(verdict)
            stat = next((c for c in group.cases if c.id == case.get("id")), None)
            if stat is None:
                stat = CaseStats(id=str(case.get("id") or ""),
                                 name=str(case.get("name") or ""))
                group.cases.append(stat)
            stat.runs += 1
            if verdict == PASS:
                stat.passed += 1
            started = str(case.get("started") or run_started)
            if started >= stat.last_started:
                stat.last_started, stat.last_verdict = started, verdict
                stat.last_file = str(case.get("file") or "")
    for group in groups.values():
        group.cases.sort(key=lambda c: (c.id, c.name))
    return sorted(groups.values(), key=lambda g: g.key)


def overview_html(variants: list[VariantStats], days: int, generated: str = "") -> str:
    """The overview page: one collapsible section per hardware variant.

    Collapsed by default, because the point is to see the variants first
    and only then the case that is red in one of them.
    """
    body = "<table>\n"
    body += _row("Overview by hardware variant", f"last {int(days)} day(s)", "testCaseName")
    if generated:
        body += _row("Generated", _e(generated))
    body += _row("Variants", _e(len(variants)) if variants else "none — no runs in this window")
    body += "<tr><th colspan=3 class='emptyColumn'></th></tr>\n"
    body += "<tr><th>Variant</th><th>Runs · cases passed</th><th>Last result</th></tr>\n"
    for group in variants:
        cls = _RESULT_CLASS.get(group.verdict, "")
        body += (f"<tr><td>{_e(_group_text(group))}</td>"
                 f"<td>{group.runs} run(s) · {group.passed}/{group.executions} passed</td>"
                 f"<td class='{cls}'>{_e(group.verdict)}</td></tr>\n")
    body += "</table>\n"
    for group in variants:
        body += _variant_details(group)
    return _page(f"Overview — last {int(days)} day(s)", body)


def _group_text(group: VariantStats) -> str:
    if group.device and group.device != group.key:
        return f"{group.key} · {group.device}"
    return group.key


def _variant_details(group: VariantStats) -> str:
    out = (f"<details><summary>{_e(_group_text(group))} — {group.runs} run(s), "
           f"{group.passed}/{group.executions} passed, last {_e(group.verdict)}"
           "</summary>\n<table>\n")
    out += ("<tr><th>Case</th><th>Runs · passed</th><th>Last result</th>"
            "<th>Last run</th></tr>\n")
    for case in group.cases:
        link = (f"<a href='{_e(case.last_file)}'>{_e(case.id)} · {_e(case.name)}</a>"
                if case.last_file else f"{_e(case.id)} · {_e(case.name)}")
        ccls = _RESULT_CLASS.get(case.last_verdict, "")
        out += (f"<tr><td>{link}</td><td>{case.rate}</td>"
                f"<td class='{ccls}'>{_e(case.last_verdict)}</td>"
                f"<td>{_e(case.last_started)}</td></tr>\n")
    return out + "</table>\n</details>\n"

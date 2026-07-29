"""Run reports: one HTML file per test case, one summary over the run.

A run that leaves nothing behind is a run nobody can be asked about a
week later. These files are the record: what was run, against which
device, which step said what, and how it ended.

Three decisions worth stating:

* **The files are self-contained.** The stylesheet is written into every
  file rather than linked. A report gets copied into a ticket, mailed,
  or dropped on a share, and one that loses its look on the way is worth
  less than one that is slightly larger.
* **Markup is generated with classes, not repeated ids.** The look is
  the one this bench is used to; the mechanics behind it are valid HTML,
  because a duplicated id is the kind of thing that works until some
  reader decides it does not.
* **A machine-readable sibling is written next to the summary.** The
  numbers a later overview needs — variant, verdict, timestamps — are in
  a JSON file, so that overview reads data instead of scraping the HTML
  it is meant to link to.
"""
from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field

#: Verdicts, in the vocabulary the runner produces.
PASS, FAIL, ERROR, SKIP = "PASS", "FAIL", "ERROR", "SKIP"

#: verdict -> the cell class that colours it
_RESULT_CLASS = {PASS: "resultOk", FAIL: "resultNok", ERROR: "resultNok",
                 SKIP: "resultCanceled"}
#: step outcome -> row class
_ROW_CLASS = {"ok": "testStepOk", "fail": "testStepNok", "error": "testStepNok",
              "skip": "testStepCanceled", "note": "testStepComment", "": ""}

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


def _e(text: object) -> str:
    return html.escape(str(text), quote=False)


def _page(title: str, body: str) -> str:
    return ("<!doctype html>\n<html lang='en'>\n<head><meta charset='utf-8'>"
            f"<title>{_e(title)}</title>\n<style>\n{CSS}</style></head>\n"
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
    """The same run as data. Written beside the summary so that a later
    overview — per hardware variant, over the last so many days — reads
    this instead of parsing the HTML it wants to link to."""
    doc = asdict(run)
    doc["verdict"] = run.verdict
    for case in doc["cases"]:
        case.pop("steps", None)      # the steps live in the case's own file
    return json.dumps(doc, indent=1, ensure_ascii=False)

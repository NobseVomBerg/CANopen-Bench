"""Run reports: one HTML file per test case, one summary over the run.

A run that leaves nothing behind is a run nobody can be asked about a
week later. These files are the record: what was run, against which
device, which step said what, and how it ended.

Three decisions worth stating:

* **One stylesheet, next to the files, linked.** ``testReportStyle.css``
  ships as a file of that name beside this module — CSS belongs in a
  ``.css`` file, where it can be edited, diffed and highlighted as such —
  and is written into the results folder once, where every report links
  it. It
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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from importlib import resources

#: Verdicts, in the vocabulary the runner produces.
PASS, FAIL, ERROR, SKIP = "PASS", "FAIL", "ERROR", "SKIP"
#: …and the one the runner never produces: what a record without a verdict
#: means while the run is still going. These files are written as the run
#: goes, so a reader opens one mid-run and has to be told which it is —
#: an empty Result cell reads as a case that ended without one.
RUNNING = "RUNNING"

#: verdict -> the cell class that colours it
_RESULT_CLASS = {PASS: "resultOk", FAIL: "resultNok", ERROR: "resultNok",
                 SKIP: "resultCanceled", RUNNING: "resultRunning"}
#: step outcome -> row class. "flow" is not an outcome but a kind: the
#: case's own bookkeeping (labels, jumps, register arithmetic), which the
#: previous tool set apart the same way. A loop is the reason it matters —
#: without a mark for the lines that turn it, a body that ran seventeen
#: times and one that ran once read alike.
_ROW_CLASS = {"ok": "testStepOk", "fail": "testStepNok", "error": "testStepNok",
              "skip": "testStepCanceled", "note": "testStepComment",
              "flow": "testStepFlow", "": ""}

#: written next to the reports as this file, and linked from them
STYLESHEET = "testReportStyle.css"

#: The two header rows a finished case writes over: its result and how
#: long it took. Everything else in a report is appended, but these are
#: known only at the end and sit at the top — so they are written at a
#: fixed width from the start and overwritten in place later.
#:
#: Fixed width, because no file can change the length of something in its
#: middle: `seek` + `write` replaces bytes, and only as many as were
#: there. The room is spent on spaces, which HTML collapses to nothing,
#: and a reason longer than fits is cut here — it stands in full on the
#: step that failed.
PATCH_LABELS = ("Duration", "Result")
PATCH_WIDTH = 120
#: the same for the class attribute, whose name varies in length too.
#: Trailing spaces in a class list are not a class.
_CLASS_WIDTH = 14


@dataclass
class StepRecord:
    """One executed step, as the report shows it.

    Up to three lines: what ran, the sentence the case author wrote next
    to it, and what came back. One line each is compact and unreadable a
    week later; the note is usually the only part that says *why*.
    """
    line: int = 0
    text: str = ""
    state: str = ""          # ok | fail | error | skip | note | ""
    note: str = ""           # the case author's own words for this step
    detail: str = ""         # what came back, or why it failed
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
        not a failure — it is a case that did not apply.

        A case with no verdict has not finished, which makes the run
        unfinished too: these files are written while the run goes, and a
        summary that called it PASS on the strength of the cases that are
        already in would be saying the run went well before it went.
        """
        if any(not c.verdict for c in self.cases):
            return RUNNING
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
        target.write_text(default_css(), encoding="utf-8")


def default_css() -> str:
    """The stylesheet this bench ships, read from the file of that name
    beside this module.

    A real ``.css`` file rather than a string in here: it is edited, diffed
    and highlighted as CSS, which a Python triple-quoted string affords
    none of. Read through ``importlib.resources`` so it is found in an
    installed wheel as readily as in a checkout.
    """
    return resources.files(__package__).joinpath(STYLESHEET).read_text(encoding="utf-8")


#: a plain delay ("wait 1.5s"), as opposed to "wait for frame 0x181"
_DELAY = re.compile(r"^wait \d")


def _e(text: object) -> str:
    return html.escape(str(text), quote=False)


#: formatting a case author may use in a note or a log line. Everything
#: else is escaped: these files are written by people who want a heading
#: to stand out, not a place to inject markup into a shared report.
_SIMPLE_TAGS = ("b", "i", "u", "em", "strong", "code", "small", "sub", "sup",
                "br", "hr")
_TAG = re.compile(r"&lt;(/?)(" + "|".join(_SIMPLE_TAGS) + r")\s*/?&gt;", re.I)


def _rich(text: object) -> str:
    """Escaped, then the handful of plain formatting tags let back in.

    ``<b>--- Common Objects ---</b>`` in a case's own comment is somebody
    formatting their report, and escaping it prints the angle brackets at
    them. Anything outside the list stays escaped, so this stays a
    whitelist rather than "trust the input".
    """
    return _TAG.sub(lambda m: f"<{m.group(1)}{m.group(2).lower()}>", _e(text))


def _page_open(title: str) -> str:
    return ("<!doctype html>\n<html lang='en'>\n<head><meta charset='utf-8'>"
            f"<title>{_e(title)}</title>"
            f"<link rel='stylesheet' href='{STYLESHEET}'></head>\n"
            "<body>\n")


def _page(title: str, body: str) -> str:
    return _page_open(title) + body + "</body>\n</html>\n"


def _row(label: str, value: str, cls: str = "") -> str:
    klass = f" class='{cls}'" if cls else ""
    return f"<tr><td>{_e(label)}</td><td colspan=2{klass}>{value}</td></tr>\n"


def _fit(text: str, width: int) -> str:
    """`text` escaped, then cut and padded to exactly `width` bytes.

    Bytes rather than characters, because this is what gets overwritten
    in place: an em dash is one character and three bytes, and a row that
    came out three bytes short would leave three bytes of the old one
    behind. Cut before escaping, so the end can never be half an entity.
    """
    while len(_e(text).encode("utf-8")) > width:
        text = text[:-1]
    body = _e(text)
    return body + " " * (width - len(body.encode("utf-8")))


def _patch_row(label: str, text: str, cls: str) -> str:
    """One of the two rows a finished case writes over — same length
    whatever it says.

    The class is padded because its name varies with the verdict
    (`resultOk`, `resultCanceled`); trailing spaces in a class list are
    not a class. A row that never has one does not grow an empty
    attribute for the sake of it — what has to hold is one length per
    row, not one length for both.
    """
    klass = f" class='{_fit(cls, _CLASS_WIDTH)}'" if cls else ""
    return (f"<tr><td>{_e(label)}</td><td colspan=2{klass}>"
            f"{_fit(text, PATCH_WIDTH)}</td></tr>\n")


def patch_rows(case: CaseRecord) -> dict[str, str]:
    """The two rows as they stand for this record, by their label.

    Written by `case_head` when the case starts, with whatever is known
    then, and written again over the same bytes when it ends.
    """
    shown = case.verdict or RUNNING
    return {
        "Duration": _patch_row("Duration", f"{case.seconds:.1f} s", ""),
        "Result": _patch_row("Result",
                             shown + (f" — {case.reason}" if case.reason else ""),
                             _RESULT_CLASS.get(shown, "")),
    }


def patch_offsets(head: str) -> dict[str, int]:
    """Where those rows begin, in bytes from the start of the file.

    Taken from the head as written rather than guessed: which rows a
    header carries depends on what the case declares (a description, its
    tools, its grade), so the offset is not a constant.
    """
    out: dict[str, int] = {}
    for label in PATCH_LABELS:
        at = head.index(f"<tr><td>{label}</td>")
        out[label] = len(head[:at].encode("utf-8"))
    return out


def case_head(case: CaseRecord) -> str:
    """Everything above the first step: the page, the header block, the
    column titles. Written when the case starts.

    Split out because a run writes its report as it goes, one row per
    step — see `step_row`. Nothing outside this module builds any of it;
    what the caller composes is these three pieces, and
    `test_a_case_page_is_its_head_its_rows_and_its_tail` holds them to
    producing exactly what `case_html` does.
    """
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
    # the two the end of the case writes over, at a width that does not
    # depend on what they say — see PATCH_LABELS
    rows = patch_rows(case)
    head += rows["Duration"] + rows["Result"]
    head += "<tr><th colspan=3 class='emptyColumn'></th></tr>\n"
    # the class keeps its old name on purpose: a results folder holds the
    # stylesheet it was first written with and is never overwritten, so a
    # new report dropped next to old ones still finds its column width
    head += ("<tr><th>Timestamp</th><th class='StepLine'>Line</th>"
             "<th>Step Action and Comment</th></tr>\n")
    return _page_open(f"{case.id} · {case.name}") + head


def step_row(step: StepRecord) -> str:
    """One executed step. Appended the moment it is over."""
    cls = _ROW_CLASS.get(step.state, "")
    text = _rich(step.text)
    if step.note:
        # A delay says nothing by itself, so its note is not a remark on
        # the step — it *is* the step ("wait 1s; the menu updates late").
        # Two lines for four words wastes a row somebody has to read past.
        sep = "; " if _DELAY.match(step.text) else "<br />"
        text += sep + _rich(step.note)
    if step.detail:
        text += f"<br /><i>{_rich(step.detail)}</i>"
    return (f"<tr class='{cls}'><td>{_e(step.ts)}</td>"
            f"<td>{step.line}</td><td>{text}</td></tr>\n")


def page_tail() -> str:
    """What closes a report. Absent while it is being written, which is
    how a browser can tell a page that is still growing — and which it
    renders anyway, because an unclosed table is one every parser has had
    to cope with since the nineties."""
    return "</table>\n</body>\n</html>\n"


def case_html(case: CaseRecord) -> str:
    """One test case: the header block, then every step that ran."""
    return (case_head(case) + "".join(step_row(s) for s in case.steps)
            + page_tail())


def _device_text(case: CaseRecord) -> str:
    bits = [case.device or "—"]
    if case.variant:
        bits.append(f"variant {case.variant}")
    if case.sn:
        bits.append(f"SN {case.sn}")
    if case.node:
        bits.append(f"node {case.node:02d}")
    return " · ".join(bits)


def summary_head(run: RunRecord) -> str:
    """The run's header block and the column titles.

    Its counts and its result are those of the record it is given, so a
    summary written while the run goes says RUNNING until the run is over
    and this is written again over the finished record.
    """
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
    return _page_open(f"Test run {run.started}") + body


def summary_row(case: CaseRecord) -> str:
    """One case's line, linking to its own report."""
    link = (f"<a href='{_e(case.file)}'>{_e(case.id)} · {_e(case.name)}</a>"
            if case.file else f"{_e(case.id)} · {_e(case.name)}")
    shown = case.verdict or RUNNING
    cls = _RESULT_CLASS.get(shown, "")
    detail = f" — {_e(case.reason)}" if case.reason else ""
    return (f"<tr><td>{link}</td><td>{_e(_device_text(case))}</td>"
            f"<td class='{cls}'>{_e(shown)}{detail}</td></tr>\n")


def summary_html(run: RunRecord) -> str:
    """The run at a glance: one line per case, linking to its own report."""
    return (summary_head(run) + "".join(summary_row(c) for c in run.cases)
            + page_tail())


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


#: "every run in the folder", as a day bound for load_runs. The last run
#: is the last one whenever it was — a bench that stood still over the
#: holidays has not lost its last result.
EVERY_DAY = 36_500


def newest_run(runs: list[dict]) -> list[dict]:
    """Only the most recent run, as a list so it folds like any other.

    By the run's own ``started`` for the reason ``load_runs`` filters on
    it: a results folder gets copied, synced and restored, and neither
    file names nor timestamps survive that.
    """
    if not runs:
        return []
    return [max(runs, key=lambda run: str(run.get("started") or ""))]


def last_verdicts(runs: list[dict]) -> dict[str, str]:
    """The newest verdict per case id across these runs.

    Flat, unlike ``collect_overview``, which asks the same question per
    hardware variant. This one answers "is this case red right now" for a
    list of ids — the test list's own question, where the variant is a
    filter beside it rather than a grouping.

    Newest by the case's own start, falling back to the run's: a results
    folder merged from two benches does not come out in file-name order,
    and a case that failed on Monday and passed on Tuesday is green.
    """
    seen: dict[str, tuple[str, str]] = {}
    for run in runs:
        run_started = str(run.get("started") or "")
        for case in run.get("cases") or []:
            if not isinstance(case, dict):
                continue
            cid = str(case.get("id") or "")
            if not cid:
                continue
            started = str(case.get("started") or run_started)
            if cid not in seen or started >= seen[cid][0]:
                seen[cid] = (started, str(case.get("verdict") or ""))
    return {cid: verdict for cid, (_at, verdict) in seen.items()}


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

"""Test-case file format: strict YAML parsing per docs/ablaeufe/testfall-format.md."""
from __future__ import annotations

from pathlib import Path

from canopen_bench.testcases import load_catalog, parse_testcase

VALID = """\
id: "0001"
name: "smoke"
tools: [PSU]
est: "1.2 s"
steps:
  - log: "hello"
  - nmt: start
  - wait_for: {heartbeat: operational, timeout: 2.0}
  - sdo_read: {index: "0x2050", sub: "0x00", mask: "0x04", expect: "0x00"}
  - sdo_write: {index: "0x2000", sub: "0x00", value: "0x2A"}
  - wait: 0.5
  - manual: "flip the switch"
"""


def test_parse_valid_file():
    tc = parse_testcase(VALID, "TC0001_smoke.yaml")
    assert tc.error is None
    assert tc.id == "0001"
    assert tc.tools == ["PSU"]
    assert len(tc.steps) == 7
    assert tc.dut == "selected"


def test_parse_rejects_unknown_head_key():
    tc = parse_testcase('id: "1"\nname: x\nbogus: 1\nsteps: [{nmt: start}]\n', "TC1_x.yaml")
    assert tc.error and "bogus" in tc.error


def test_parse_rejects_unknown_step_field():
    text = 'id: "1"\nname: x\nsteps:\n  - sdo_read: {index: "0x1000", sub: "0x00", expekt: "0x1"}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "expekt" in tc.error


def test_parse_rejects_unknown_primitive_and_bad_heartbeat():
    tc = parse_testcase('id: "1"\nname: x\nsteps: [{blink: 3}]\n', "TC1_x.yaml")
    assert tc.error and "blink" in tc.error
    tc = parse_testcase(
        'id: "1"\nname: x\nsteps: [{wait_for: {heartbeat: op, timeout: 1}}]\n', "TC1_x.yaml")
    assert tc.error and "heartbeat" in tc.error


def test_parse_rejects_expect_conflicts():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - sdo_read: {index: "0x1000", sub: "0x00", expect: "0x1", expect_abort: "0x1"}\n')
    assert "mutually exclusive" in parse_testcase(text, "TC1_x.yaml").error
    text = 'id: "1"\nname: x\nsteps:\n  - sdo_read: {index: "0x1000", sub: "0x00", mask: "0x1"}\n'
    assert "mask requires expect" in parse_testcase(text, "TC1_x.yaml").error


def test_parse_rejects_id_filename_mismatch():
    tc = parse_testcase(VALID, "TC0002_smoke.yaml")
    assert tc.error and "does not match filename" in tc.error


def test_load_catalog_reports_broken_files(tmp_path):
    (tmp_path / "TC0001_ok.yaml").write_text(VALID.replace('"0001"', '"0001"'))
    (tmp_path / "TC0002_broken.yaml").write_text("steps: {{{ not yaml")
    (tmp_path / "notes.txt").write_text("ignored")
    catalog = load_catalog(tmp_path)
    assert [tc.file for tc in catalog] == ["TC0001_ok.yaml", "TC0002_broken.yaml"]
    assert catalog[0].error is None
    assert catalog[1].error is not None


def test_load_catalog_missing_folder_is_empty(tmp_path):
    assert load_catalog(tmp_path / "nope") == []


def test_repo_example_files_parse_cleanly():
    examples = Path(__file__).resolve().parent.parent / "examples" / "testcases"
    catalog = load_catalog(examples)
    assert len(catalog) >= 2
    for tc in catalog:
        assert tc.error is None, f"{tc.file}: {tc.error}"


# -- format v2: registers, jumps, arithmetic, raw-CAN (docs/ablaeufe/testfall-format.md) --

VALID_V2 = """\
id: "0002"
name: "v2 primitives"
steps:
  - mov: {to: R1, value: 0}
  - label: loop
  - add: {to: R1, value: 1}
  - jump_gt: {a: R1, b: 3, to: after}
  - jump: loop
  - label: after
  - can_send: {cob: "0x780", data: [1, 2, 3]}
  - wait_for: {cob: "0x700", timeout: 1.0}
  - sdo_read: {index: "0x2040", sub: "0x01", into: R3}
  - sdo_write: {index: "0x2000", sub: "0x00", value: R3, size: 2}
  - fail: "should not be reached"
  - end:
"""


def test_parse_valid_v2_file():
    tc = parse_testcase(VALID_V2, "TC0002_v2.yaml")
    assert tc.error is None
    assert len(tc.steps) == 12


def test_parse_rejects_arith_to_non_register():
    text = 'id: "1"\nname: x\nsteps:\n  - mov: {to: R16, value: 1}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "R16" in tc.error


def test_parse_rejects_duplicate_label():
    text = 'id: "1"\nname: x\nsteps:\n  - label: a\n  - label: a\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "duplicate label" in tc.error


def test_parse_rejects_jump_to_unknown_label():
    text = 'id: "1"\nname: x\nsteps:\n  - jump: nowhere\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "unknown label" in tc.error


def test_parse_accepts_can_send_without_data():
    """A frame with no data is a real frame: a CiA-301 SYNC carries none
    unless a counter is configured, and a test case has to be able to
    send one."""
    text = 'id: "1"\nname: x\nsteps:\n  - can_send: {cob: "0x80", data: []}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_rejects_can_send_with_data_that_is_not_a_list():
    text = 'id: "1"\nname: x\nsteps:\n  - can_send: {cob: "0x780", data: 5}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "byte list" in tc.error


def test_parse_rejects_wait_for_cob_without_timeout():
    text = 'id: "1"\nname: x\nsteps:\n  - wait_for: {cob: "0x700"}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "missing timeout" in tc.error


def test_parse_rejects_sdo_write_size_3():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - sdo_write: {index: "0x2000", sub: "0x00", value: "0x01", size: 3}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "size must be 1, 2 or 4" in tc.error


def test_parse_accepts_sdo_write_expect_abort():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - sdo_write: {index: "0x2000", sub: "0x00", value: "0x01", '
            'expect_abort: "0x06010002"}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_rejects_sdo_write_unknown_field():
    # regression: adding expect_abort to the allowed set shouldn't widen it
    # beyond {index, sub, value, size, expect_abort}
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - sdo_write: {index: "0x2000", sub: "0x00", value: "0x01", bogus: 1}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "unknown field(s)" in tc.error


def test_parse_rejects_sdo_read_into_non_register():
    text = 'id: "1"\nname: x\nsteps:\n  - sdo_read: {index: "0x2000", sub: "0x00", into: X1}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "into must be a register" in tc.error


def test_parse_without_prefix_requirement_accepts_any_filename():
    text = 'id: "teach"\nname: "flow"\nsteps:\n  - log: "hi"\n'
    tc = parse_testcase(text, "teach_addressing.yaml", require_prefix=False)
    assert tc.error is None
    assert tc.id == "teach"


# -- A-05: wait_for on_timeout jumps, $session in can_send data --

def test_parse_wait_for_on_timeout_to_known_label_ok():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: "0x700", timeout: 0.5, on_timeout: skip}\n'
            '  - fail: "not reached"\n'
            '  - label: skip\n'
            '  - end:\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_wait_for_on_timeout_to_unknown_label_is_schema_error():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: "0x700", timeout: 0.5, on_timeout: nowhere}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "unknown label" in tc.error


def test_parse_session_builtin_as_data_list_entry_ok():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - can_send: {cob: "0x781", data: [$session, "0x02", 0, 0]}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


# -- lss_assign (standard-LSS addressing, core.core._exec_one) --

def test_parse_lss_assign_valid_with_expected_builtin():
    text = 'id: "1"\nname: x\nsteps:\n  - lss_assign: {count: "$expected"}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_lss_assign_valid_with_into_register():
    text = 'id: "1"\nname: x\nsteps:\n  - lss_assign: {count: 3, into: R2}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_lss_assign_rejects_missing_count():
    text = 'id: "1"\nname: x\nsteps:\n  - lss_assign: {into: R2}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "lss_assign" in tc.error


def test_parse_lss_assign_rejects_bad_register():
    text = 'id: "1"\nname: x\nsteps:\n  - lss_assign: {count: 3, into: Q9}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "into must be a register" in tc.error


def test_parse_lss_assign_rejects_extra_field():
    text = 'id: "1"\nname: x\nsteps:\n  - lss_assign: {count: 3, bogus: 1}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "lss_assign" in tc.error


# -- wait_for list-form cob/data + into (races multiple COBs in one wait) --

def test_parse_wait_for_list_form_cob_accepted():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: ["0x700", "0x783"], data: ["", "02"], timeout: 0.5}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_wait_for_list_form_cob_data_length_mismatch_rejected():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: ["0x700", "0x783"], data: ["00"], timeout: 0.5}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "same length" in tc.error


def test_parse_wait_for_into_valid_register_accepted():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: ["0x700", "0x783"], timeout: 0.5, into: R4}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None


def test_parse_wait_for_into_invalid_register_rejected():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: "0x700", timeout: 0.5, into: Q9}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "invalid into" in tc.error


def test_parse_wait_for_frame_form_still_rejects_unknown_field():
    # regression: adding `into` to the allowlist must not accidentally widen
    # it beyond {cob, timeout, data, on_timeout, into}
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - wait_for: {cob: "0x700", timeout: 0.5, bogus: 1}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "unknown field" in tc.error


# -- header and steps a foreign suite needs translated ----------------------

def test_desc_and_grade_are_optional_header_fields():
    """Both exist because a report has to say what the case is about and
    whether somebody has to stand next to the bench for it."""
    text = ('id: "1"\nname: x\ndesc: "what this checks"\ngrade: semi\n'
            'steps:\n  - end:\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error is None
    assert tc.desc == "what this checks" and tc.grade == "semi"


def test_an_invented_grade_is_a_schema_error():
    text = 'id: "1"\nname: x\ngrade: mostly\nsteps:\n  - end:\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "grade" in tc.error


def test_a_step_may_name_its_own_node():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - sdo_read: {index: "0x2000", sub: "00", node: 2}\n'
            '  - sdo_write: {index: "0x2000", sub: "00", value: 1, node: R3}\n')
    assert parse_testcase(text, "TC1_x.yaml").error is None


def test_a_node_that_is_not_a_value_is_rejected():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - sdo_read: {index: "0x2000", sub: "00", node: everyone}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "invalid node" in tc.error


def test_expect_emcy_needs_a_code_and_takes_a_mask():
    ok = ('id: "1"\nname: x\nsteps:\n'
          '  - emcy_clear:\n'
          '  - expect_emcy: {code: "0x7100", mask: "0x00FF", node: 2, timeout: 1.5}\n')
    assert parse_testcase(ok, "TC1_x.yaml").error is None
    missing = 'id: "1"\nname: x\nsteps:\n  - expect_emcy: {mask: "0x00FF"}\n'
    tc = parse_testcase(missing, "TC1_x.yaml")
    assert tc.error and "missing field" in tc.error


def test_the_register_file_goes_to_r15():
    text = 'id: "1"\nname: x\nsteps:\n  - mov: {to: R15, value: 1}\n'
    assert parse_testcase(text, "TC1_x.yaml").error is None


def test_skip_needs_a_reason():
    text = 'id: "1"\nname: x\nsteps:\n  - skip:\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "skip" in tc.error


def test_the_psu_step_takes_volts_amps_and_the_output():
    ok = ('id: "1"\nname: x\nsteps:\n'
          '  - psu: {ch: 2, volt: 26.5, curr: 2}\n'
          '  - psu: {output: on}\n'
          '  - psu: {output: off}\n')
    assert parse_testcase(ok, "TC1_x.yaml").error is None


def test_a_psu_step_that_asks_for_nothing_is_a_schema_error():
    text = 'id: "1"\nname: x\nsteps:\n  - psu: {ch: 1}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "at least one" in tc.error


def test_volts_may_be_fractional_but_not_prose():
    """26.5 V is an ordinary request; "high" is not."""
    good = 'id: "1"\nname: x\nsteps:\n  - psu: {volt: 26.5}\n'
    assert parse_testcase(good, "TC1_x.yaml").error is None
    bad = 'id: "1"\nname: x\nsteps:\n  - psu: {volt: high}\n'
    assert parse_testcase(bad, "TC1_x.yaml").error


# -- a scalar `variants:` is a schema error, not a crash (parse_testcase) ---
# `doc.get("variants") or []` is truthy for a bare int, so iterating it to
# build tc.variants raised TypeError before this was guarded — a parse bug
# in one file must not be able to raise out of parse_testcase.

def test_parse_rejects_a_scalar_variants_instead_of_crashing():
    text = 'id: "1"\nname: x\nvariants: 820\nsteps:\n  - end:\n'
    tc = parse_testcase(text, "TC1_x.yaml")  # must not raise
    assert tc.error and "variants must be a list" in tc.error


def test_load_catalog_survives_a_bad_scalar_variants_file_next_to_a_good_one(tmp_path):
    """Before the fix the TypeError from the scalar `variants:` escaped
    load_catalog entirely — it only catches OSError — so one bad file lost
    every other case in the folder, not just its own entry."""
    (tmp_path / "TC0001_ok.yaml").write_text(VALID)
    (tmp_path / "TC0002_bad.yaml").write_text(
        'id: "0002"\nname: x\nvariants: 820\nsteps:\n  - end:\n')
    catalog = load_catalog(tmp_path)  # must not raise
    assert [tc.file for tc in catalog] == ["TC0001_ok.yaml", "TC0002_bad.yaml"]
    assert catalog[0].error is None
    assert catalog[1].error and "variants must be a list" in catalog[1].error


# -- `ask` and `adjust` validate `timeout` (parse_testcase) -----------------
# A non-numeric timeout used to reach `float()` in the executor and kill
# the run task with no verdict; this must be a schema error instead.

def test_ask_rejects_a_non_numeric_timeout():
    text = 'id: "1"\nname: x\nsteps:\n  - ask: {text: "sure?", timeout: "soon"}\n'
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "timeout" in tc.error


def test_adjust_rejects_a_non_numeric_timeout():
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - adjust: {index: "0x2000", sub: "00", timeout: "soon"}\n')
    tc = parse_testcase(text, "TC1_x.yaml")
    assert tc.error and "timeout" in tc.error


def test_every_mapping_valued_step_may_carry_a_note():
    """The note is what makes a report say *why* a step ran, so it has to
    work on all of them. Three primitives compared their key set exactly
    and rejected it — which only showed up as a whole generated test case
    failing to load, not as anything the schema complained about clearly.
    """
    text = ('id: "1"\nname: x\nsteps:\n'
            '  - mov: {to: R1, value: 5, note: "remember the variant"}\n'
            '  - jump_eq: {a: R1, b: 5, to: done, note: "700 -> jump"}\n'
            '  - lss_assign: {count: 3, note: "address them"}\n'
            '  - can_send: {cob: "0x80", data: [], note: "SYNC"}\n'
            '  - nmt: {cmd: start, note: "needed for PDOs"}\n'
            '  - wait_for: {cob: "0x700", timeout: 1.0, note: "boot-up"}\n'
            '  - sdo_read: {index: "0x2000", sub: "00", note: "read it back"}\n'
            '  - manual: {text: "flip it", note: "the aux supply"}\n'
            '  - ask: {text: "turning?", note: "watch the wheel"}\n'
            '  - label: done\n')
    assert parse_testcase(text, "TC1_x.yaml").error is None


def test_a_note_must_be_a_text_and_a_typo_is_still_caught():
    """Allowing `note` everywhere must not turn the strict-key contract
    into a free-for-all — `notee` is still a schema error."""
    bad = 'id: "1"\nname: x\nsteps:\n  - mov: {to: R1, value: 5, notee: "typo"}\n'
    assert parse_testcase(bad, "TC1_x.yaml").error
    empty = 'id: "1"\nname: x\nsteps:\n  - mov: {to: R1, value: 5, note: ""}\n'
    assert "note must be a text" in parse_testcase(empty, "TC1_x.yaml").error

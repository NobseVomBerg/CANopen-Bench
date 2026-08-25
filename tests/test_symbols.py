"""Symbol tables parsed from C headers (canopen_bench/symbols.py).

The parser exists so the bench says what the firmware says. Its whole value
depends on being exact, so these tests are mostly about the ways a
hand-written header can be misread — implicit successors, expressions over
other symbols, octal-looking literals — plus refusing rather than guessing
when it cannot tell.
"""
from __future__ import annotations

import pytest

from canopen_bench.core import Bench
from canopen_bench.db import Db
from canopen_bench.plugin import BenchPlugin
from canopen_bench.symbols import SymbolError, load_symbols, parse_header
from canopen_bench.testcases import parse_testcase


def syms(text: str, source: str = "fake/x.h"):
    parsed, errors = parse_header(text, source)
    return {s.name: s for s in parsed}, errors


# -- enum shapes ------------------------------------------------------------

def test_typedef_enum_members_and_table_name():
    table, errors = syms("""
        typedef enum eObjIdx {
            eObjIdx_LampControl = 0x2050,
            eObjIdx_MotorCurrent = 0x2060,
        } eObjIdx;
    """)
    assert not errors
    assert table["eObjIdx_LampControl"].value == 0x2050
    assert table["eObjIdx_MotorCurrent"].value == 0x2060


def test_implicit_successors_continue_from_the_last_anchor():
    """a real sub-index table anchors at 0x01, 0x05, 0x0A, 0x10, 0x16 and lets the rest run
    on. Miscounting shifts a sub-index silently, which is the worst thing
    that can go wrong here."""
    table, _ = syms("""
        typedef enum eSub {
            eSub_A = 0x01,
            eSub_B,
            eSub_C,
            eSub_D,
            eSub_E = 0x05,
            eSub_F,
        } eSub;
    """)
    assert [table[n].value for n in ("eSub_A", "eSub_B", "eSub_C", "eSub_D",
                                     "eSub_E", "eSub_F")] == [1, 2, 3, 4, 5, 6]


def test_bare_enum_starts_at_zero():
    table, _ = syms("typedef enum eKey { eKey_Up, eKey_Down, eKey_Enter } eKey;")
    assert [table[n].value for n in ("eKey_Up", "eKey_Down", "eKey_Enter")] == [0, 1, 2]


def test_two_enums_in_one_file_do_not_bleed_into_each_other():
    table, _ = syms("""
        typedef enum eA { eA_One = 7 } eA;
        typedef enum eB { eB_First } eB;
    """)
    assert table["eB_First"].value == 0
    assert table["eA_One"].table == "eA" and table["eB_First"].table == "eB"


# -- expressions ------------------------------------------------------------

def test_value_may_be_an_expression_over_earlier_symbols():
    """A composed state written as (eLamp_Off << 8) makes a literal-only
    reader throw out the whole file."""
    table, errors = syms("""
        typedef enum eLamp { eLamp_Off = 1, eLamp_Blinking = 3 } eLamp;
        typedef enum eLampState {
            eLampState_GreenOff = (eLamp_Off << 8),
            eLampState_BlueBlinking = (eLamp_Blinking << 16),
        } eLampState;
    """)
    assert not errors
    assert table["eLampState_GreenOff"].value == 0x0100
    assert table["eLampState_BlueBlinking"].value == 0x030000


def test_unknown_reference_is_an_error_not_a_zero():
    table, errors = syms("typedef enum eX { eX_A = (eNope << 8) } eX;")
    assert "eX_A" not in table
    assert any("eNope" in e for e in errors)


def test_a_function_call_is_refused():
    _table, errors = syms("typedef enum eX { eX_A = compute(1) } eX;")
    assert any("constant expression" in e for e in errors)


def test_one_bad_member_does_not_take_the_enum_with_it():
    table, errors = syms("""
        typedef enum eX {
            eX_Good = 1,
            eX_Bad = nope(),
            eX_AlsoGood = 3,
        } eX;
    """)
    assert table["eX_Good"].value == 1 and table["eX_AlsoGood"].value == 3
    assert len(errors) == 1


# -- integer literals -------------------------------------------------------

def test_real_c_octal_is_read_as_octal():
    table, errors = syms("#define MODE 0755\n")
    assert not errors and table["MODE"].value == 0o755


def test_leading_zero_with_an_8_is_refused_rather_than_guessed():
    """08150815 is not valid C either. This number unlocks write access on a
    device — picking a reading for it is not the parser's call."""
    _table, errors = syms("#define UNLOCK_CODE 0918\n")
    assert any("octal" in e for e in errors)


def test_integer_suffixes_are_tolerated():
    table, errors = syms("#define A 0x10U\n#define B 12UL\n")
    assert not errors and table["A"].value == 0x10 and table["B"].value == 12


# -- comments ---------------------------------------------------------------

def test_trailing_doc_comment_becomes_the_description():
    table, _ = syms("""
        typedef enum eSub {
            eSub_Tension = 0x0B,   //!< In 0.1 cN
        } eSub;
    """)
    assert table["eSub_Tension"].desc == "In 0.1 cN"


def test_block_comments_are_ignored_without_shifting_line_numbers():
    table, _ = syms("""
        /* a
           multi-line
           comment */
        typedef enum eX { eX_A = 1 } eX;
    """)
    assert table["eX_A"].line == 5


def test_includes_and_pragmas_are_ignored():
    table, errors = syms('#pragma once\n#include "missing.h"\n'
                         "typedef enum eX { eX_A = 1 } eX;")
    assert not errors and table["eX_A"].value == 1


# -- lookups ----------------------------------------------------------------

@pytest.fixture()
def tables(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "a.h").write_text(
        "typedef enum eMode { eMode_Off = 2, eMode_Run = 4 } eMode;\n"
        "typedef enum eObjIdx { eObjIdx_Lamp = 0x2050 } eObjIdx;\n")
    return load_symbols([("acme", tmp_path / "acme")])


def test_forward_and_reverse_lookup(tables):
    assert tables.value("eObjIdx_Lamp") == 0x2050
    assert tables.name("eMode", 4) == "eMode_Run"
    assert tables.name("eMode", 99) == ""


def test_unknown_symbol_raises_with_the_name_in_the_message(tables):
    with pytest.raises(SymbolError, match="eNope"):
        tables.value("eNope")


def test_conflicting_definitions_are_refused_and_need_qualifying(tmp_path):
    """Silently picking one of two definitions is how a bench ends up
    writing to the wrong object and blaming the device."""
    for origin, value in (("acme", "0x2050"), ("globex", "0x3000")):
        (tmp_path / origin).mkdir()
        (tmp_path / origin / "h.h").write_text(
            f"typedef enum eIdx {{ eIdx_Button = {value} }} eIdx;\n")
    tables = load_symbols([(o, tmp_path / o) for o in ("acme", "globex")])
    assert any("eIdx_Button" in e for e in tables.errors)
    with pytest.raises(SymbolError, match="qualify"):
        tables.value("eIdx_Button")
    assert tables.value("acme:eIdx_Button") == 0x2050
    assert tables.value("globex:eIdx_Button") == 0x3000


# -- workspace wiring -------------------------------------------------------

class _SymbolPlugin(BenchPlugin):
    name = "fake"

    def __init__(self, directory):
        self._dir = directory

    def symbol_dirs(self):
        return [self._dir]


@pytest.fixture()
def sym_bench(tmp_path):
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "obj.h").write_text(
        "typedef enum eObjIdx { eObjIdx_Lamp = 0x2050 } eObjIdx;\n")
    return Bench(Db(tmp_path / "s.db"), plugins=[_SymbolPlugin(packaged)]), packaged


def test_plugin_headers_are_seeded_into_the_workspace(sym_bench):
    bench, _packaged = sym_bench
    assert (bench.symbols_dir / "fake" / "obj.h").exists()
    assert bench.symbols.value("eObjIdx_Lamp") == 0x2050


def test_workspace_copy_wins_and_is_never_overwritten(tmp_path):
    """The operator drops in the headers of the firmware actually under
    test; a plugin release must not quietly replace them. Said once in the
    log, because a difference nobody mentions is one nobody finds."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "obj.h").write_text("typedef enum eX { eX_A = 1 } eX;\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "symbols" / "fake").mkdir(parents=True)
    (ws / "symbols" / "fake" / "obj.h").write_text("typedef enum eX { eX_A = 99 } eX;\n")
    bench = Bench(Db(ws / "s.db"), plugins=[_SymbolPlugin(packaged)])
    assert bench.symbols.value("eX_A") == 99
    assert [x for x in bench.logs if "differs from the one fake ships" in x["msg"]]

    # …and the next start says nothing and still leaves it alone: a bench
    # that repeats it every time is a bench whose log nobody reads
    again = Bench(Db(ws / "s.db"), plugins=[_SymbolPlugin(packaged)])
    assert again.symbols.value("eX_A") == 99
    assert not [x for x in again.logs if "differs from the one fake ships" in x["msg"]]


def test_a_symbol_directory_that_is_not_there_is_said_out_loud(tmp_path):
    """Deleting the packaged headers to force a re-seed deletes what the
    re-seed reads from, and the workspace then has no tables at all —
    which on screen is indistinguishable from a firmware that declares
    none."""
    bench = Bench(Db(tmp_path / "s.db"), plugins=[_SymbolPlugin(tmp_path / "gone")])
    assert not bench.symbols.by_name
    assert [x for x in bench.logs if "no such directory" in x["msg"]]


def test_a_header_the_plugin_changes_reaches_a_workspace_that_kept_the_old_one(tmp_path):
    """The trap this was: a workspace is seeded once and then never again,
    so it stays the snapshot of the day it was made. A plugin author adds a
    table, the panel beside it updates — panels are read from the package —
    and the dropdown those symbols fill stays empty with nothing on screen
    saying why.

    A copy nobody has touched follows the package. One that has been
    touched does not, which is the test above."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    header = packaged / "obj.h"
    header.write_text("typedef enum eX { eX_A = 1 } eX;\n")
    ws = tmp_path / "ws"
    ws.mkdir()

    bench = Bench(Db(ws / "s.db"), plugins=[_SymbolPlugin(packaged)])
    assert bench.symbols.value("eX_A") == 1
    assert "eX_Bad" not in bench.symbols.by_name

    header.write_text("typedef enum eX { eX_A = 1, eX_Bad = 2 } eX;\n")
    later = Bench(Db(ws / "s.db"), plugins=[_SymbolPlugin(packaged)])
    assert later.symbols.value("eX_Bad") == 2
    assert (ws / "symbols" / "fake" / "obj.h").read_text() == header.read_text()


def test_symbol_summary_reaches_the_snapshot(sym_bench):
    bench, _ = sym_bench
    assert bench.snapshot()["ext"]["symbols"]["symbols"] == 1


def test_reload_action_picks_up_a_changed_header(sym_bench):
    bench, _ = sym_bench
    (bench.symbols_dir / "fake" / "obj.h").write_text(
        "typedef enum eObjIdx { eObjIdx_Lamp = 0x2222 } eObjIdx;\n")
    bench.dispatch("symbols_reload", {})
    assert bench.symbols.value("eObjIdx_Lamp") == 0x2222


# -- test-case references ---------------------------------------------------

_CASE = """
id: "9.9"
name: Symbol reference
steps:
  - sdo_write: {index: $eObjIdx_Lamp, sub: "00", value: "0x01", size: 4}
"""


def test_symbol_reference_resolves_at_parse_time(tables):
    tc = parse_testcase(_CASE, "TC9.9_x.yaml", symbols=tables)
    assert tc.error is None
    assert tc.steps[0]["sdo_write"]["index"] == "0x2050"


def test_unknown_symbol_fails_the_file_not_the_run(tables):
    """A typo must surface in the catalog, not twenty minutes into a run
    against real hardware."""
    tc = parse_testcase(_CASE.replace("eObjIdx_Lamp", "eObjIdx_Buttn"),
                        "TC9.9_x.yaml", symbols=tables)
    assert tc.error and "eObjIdx_Buttn" in tc.error


def test_builtins_are_left_alone(tables):
    case = _CASE.replace("$eObjIdx_Lamp", '"0x2050"').replace(
        'value: "0x01"', "value: $node")
    tc = parse_testcase(case, "TC9.9_x.yaml", symbols=tables)
    assert tc.error is None and tc.steps[0]["sdo_write"]["value"] == "$node"

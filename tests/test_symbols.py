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
from canopen_bench.testcases import load_catalog, parse_testcase


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


# -- two origins: the tables as each of them reads them ----------------------
#
# Two device families each ship an eObjIdx and a mode table — the same names,
# other values. The bench-wide tables cannot hold both, and `scoped` is what
# lets each family read the one it wrote.

_ACME_H = """\
/// @brief Lamp states of the acme family
typedef enum eLamp { eLamp_Off = 0, eLamp_On = 1 } eLamp;
typedef enum eObjIdx { eObjIdx_Lamp = 0x2050 } eObjIdx;
#define SHARED_LIMIT 10
#define ACME_LIMIT 99
"""

_BORG_H = """\
/// @brief Lamp states of the borg family
typedef enum eLamp { eLamp_Off = 0, eLamp_On = 5, eLamp_Blinking = 6 } eLamp;
/// @brief Object indices of the borg family
typedef enum eObjIdx { eObjIdx_Lamp = 0x2150 } eObjIdx;
/// @brief Speeds of a borg motor
typedef enum eSpeed { eSpeed_Slow = 1, eSpeed_Fast = 2 } eSpeed;
#define SHARED_LIMIT 20
#define BORG_RETRIES 3
"""


def _origins(tmp_path, **headers: str | list[str]):
    """One directory per origin, holding the header or headers it is given."""
    dirs = []
    for origin, texts in headers.items():
        (tmp_path / origin).mkdir()
        for i, text in enumerate([texts] if isinstance(texts, str) else texts):
            (tmp_path / origin / f"h{i}.h").write_text(text)
        dirs.append((origin, tmp_path / origin))
    return load_symbols(dirs)


@pytest.fixture()
def two(tmp_path):
    return _origins(tmp_path, acme=_ACME_H, borg=_BORG_H)


def test_the_bench_wide_tables_leave_a_shared_name_ambiguous(two):
    for name in ("eObjIdx_Lamp", "eLamp_On", "SHARED_LIMIT"):
        with pytest.raises(SymbolError, match="qualify"):
            two.value(name)


def test_a_name_every_origin_defines_alike_is_not_ambiguous(two):
    assert two.value("eLamp_Off") == 0
    assert "eLamp_Off" not in two.ambiguous


def test_a_scope_reads_a_shared_name_as_its_own_headers_define_it(two):
    acme, borg = two.scoped("acme"), two.scoped("borg")
    assert (acme.value("eObjIdx_Lamp"), borg.value("eObjIdx_Lamp")) == (0x2050, 0x2150)
    assert (acme.value("eLamp_On"), borg.value("eLamp_On")) == (1, 5)
    assert (acme.value("SHARED_LIMIT"), borg.value("SHARED_LIMIT")) == (10, 20)
    assert not acme.ambiguous and not borg.ambiguous


@pytest.mark.parametrize("view", ["merged", "acme", "borg"])
def test_a_qualified_name_means_the_same_in_the_merged_tables_and_in_either_scope(two, view):
    tables = two if view == "merged" else two.scoped(view)
    assert tables.value("acme:eObjIdx_Lamp") == 0x2050
    assert tables.value("borg:eObjIdx_Lamp") == 0x2150


def test_a_reverse_lookup_in_a_scope_answers_from_its_own_table_only(two):
    acme, borg = two.scoped("acme"), two.scoped("borg")
    assert acme.name("eLamp", 1) == "eLamp_On" and borg.name("eLamp", 5) == "eLamp_On"
    # 5 is something in borg's table and nothing in acme's, and the other way round
    assert acme.name("eLamp", 5) == "" and borg.name("eLamp", 1) == ""
    assert acme.name("eLamp", 6) == "", "borg's member, a value acme's table does not have either"


def test_a_member_only_the_other_version_of_a_table_has_stays_out_of_the_scope(two):
    """The table is the unit: taking a neighbour's members into a table of
    the same name is how a dropdown comes to offer choices its device never
    had."""
    acme, borg = two.scoped("acme"), two.scoped("borg")
    assert set(acme.tables["eLamp"]) == {"eLamp_Off", "eLamp_On"}
    assert set(borg.tables["eLamp"]) == {"eLamp_Off", "eLamp_On", "eLamp_Blinking"}
    with pytest.raises(SymbolError, match="unknown symbol"):
        acme.value("eLamp_Blinking")
    assert acme.value("borg:eLamp_Blinking") == 6, "reachable, but only by saying whose it is"


def test_a_table_or_a_define_only_the_other_origin_has_is_visible(two):
    acme, borg = two.scoped("acme"), two.scoped("borg")
    assert acme.tables["eSpeed"]["eSpeed_Fast"].value == 2
    assert acme.value("eSpeed_Slow") == 1
    assert acme.value("BORG_RETRIES") == 3
    assert borg.value("ACME_LIMIT") == 99


def test_a_scope_describes_a_shared_table_by_its_own_brief_and_the_others_by_theirs(two):
    acme, borg = two.scoped("acme"), two.scoped("borg")
    assert acme.descriptions["eLamp"] == "Lamp states of the acme family"
    assert borg.descriptions["eLamp"] == "Lamp states of the borg family"
    assert acme.descriptions["eSpeed"] == "Speeds of a borg motor"
    # a table acme defines is described by acme or not at all: borg's sentence
    # is about borg's eObjIdx
    assert "eObjIdx" not in acme.descriptions


def test_scoped_hands_back_the_merged_tables_for_a_single_origin(tmp_path):
    solo = _origins(tmp_path, acme=_ACME_H)
    assert solo.scoped("acme") is solo


@pytest.mark.parametrize("origin", ["nobody", "", "empty"],
                         ids=["unknown origin", "no origin", "origin without symbols"])
def test_scoped_hands_back_the_merged_tables_where_there_is_nothing_to_tell_apart(tmp_path, origin):
    tables = _origins(tmp_path, acme=_ACME_H, borg=_BORG_H, empty="// nothing declared in here\n")
    assert tables.scoped(origin) is tables


def test_a_scope_is_built_once_and_each_origin_has_its_own(two):
    assert two.scoped("acme") is two.scoped("acme")
    assert two.scoped("acme") is not two
    assert two.scoped("acme") is not two.scoped("borg")


def test_an_origin_that_contradicts_itself_stays_ambiguous_in_its_own_scope(tmp_path):
    tables = _origins(tmp_path,
                      acme=["typedef enum eX { eX_A = 1 } eX;\n",
                            "typedef enum eX { eX_A = 2 } eX;\n"],
                      borg="typedef enum eB { eB_First = 3 } eB;\n")
    with pytest.raises(SymbolError, match="qualify"):
        tables.scoped("acme").value("eX_A")


def test_a_name_defined_three_ways_stays_ambiguous(tmp_path):
    """Dropped from the bare index by the second definition, the name was
    free for a third to claim — and resolved again, to whichever origin
    happened to be read last. The same silent pick the refusal exists to
    prevent."""
    tables = _origins(tmp_path, **{origin: f"typedef enum eX {{ eX_A = {value} }} eX;\n"
                                   for origin, value in (("acme", 1), ("borg", 2), ("globex", 3))})
    with pytest.raises(SymbolError, match="qualify"):
        tables.value("eX_A")
    assert "eX_A" not in tables.by_name
    assert [tables.value(f"{origin}:eX_A") for origin in ("acme", "borg", "globex")] == [1, 2, 3]
    assert any("globex/h0.h" in error for error in tables.errors), "the third is reported too"
    # …and each of them still reads its own
    assert [tables.scoped(origin).value("eX_A") for origin in ("acme", "borg", "globex")] == [1, 2, 3]


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


def test_the_packaged_header_wins_over_a_workspace_copy_of_it(tmp_path):
    """A header is part of its plugin the way its panels and its EDS are,
    and those are read from the package every start. A copy that outranked
    the package meant editing a header changed the panel beside it and not
    the dropdown its symbols fill — and the way out was knowing which of
    two folders to delete."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "obj.h").write_text("typedef enum eX { eX_A = 1 } eX;\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "symbols" / "fake").mkdir(parents=True)
    (ws / "symbols" / "fake" / "obj.h").write_text("typedef enum eX { eX_A = 99 } eX;\n")
    bench = Bench(Db(ws / "s.db"), plugins=[_SymbolPlugin(packaged)])
    assert bench.symbols.value("eX_A") == 1


def test_a_header_no_plugin_ships_is_left_where_it_was_put(tmp_path):
    """Which is what the folder is for besides looking at: the headers of
    a firmware under test go in beside the packaged ones, under names of
    their own."""
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    (packaged / "obj.h").write_text("typedef enum eX { eX_A = 1 } eX;\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "symbols" / "fake").mkdir(parents=True)
    (ws / "symbols" / "fake" / "under_test.h").write_text(
        "typedef enum eStatus { eStatus_Running = 7 } eStatus;\n")
    bench = Bench(Db(ws / "s.db"), plugins=[_SymbolPlugin(packaged)])
    assert bench.symbols.value("eStatus_Running") == 7
    assert bench.symbols.value("eX_A") == 1


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
    saying why."""
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
    """A header dropped into the workspace, which is the one kind the
    workspace owns — a copy of a packaged file is replaced by the package
    on the way through, which is the point of the reload as well."""
    bench, packaged = sym_bench
    (bench.symbols_dir / "fake" / "under_test.h").write_text(
        "typedef enum eStatus { eStatus_Running = 0x2222 } eStatus;\n")
    bench.dispatch("symbols_reload", {})
    assert bench.symbols.value("eStatus_Running") == 0x2222

    # …and the same for one the plugin ships, without a restart
    (packaged / "obj.h").write_text(
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


@pytest.fixture()
def cases(tmp_path):
    """A folder with one case per family, both written with the bare name."""
    folder = tmp_path / "cases"
    folder.mkdir()
    for ident, origin in (("9.1", "acme"), ("9.2", "borg")):
        (folder / f"TC{ident}_{origin}.yaml").write_text(_CASE.replace('"9.9"', f'"{ident}"'))
    return folder


def test_a_catalog_reads_each_file_against_the_tables_symbols_for_gives_it(two, cases):
    """What a case a device family ships needs: it was written against that
    family's headers, and has to keep loading when another family defines the
    same names. Given the merged tables as well, which would refuse both
    files, the per-file answer is the one that counts."""
    views = {"TC9.1_acme.yaml": two.scoped("acme"), "TC9.2_borg.yaml": two.scoped("borg")}
    asked: list[str] = []

    def symbols_for(name: str):
        asked.append(name)
        return views[name]

    catalog = load_catalog(cases, symbols=two, symbols_for=symbols_for)
    assert asked == ["TC9.1_acme.yaml", "TC9.2_borg.yaml"]
    assert [tc.error for tc in catalog] == [None, None]
    assert [tc.steps[0]["sdo_write"]["index"] for tc in catalog] == ["0x2050", "0x2150"]


def test_a_catalog_without_symbols_for_reads_the_whole_folder_against_one_set_of_tables(two, cases):
    catalog = load_catalog(cases, symbols=two)
    assert all(tc.error and "qualify" in tc.error for tc in catalog)

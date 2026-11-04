import json
from pathlib import Path

from forever_lint import cli, toc
from forever_lint import db as dbmod

FIX = Path(__file__).parent / "fixtures"


def run(*args):
    parsed = cli.build_parser().parse_args([str(a) for a in args])
    return cli.run(parsed)


def addon(name):
    found = toc.find_addons(FIX / name)
    assert len(found) == 1
    return toc.resolve(found[0])


def test_classic_only_addon_has_no_forever_toc():
    a = addon("ClassicOnly")
    assert a.toc is None and a.problems[0][0] == "FL008" and a.problems[0][1] == "error"


def test_forever_picks_camelot_toc_and_its_files():
    a = addon("Ported")
    assert a.toc.path.name == "Ported_Camelot.toc"
    names = sorted(p.name for p in a.lua_files)
    # Classic.lua is vanilla-only, Retail.lua excludes camelot, [TextLocale] becomes enUS, Libs path uses backslashes
    assert names == ["Core.lua", "LibFoo.lua", "Locale.enUS.lua"]
    assert a.problems == []


def test_plain_toc_with_xml_includes_and_interface_warning():
    a = addon("Sample")
    assert a.toc.path.name == "Sample.toc"
    assert sorted(p.name for p in a.lua_files) == ["Core.lua", "Frames.lua"]  # Frames.lua comes from UI.xml
    assert [p[0] for p in a.problems] == ["FL009"]


def test_excluded_by_game_type_is_a_note():
    a = addon("Excluded")
    assert a.problems[0][:2] == ("FL010", "note")


def test_find_addons_skips_embedded_libraries():
    names = sorted(a.name for a in toc.find_addons(FIX))
    assert names == ["ClassicOnly", "Excluded", "Ported", "Sample"]


def test_ported_addon_is_clean_and_library_findings_do_not_fail(tmp_path):
    out, code = run(FIX / "Ported", "--format", "json")
    data = json.loads(out)
    assert code == 0
    own = [f for f in data["findings"] if not f["library"]]
    assert [(f["name"], f["severity"]) for f in own] == [("GetAddOnMetadata", "note")]  # fallback after checking C_AddOns
    assert [f["name"] for f in data["findings"] if f["library"]] == ["GetItemInfo"]


def test_exit_codes_and_ignore():
    _, code = run(FIX / "Sample")
    assert code == 1
    _, code = run(FIX / "Sample", "--fail-on", "never")
    assert code == 0
    out, _ = run(FIX / "Sample", "--format", "json", "--ignore", "FL001,FL003,FL004,FL005,FL006,FL007,FL009")
    assert json.loads(out)["findings"] == []


def test_sarif_is_well_formed():
    out, _ = run(FIX / "Sample", "--format", "sarif")
    doc = json.loads(out)
    run0 = doc["runs"][0]
    rules = [r["id"] for r in run0["tool"]["driver"]["rules"]]
    assert doc["version"] == "2.1.0" and rules[0] == "FL001"
    for r in run0["results"]:
        assert rules[r["ruleIndex"]] == r["ruleId"]
        assert r["level"] in ("error", "warning", "note")
        loc = r["locations"][0]["physicalLocation"]
        assert not loc["artifactLocation"]["uri"].startswith("/")


def test_text_report_mentions_target_and_summary():
    out, _ = run(FIX / "Sample", "--no-color")
    assert "interface 16001" in out.splitlines()[0]
    assert out.rstrip().splitlines()[-1].startswith("8 errors")


def test_explain():
    out, _ = run("--explain", "GetItemInfo")
    assert "replaced by C_Item.GetItemInfo" in out
    out, _ = run("--explain", "C_Spell.GetSpellInfo")
    assert "available" in out


def test_database_facts():
    db = dbmod.load()
    assert db.interface == 16001 and db.project_id == 18 and db.project_name == "WOW_PROJECT_CAMELOT"
    assert not (set(db.removed) & db.available)
    assert "C_Spell.GetSpellInfo" in db.available and "GetSpellInfo" in db.removed
    assert db.secret["UnitHealth"]["always"] is True
    assert "C_SpellBook" not in db.removed and "math" not in db.removed
    assert all(n[0].isupper() or n.startswith("C_") or "." in n or n[0] == "_" or n[0].islower() for n in db.removed)
    assert not [n for n in db.removed if n in ("name", "value", "info")]


def test_toc_loading_a_file_from_a_sibling_folder(tmp_path):
    # Regression: RealUI's SharedMedia TOC loads ../SharedMedia_MyMedia/MyMedia.lua and the CLI crashed.
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "A.toc").write_text("## Interface: 16001\n..\\B\\shared.lua\n")
    (tmp_path / "B" / "shared.lua").write_text("GetItemInfo(1)")
    out, _ = run(tmp_path / "A", "--format", "json")
    assert [f["name"] for f in json.loads(out)["findings"]] == ["GetItemInfo"]


def test_export_luacheck():
    out, code = run("--export-luacheck")
    assert code == 0 and out.startswith("-- WoW Forever")
    assert '\tC_Spell = { fields = {' in out and '"GetSpellInfo"' in out.split("C_Spell = ")[1].split("\n")[0]
    assert '\t"GetSpellInfo",' not in out  # removed in Forever, so luacheck should flag it

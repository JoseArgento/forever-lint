import pytest

from forever_lint import analyzer
from forever_lint import db as dbmod


@pytest.fixture(scope="module")
def db():
    return dbmod.load()


def lint(db, *sources):
    scans = [analyzer.scan(f"f{i}.lua", src) for i, src in enumerate(sources)]
    return analyzer.check(scans, db, lambda p: False)


def by_rule(findings, rule):
    return [f for f in findings if f.rule == rule]


def test_removed_api_is_an_error_with_replacement(db):
    f = by_rule(lint(db, "local n = GetItemInfo(19019)"), "FL001")
    assert len(f) == 1 and f[0].severity == "error" and "C_Item.GetItemInfo" in f[0].message
    assert (f[0].line, f[0].col) == (1, 11)


def test_cached_upvalue_reports_once(db):
    f = lint(db, "local GetSpellInfo = GetSpellInfo\nGetSpellInfo(1)\nGetSpellInfo(2)")
    assert [(x.line, x.name) for x in by_rule(f, "FL001")] == [(1, "GetSpellInfo")]
    assert "SpellInfo table" in f[0].message


def test_locals_parameters_loop_vars_and_fields_are_not_globals(db):
    src = """
local function GetContainerItemInfo() end
GetContainerItemInfo()
local function f(UnitAura) return UnitAura() end
for GetSpellInfo = 1, 3 do print(GetSpellInfo) end
for _, GetItemInfo in pairs({}) do GetItemInfo() end
local t = { GetMouseFocus = 1, UnitBuff = true }
t.GetSpellInfo = 2
print(t.GetItemInfo, ns:GetSpellInfo())
"""
    assert by_rule(lint(db, src), "FL001") == []


def test_local_declared_after_expression(db):
    # In "local x = GetItemInfo" the right side is the global; after the statement x is local.
    f = by_rule(lint(db, "local GetItemInfo = GetItemInfo or C_Item.GetItemInfo\nprint(GetItemInfo(1))"), "FL001")
    assert len(f) == 1 and f[0].line == 1 and f[0].severity == "note"  # "or" makes it a guarded fallback


def test_block_scopes_end(db):
    src = "do local GetItemInfo = 1 end\nGetItemInfo(1)"
    f = by_rule(lint(db, src), "FL001")
    assert [x.line for x in f] == [2]


def test_locals_inside_a_function_passed_as_argument(db):
    # Regression: the open "(" of the call kept "local x = ..." from becoming local inside the callback.
    src = """
C_Timer.After(1, function()
  if true then
    local GetItemInfo = C_Item.GetItemInfo
    GetItemInfo(1)
  end
end)
frame:SetScript("OnEvent", function(self, event)
  local GetSpellInfo = C_Spell.GetSpellInfo
  print(GetSpellInfo(1))
end)
GetMouseFocus()
"""
    f = by_rule(lint(db, src), "FL001")
    assert [(x.line, x.name) for x in f] == [(12, "GetMouseFocus")]


def test_local_table_then_function_on_the_next_line(db):
    # Regression: "local t = {}" followed by "function t.x()" left t pending, so uses inside looked global.
    src = "local GetItemInfo = {}\nfunction ns.f()\n  return GetItemInfo[1]\nend\n"
    assert by_rule(lint(db, src), "FL001") == []


def test_secret_value_through_a_cached_local(db):
    # BigWigs: "local UnitHealth, UnitHealthMax = UnitHealth, UnitHealthMax" then compares the result.
    src = "local UnitHealth, UnitHealthMax = UnitHealth, UnitHealthMax\nlocal hp = UnitHealth(unit)\nif hp > 0 then end\n"
    f = by_rule(lint(db, src), "FL003")
    assert [(x.line, x.name) for x in f] == [(3, "UnitHealth")]


def test_secret_check_on_the_same_line_is_a_guard(db):
    src = "local hp = UnitHealth(unit)\nif E:NotSecretValue(hp) and hp > 0 then end\nif issecretvalue(hp) or hp > 1 then end\nif hp > 2 then end\n"
    assert [x.line for x in by_rule(lint(db, src), "FL003")] == [4]


def test_polyfill_in_another_file_suppresses(db):
    f = lint(db, "GetSpellInfo(1)", "GetSpellInfo = GetSpellInfo or function() end")
    assert by_rule(f, "FL001") == []


def test_guard_makes_a_note(db):
    f = by_rule(lint(db, "if GetMouseFocus then GetMouseFocus() end"), "FL001")
    assert f and all(x.severity == "note" for x in f)


def test_fallback_after_checking_the_replacement_is_a_note(db):
    src = "if C_Item and C_Item.GetItemSpell then return C_Item.GetItemSpell(1) end\nreturn GetItemSpell(1)"
    f = by_rule(lint(db, src), "FL001")
    assert len(f) == 1 and f[0].severity == "note" and f[0].extra.get("fallback")


def test_reassigned_cached_local_is_a_fallback(db):
    src = "local GetSpellBookItemInfo = GetSpellBookItemInfo\nif X then GetSpellBookItemInfo = function() end end"
    f = by_rule(lint(db, src), "FL001")
    assert f and f[0].severity == "note"


def test_namespace_member_and_missing_namespace(db):
    f = by_rule(lint(db, "C_Engraving.GetRuneCategories(false, false)\nC_Spell.GetSpellInfo(1)\nC_Timer.After(1, print)"), "FL001")
    assert [x.name for x in f] == ["C_Engraving.GetRuneCategories"]


def test_restricted_combat_log(db):
    f = lint(db, 'f:RegisterEvent("COMBAT_LOG_EVENT_UNFILTERED")\nlocal a = CombatLogGetCurrentEventInfo()')
    assert by_rule(f, "FL006")[0].severity == "warning"
    removed = by_rule(f, "FL001")[0]
    assert "not available to addons" in removed.message


def test_removed_event(db):
    f = by_rule(lint(db, 'self:RegisterEvent("ENGRAVING_MODE_CHANGED", "Handler")\nRegisterEvent(frame, "PLAYER_LOGIN")'), "FL005")
    assert [x.name for x in f] == ["ENGRAVING_MODE_CHANGED"] and f[0].severity == "error"


def test_project_id_comparisons(db):
    f = by_rule(
        lint(
            db,
            "a = WOW_PROJECT_ID == WOW_PROJECT_CLASSIC\nb = WOW_PROJECT_ID ~= WOW_PROJECT_MAINLINE\nc = 18 == WOW_PROJECT_ID\nd = t[WOW_PROJECT_ID]",
        ),
        "FL004",
    )
    assert [x.extra.get("evaluates") for x in f] == [False, True, True, None]
    assert f[3].severity == "note"
    aware = by_rule(lint(db, "if WOW_PROJECT_ID ~= WOW_PROJECT_MAINLINE and WOW_PROJECT_ID ~= WOW_PROJECT_CAMELOT then end"), "FL004")
    assert [x.severity for x in aware] == ["note", "note"]


def test_build_version_check(db):
    f = by_rule(lint(db, "if select(4, GetBuildInfo()) >= 100000 then end"), "FL007")
    assert f and f[0].extra["evaluates"] is False


def test_secret_values(db):
    src = """
local hp = UnitHealth("target")
local pct = hp / UnitHealthMax("target")
if UnitHealth("player") < 100 then end
bar:SetValue(UnitHealth("player"))
local ok = issecretvalue(UnitHealth("player"))
"""
    f = by_rule(lint(db, src), "FL003")
    assert sorted(x.line for x in f) == [3, 3, 4]


def test_deprecated_fallback_is_a_warning(db):
    name, info = next(iter(db.deprecated.items()))
    f = by_rule(lint(db, f"{name}()"), "FL002")
    assert f and f[0].severity == "warning" and info["file"] in f[0].message


def test_hooksecurefunc(db):
    f = by_rule(
        lint(
            db,
            'hooksecurefunc("QuestLog_Update", fn)\nhooksecurefunc(GameTooltip, "SetHyperlink", fn)\nif ToggleQuestLog then hooksecurefunc("ToggleQuestLog", fn) end',
        ),
        "FL001",
    )
    assert [x.name for x in f if x.severity == "error"] == ["QuestLog_Update"]


def test_suppression_comments(db):
    src = """-- forever-lint: ignore-next-line
GetSpellInfo(1)
GetItemInfo(1) -- forever-lint: ignore FL001
GetMouseFocus() -- forever-lint: ignore FL003
"""
    f = by_rule(lint(db, src), "FL001")
    assert [x.line for x in f] == [4]
    assert lint(db, "-- forever-lint: ignore-file\nGetSpellInfo(1)") == []


def test_method_and_dotted_function_definitions(db):
    src = (
        "function MyAddon:GetSpellInfo() end\nfunction MyAddon.GetItemInfo() end\nlocal o = {}\nfunction o.x(UnitAura) return UnitAura end"
    )
    assert by_rule(lint(db, src), "FL001") == []


def test_require_renamed_to_import(db):
    f = by_rule(lint(db, 'local m = require("mod")'), "FL001")
    assert f and "`import`" in f[0].message


def test_chat_hardware_event(db):
    src = 'C_ChatInfo.SendChatMessage("hi", "EMOTE")\nC_ChatInfo.SendChatMessage(msg, "GUILD")\nC_ChatInfo.SendChatMessage(msg)\nC_ChatInfo.SendChatMessage(fmt("%d", x), "yell")'
    f = by_rule(lint(db, src), "FL011")
    assert [x.extra["chatType"] for x in f] == ["EMOTE", "SAY", "YELL"]


def test_trial_accounts_note_once(db):
    f = by_rule(lint(db, 'C_ChatInfo.SendAddonMessage("P", "x", "RAID")', 'C_ChatInfo.SendAddonMessage("P", "y", "RAID")'), "FL012")
    assert len(f) == 1 and f[0].severity == "note"


def test_frame_strata_comparison(db):
    src = 'if frame:GetFrameStrata() == "HIGH" then end\nif "LOW" ~= self.bar:GetFrameStrata() then end\nlocal s = frame:GetFrameStrata()'
    f = [x for x in lint(db, src) if x.name == "GetFrameStrata"]
    assert [x.line for x in f] == [1, 2]


def test_beta_event_quirks_are_notes(db):
    src = 'f:RegisterEvent("PLAYER_DEAD")\nf:RegisterEvent("MERCHANT_CLOSED")\nf:RegisterEvent("ZONE_CHANGED_NEW_AREA")\nf:RegisterEvent("PLAYER_LOGIN")'
    f = by_rule(lint(db, src), "FL013")
    assert [x.name for x in f] == ["PLAYER_DEAD", "MERCHANT_CLOSED", "ZONE_CHANGED_NEW_AREA"]
    assert all(x.severity == "note" and "not confirmed" in x.message for x in f)

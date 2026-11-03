local addonName, ns = ...
local GetSpellInfo = GetSpellInfo
local GetAddOnMetadata = C_AddOns and C_AddOns.GetAddOnMetadata or GetAddOnMetadata
local isClassic = WOW_PROJECT_ID == WOW_PROJECT_CLASSIC
if select(4, GetBuildInfo()) >= 100000 then ns.retail = true end

function ns:Health(unit)
  local hp = UnitHealth(unit)
  local max = UnitHealthMax(unit)
  return hp / max
end

local f = CreateFrame("Frame")
f:RegisterEvent("COMBAT_LOG_EVENT_UNFILTERED")
f:RegisterEvent("ENGRAVING_MODE_CHANGED")
f:SetScript("OnEvent", function(self, event)
  local _, sub = CombatLogGetCurrentEventInfo()
  local name = GetItemInfo(19019)
  local runes = C_Engraving.GetRuneCategories(false, false)
  for i = 1, GetNumTalentTabs() do print(i) end
end)

-- a local shadowing a removed name is fine
local function GetContainerItemInfo(bag, slot) return nil end
GetContainerItemInfo(0, 1)

MyAddonDB = MyAddonDB or {}
local t = { GetSpellInfo = 1, UnitAura = true }  -- table keys are not references
print(GetMouseFocus()) -- forever-lint: ignore FL001
hooksecurefunc("QuestLog_Update", function() end)

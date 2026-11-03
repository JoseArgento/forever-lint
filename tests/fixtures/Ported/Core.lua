-- Polyfill: the addon defines the global itself, so uses are fine.
GetSpellInfo = GetSpellInfo or function(id)
  local info = C_Spell.GetSpellInfo(id)
  return info and info.name
end
local name = GetSpellInfo(133)

local function getMeta(addon, field)
  if C_AddOns and C_AddOns.GetAddOnMetadata then
    return C_AddOns.GetAddOnMetadata(addon, field)
  end
  return GetAddOnMetadata(addon, field)
end

local GetItemCount = C_Item.GetItemCount
print(GetItemCount(6948), UnitHealth("player"))
local bar = CreateFrame("StatusBar")
bar:SetValue(UnitHealth("player"))

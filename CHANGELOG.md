# Changelog

## 0.2.0 (2026-11-05)

- `--export-luals`: a LuaLS `---@meta` file with a stub for every function Forever has (deprecation fallbacks marked `---@deprecated`, secret-value functions annotated), for editors until vscode-wow-api and wowlua-ls get a Forever flavor.
- Compatibility index: 47 addons (was 15), and a shields.io endpoint badge per addon at `badge/<slug>.json`.
- API database rebuilt against the launch-day UI source 1.60.1 (70790): no API changes (Blizzard added documentation for `C_BattleNet.GetDefaultTitleWhisperTarget`; the client dump is still 70532). `tools/build_db.py` no longer takes `OnAccept = function` fields of StaticPopup tables for deprecation shims when run on a full checkout of the UI source.
- Launch day (4 November 2026): no new event quirks reported in the addon feedback threads, so FL013 is unchanged.

## 0.1.0 (2026-11-04)

First release, against WoW Forever 1.60.1 (70532) and its UI source 1.60.1 (70755).

- Rules FL001–FL013: removed and deprecated API (with the replacement when there is one), secret values, `WOW_PROJECT_ID`, `GetBuildInfo` version checks, removed and restricted events, TOC selection, interface version and game-type exclusions, SAY/YELL/EMOTE hardware events, trial accounts and addon messages, and events reported in the beta to behave differently (`PLAYER_DEAD` twice, stale zone text after travel).
- Follows the TOC Forever loads (game-type suffixes, per-file directives, XML includes).
- Scope-aware analysis: locals, aliases (`local load = loadstring`), polyfills, guards and checked replacements. Events registered from handler tables are checked too.
- Text, JSON and SARIF output; `--explain`; inline suppressions; GitHub Action.
- **Compatibility index** of popular addons, rebuilt weekly and published with GitHub Pages, with an **API migration table** (`api.html`, `api.json`) and a **"now works on Forever" RSS feed** (`feed.xml`, optional Telegram).
- Weekly workflow that refreshes the API database when Blizzard patches.

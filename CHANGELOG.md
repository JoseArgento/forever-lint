# Changelog

## 0.1.0 (2026-11-03)

First release, against WoW Forever 1.60.1 (70532) and its UI source 1.60.1 (70755).

- Rules from the 1.60.1 API notes (warcraft.wiki, Oct 2026): `require` renamed to `import` (FL001), `GetFrameStrata` secret values (FL003), SAY/YELL/EMOTE hardware events (FL011), trial accounts and addon messages (FL012).
- Rules FL001–FL010: removed and deprecated API, secret values, `WOW_PROJECT_ID`, `GetBuildInfo` version checks, removed and restricted events, TOC selection, interface version and game-type exclusions.
- Follows the TOC Forever loads (game-type suffixes, per-file directives, XML includes).
- Scope-aware analysis with polyfill, guard and checked-replacement detection.
- Text, JSON and SARIF output; `--explain`; inline suppressions; GitHub Action.

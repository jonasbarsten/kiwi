# Kiwi Pianoteq Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each preset slot can carry a Pianoteq preset; the page browses all exported presets by family, stars favourites, and steps prev/next through the favourites.

**Architecture:** `install.sh` exports Pianoteq's presets as LV2 bundles (outside `LV2_PATH`) and generates `web/presets.json` (uri, name, family, bundle). A slot document's `preset` field holds the URI; the `Applier` emits a `('preset', uri)` op (after the reverb, before Pianoteq parameters) and `execute_ops` runs `bundle_add` + `preset_load` + Pianoteq's reverb switch off. Favourites are global, in `~/.local/state/kiwi/favourites.json`, written only when a star is toggled.

**Spec:** `docs/superpowers/specs/2026-10-03-kiwi-webui-design.md` §5 and `docs/superpowers/specs/2026-10-03-preset-spike.md`; slots per `2026-10-04-kiwi-presets-design.md`.

## Global Constraints

- URIs are `file://` + percent-encoded real path; `bundle_add` takes the percent-encoded directory with a trailing `/` (verified in the spike).
- No disk writes while playing except save/select and the favourites file on a star toggle.
- A slot without a preset leaves the piano as it is, unless `kiwi.patch` names a default preset (`preset_load 0 <uri>` line), which then is the baseline.
- The preset is never morphed; Pianoteq parameters stored in a slot apply on top of its preset.

## Review Focus

1. Switching slots with different presets must load the right preset before the slot's Pianoteq parameters: `test_preset_op_precedes_patch_params` (Task 2).
2. A preset from an unknown URI (stale favourites, deleted export) must fail cleanly: `test_unknown_preset_is_refused` (Task 3).
3. Favourites must survive a reboot and a corrupt file: `test_favourites_roundtrip_and_corrupt` (Task 3).
4. Prev/next with no favourites must still do something sensible (steps through the current family): page logic (Task 4).
5. The boot restore applies the slot's preset: `test_restore_applies_preset` (Task 3).

## Tasks

1. **Preset index + patch parser** — `web/kiwi_web/presets.py` (`index_presets(root)`, `encode_path`, `load_commands(uri, bundle)`), `patchfile.py` (`preset_load` lines → `patch.presets`), tests; `web/tools/gen_presets.py`.
2. **Applier** — `default_preset` baseline, `('preset', uri)` ops, morph carries the current preset; tests.
3. **Server** — `execute_ops` preset handling (bundle map from `presets.json`), `/presets.json`, `POST /preset {uri}`, `POST /favourite {uri, on}`, model `preset:current`/`preset:favourites`, favourites store, restore; tests with a FakeHost that understands `bundle_add`/`preset_load`.
4. **Page** — preset bar in the Pianoteq section: name, ◀ ▶ (favourites, else current family), browser with family select, search, ★.
5. **Install, docs, device** — export with a version marker, `gen_presets`, README/spec; verify on the Pi: switch presets via the page, slot with preset restored after host restart, `kiwi-stress 120` while switching → acceptable.

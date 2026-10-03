# Pianoteq preset spike (web UI phase 3 gate)

Date: 2026-10-03
Question: can the web UI switch Pianoteq presets live under mod-host without dropouts?

## What was done

1. **Export** (Pi, nice 19 / idle I/O, no JACK access so it cannot join the audio graph):
   `"/home/patch/.vst/Pianoteq 8" --headless --export-lv2-presets ~/kiwi-data/pianoteq-presets --export-presets-filter all`
   → 2 s, exit 0, 24 bundles (one per instrument family plus `My Presets`), 654 presets, 2.9 MB.
   Bundle names contain spaces and non-ASCII (`Pianoteq 8-factory-presets-Steinway D.lv2`,
   `…-Blüthner.lv2`). Each bundle has a `manifest.ttl` with a `pset:Bank` (family label) and one
   `<name>.ttl` per preset (`rdfs:label` = display name, state = a JUCE binary blob).
2. **Make known to mod-host** without restart: `bundle_add <dir>/` → `resp 0` in 3–10 ms.
3. **Load**: `preset_load 0 <uri>` where `<uri>` is the **real path, percent-encoded**:
   `file:///home/patch/kiwi-data/pianoteq-presets/Pianoteq%208-factory-presets-Steinway%20D.lv2/HB_Steinway_D_Blues.ttl`
   → `resp 0`. URIs through a symlinked, space-free bundle path were rejected with `resp -105`
   (lilv resolves the real path). mod-host splits commands on spaces, so the `bundle_add`
   argument must be percent-encoded too (it was accepted that way).
4. **Switch under worst-case load**: `host/kiwi-stress 30` (sustain held, 8-note chords every
   0.4 s) while loading 5 presets 4 s apart, alternating Steinway D and Electric (MKI/MKII):
   each load answered in 11–14 ms, **0 xruns / process errors**. No mute sequence was used.
5. Afterwards `patch_set 0 …Reverb_20Switch 0` (presets carry their own reverb setting), and a
   `kiwi-host` restart returned the instance to its default preset.

## Not verified here

- Audibly that each preset took effect, and how unlicensed instrument families behave
  (Pianoteq plays them in demo mode with some notes muted). Needs the user's ears.
- Whether the state restore finishes asynchronously after `resp 0` (a short audible change
  delay is possible, not a dropout).

## Recommendation

**Ship** prev/next/browse, with:

- Presets exported by `install.sh` into `~/kiwi-data/pianoteq-presets` (outside `LV2_PATH`),
  re-exported when the Pianoteq version changes; `bundle_add` with percent-encoded paths after
  each host start, before the first load.
- URIs built from the real path, percent-encoded; names, families and order from the bundle
  manifests (`pset:Bank` label = family).
- After each load: `patch_set` Pianoteq's reverb switch off again (the shared reverb is used).
- The mute → load → unmute sequence from the spec is **not needed** for dropouts; keep it out
  unless listening shows a click on switching.
- Favourites drive prev/next (user decision); the current preset is autosaved and restored by
  `kiwi-restore` (which then also needs the `bundle_add`).

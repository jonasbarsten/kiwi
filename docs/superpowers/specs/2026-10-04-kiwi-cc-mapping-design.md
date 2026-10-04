# kiwi: CC mapping, panic, baseline preset

Date: 2026-10-04. Status: approved in conversation, implemented inline.

## What

1. **CC mapping from the page.** A *Map* button in the header enters map mode. In
   map mode controls do not move; tapping a control marks it "waiting for CC…" and the
   next control change that arrives (any channel) is bound to it. Tapping anywhere
   else, or Map again, leaves map mode. Mapped controls carry a `CC n` badge; in map
   mode a ✕ next to the badge removes the mapping.
2. **Many targets per CC, one CC per target.** A CC may drive several controls at once.
   Mapping a control that already has a CC replaces that control's mapping.
3. **Targets**: every port control (mix, carrier, synth, limiter, reverb), every
   Pianoteq parameter, the sampler envelope (CC 102–105), and the actions *Panic*,
   *Morph*, *Next slot*, *Previous slot*, *Save slot*.
4. **Panic**: a header button (and action) sending All Sound Off (CC 120) and All
   Notes Off (CC 123) on all 16 channels into Midi Through, which every instrument
   hears.
5. **Baseline Pianoteq preset**: `kiwi.patch` loads *Ant. Petrof Warm* explicitly, so a
   slot without a preset means that preset rather than "whatever was loaded".
6. **Reset to patch** is removed (the user does not want a "clean starting point").

## Where it runs

- Port targets map inside mod-host (`midi_map <inst> <sym> <ch> <cc> <min> <max>`,
  `midi_unmap <inst> <sym>`): audio-thread, no added latency. The service re-reads the
  port after each CC so RAM follows (as the fixed knobs did). The reverb instance is
  recreated on a reverb switch, so its mappings are re-applied after one.
- Pianoteq parameters and the sampler envelope have no mod-host ports: the service
  forwards (CC value scaled into the parameter's range → `patch_set` / sampler CC),
  the same path the page's sliders use. While such a mapping exists the host link stays
  connected so the first CC is not delayed by a reconnect.
- Actions: Morph takes the CC value (0–127 → 0–1); the others fire on value ≥ 64.

## Storage

`~/.local/state/kiwi/ccmap.json`: `{"mappings": [{"channel": 0, "cc": 20, "target":
{...}}, ...]}`, written atomically only when a mapping changes. With no file the map
is the former fixed one (mix/carrier knobs on CC 20–26, morph on CC 27). The
`midi_map` lines leave `kiwi.patch`; kiwi-restore applies the port mappings at boot.

Target shapes: `{"kind":"port","instance":5,"symbol":"piano_vol"}`,
`{"kind":"patch","instance":0,"uri":"…:Volume"}`, `{"kind":"cc","instance":1,"number":102}`,
`{"kind":"action","name":"panic"}`.

## Interface

- `GET /params.json`: `cc_map` becomes the live list of mappings (page badges).
- Model keys: `map:mappings` (list, published on every change), `map:learning`
  (the target waiting for a CC, or null).
- `POST /map/learn {target}` → 204 (409 if the target is unknown), `POST /map/cancel`,
  `POST /map/remove {target}`; `POST /panic`.
- `midi:cc:<n>` keeps feeding the CC bars for mapped CCs.

## Tasks

1. `kiwi_web/ccmap.py` (store, defaults, validation) + tests.
2. `patchfile.py` drops `midi_map`; `kiwi.patch` drops the knob map, gains the baseline
   preset lines.
3. `server.py`: mappings replace `cc_map`/`cc_targets`; learn/remove/cancel/panic
   endpoints; forwarding and actions in `on_midi`; map application through the link
   (and after a reverb switch); restore applies port maps; reset removed. Fake host
   learns `midi_map`/`midi_unmap`. Tests.
4. `static/index.html`: header Map + Panic, map mode, badges, ✕, footer without Reset.
5. README.

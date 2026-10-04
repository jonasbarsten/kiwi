# Kiwi presets — design

Date: 2026-10-04
Status: Draft for review
Builds on: `2026-09-29-kiwi-design.md` (instrument), `2026-10-03-kiwi-webui-design.md` (web UI).

## 1. Goal

Make the box safe to unplug at any moment and give it a stage-friendly way to store
sounds: **eight preset slots**, edited in RAM, saved only on purpose. Nothing writes to the
SD card while playing, so a pulled plug meets an idle filesystem.

### Decisions taken with the user (2026-10-04)

| Topic | Decision |
|---|---|
| Disk writes while playing | None. Parameter changes live in RAM. Autosave is removed. |
| Saving | Hold the Pisound button (≥ 3 s) → the current RAM state is written into the current slot. |
| Selecting | Single click → next slot; double click → previous slot. The page can pick any slot. |
| Boot | Loads the **last selected** slot. Selecting writes one tiny file (`current`) atomically; this is the only write outside saving. |
| Slots | 8. |
| Morph | A CC crossfades between the current slot and the next one (continuous parameters only). |
| Logs | The system journal lives in RAM with a hard cap, so weeks of uptime cannot fill it. |

### Success criteria

- `kiwi-stress 600` while turning knobs and morphing: no file under `/home` or `/var` is
  modified (verified with `find -newer`), and 0 xruns.
- Click, double click and hold do what the table says, with LED feedback, with no page open.
- A reboot (or `kiwi-host` restart) comes up in the last selected slot with its saved values.
- The journal never exceeds 32 MB and is gone after a reboot; the 2.8 GB persistent journal
  on the card is removed by `install.sh`.
- The page shows the slot, lets you rename and save it, and reflects button actions live.

## 2. Storage

```
~/.local/state/kiwi/
  current            "3\n"            the last selected slot (1–8)
  presets/1.json … 8.json             one full state document per slot
```

A slot document is the existing state schema (`params`, `patch_params`, `reverb`,
`reverb_params`, `cc`, later `preset`) plus `"name": "Preset 3"`. Missing slot files mean
"the patch defaults". Writes are atomic (temp file + `os.replace` + fsync) and happen only in
`save` (one slot file) and `select` (`current`). The old `state.json` is migrated once into
slot 1 and renamed `state.json.migrated`.

## 3. State model in `kiwi-web`

- `StateStore` becomes RAM-only (`due`/`flush` and the debounce go away); it gains
  `name`, `slot`, `save(slot)`, `load_slot(slot)`, `select(slot)`.
- `HostLink` keeps applying changes to mod-host as today; nothing is written.
- **Select slot n**: write `current`; load `presets/n.json` (or empty); apply it fully:
  reverb switch if the reverb differs, every stored port/patch/CC value, and the patch
  baseline for every value the previous RAM state had set but the slot does not (so leftovers
  from the previous slot do not bleed in). Blink the LED n times (short).
- **Save**: write RAM into `presets/<current>.json`. One long blink.
- **Reset to patch**: RAM back to the patch baselines (unchanged behaviour, no file removed).
- Model keys: `slot:current` (int), `slot:name`, `slot:names` (list of 8), `slot:dirty`
  (RAM differs from the saved slot), `morph:value` (0–1).

## 4. Button and LED

`/etc/pisound.conf` is replaced by `system/pisound.conf` (installed by `install.sh`, which
restarts `pisound-btn`); Patchbox's defaults (hotspot toggle, shutdown, Pure Data) are gone.

| Event | Script | Action |
|---|---|---|
| CLICK_1 | `kiwi-btn next` | next slot (wraps 8 → 1) |
| CLICK_2 | `kiwi-btn prev` | previous slot (wraps 1 → 8) |
| CLICK_3, CLICK_OTHER | `do_nothing.sh` | — |
| HOLD_1S, HOLD_OTHER | `do_nothing.sh` | too short: avoids accidental saves |
| HOLD_3S, HOLD_5S | `kiwi-btn save` | save RAM into the current slot |
| DOWN / UP | Patchbox's `down.sh` / `up.sh` | the existing press blink |

`host/kiwi-btn` (bash, runs as root from `pisound-btn`) posts to the web server on
loopback: `curl -s -m 3 -X POST http://127.0.0.1/slot/next|prev|save`. Loopback POSTs are
exempt from the `Origin` check (they can only come from the Pi itself). LED feedback is done
by `kiwi-web` writing the blink count to `/sys/kernel/pisound/led`; `install.sh` installs a
tmpfiles rule making that file writable by group `audio`. If `kiwi-web` is down the button
does nothing and `kiwi-btn` logs it.

## 5. Morph

- **CC 27** (next free after the knobs), MIDI channel 1, 0–127 → `t` in 0–1. A `/morph`
  endpoint and a slider on the page do the same thing.
- A = the current slot as saved, B = the next slot as saved (`presets/<n+1>.json`, wrapping).
  For every **continuous** parameter present in the patch metadata, value = A + (B − A) · t,
  where a slot without a stored value contributes its patch baseline.
- Included: `kiwi mix`, `kiwi carrier`, the limiter, the vocoder quality, sampler ports and
  envelope CCs, amsynth's float parameters, and the current reverb's parameters when A and B
  use the same reverb. Excluded: toggles, enums and integers, the reverb choice, every
  Pianoteq parameter and the Pianoteq preset.
- The morph only changes RAM. Saving while morphed stores the morphed values.
- Selecting a slot re-anchors A and B; the knob's next movement morphs from the new A.
  (CC knobs are absolute, so the first move after a slot change jumps to the knob's position.)
- `kiwi-web` must hear CC 27 with no page open, so the MIDI monitor (`aseqdump`) now runs
  permanently. It costs nothing measurable at idle priority. Morph changes wake the host link
  like any other change; it still disconnects 10 s after the last need.

## 6. Journal

`system/journald.conf` → `/etc/systemd/journald.conf.d/kiwi.conf`:
`Storage=volatile`, `RuntimeMaxUse=32M`, `RuntimeMaxFileSize=8M`, `Compress=yes`.
`install.sh` removes `/var/log/journal` (the persistent journal, currently 2.8 GB) and
restarts `systemd-journald`. Logs survive until reboot, which covers debugging a gig.

## 7. Page

A **preset bar** under the header, on every layout: `◀  3 · Warm Rhodes  ▶`, a **Save**
button (highlighted when `slot:dirty`), and a sheet listing the 8 slots with editable names
(a name change is RAM until saved). A **Morph** slider in the bar mirrors CC 27. The footer's
"Reset to patch" stays (RAM only). Button actions on the box update the bar through the
event stream.

## 8. Boot

`kiwi-restore` becomes "load the current slot": it reads `current` and
`presets/<n>.json` and applies them exactly as a page-driven select would (reverb switch,
params, CCs). With no slot files it does nothing.

## 9. Testing

- Unit: slot save/load/select (atomic, missing files, migration from `state.json`),
  "leftovers from the previous slot are reset", morph math (t = 0 → A, t = 1 → B, excluded
  kinds untouched, reverb params only when the reverb matches), loopback Origin exemption,
  `/slot/*` and `/morph` endpoints, restore from `current`.
- On device: button click/double/hold with no page open (LED blinks, `current` changes,
  `journalctl` shows the actions); `find ~ /var -newer <marker>` empty after 10 minutes of
  playing, knob turning and morphing; reboot lands in the selected slot; journal size cap.

## 10. Risks

- Applying a slot sends ~40 commands (more with Pianoteq parameters) in one burst while audio
  runs; a reverb switch cuts the tail. Measured per-switch blips are rare; a slot change is
  meant to happen between songs.
- A crash of `kiwi-host` loses unsaved RAM edits (the slot is reloaded). Intended.
- `pisound-btn` runs the scripts as root; `kiwi-btn` only talks to loopback HTTP.

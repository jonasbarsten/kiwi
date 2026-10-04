# Kiwi Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eight preset slots edited in RAM and saved only on purpose (Pisound button hold or page), selected by button click/double-click or page, morphed with CC 27, loaded at boot from the last selection; no SD-card writes while playing, journal in RAM.

**Architecture:** `StateStore` becomes a RAM document with explicit `select`/`save` to `~/.local/state/kiwi/presets/<n>.json` + `current`. A pure `Applier` turns (current RAM document, target document) into mod-host/CC operations, used by slot selection, morph and boot restore. The Pisound button runs `kiwi-btn-*` scripts that POST to `kiwi-web` on loopback; the LED answers. The MIDI monitor runs permanently so CC 27 works without a page.

**Tech Stack:** Python 3 stdlib, bash, systemd, pisound-btn, `/sys/kernel/pisound/led`.

**Spec:** `docs/superpowers/specs/2026-10-04-kiwi-presets-design.md`

## Global Constraints

- No disk writes from `kiwi-web` except `save` (one slot file) and `select` (`current`), both atomic.
- Loopback POSTs skip the `Origin` check; everything else keeps the existing checks.
- Morph includes only `type == 'float'` port params, sampler CCs, and the current reverb's float params when both slots use that reverb; never Pianoteq, toggles, enums, ints or the reverb choice.
- Test command: `python3 -m unittest discover -s web/tests -t web` (repo root). Deploy: the usual rsync + `./install.sh`.
- Push to `main` is allowed.

## Review Focus

1. **Unplugging mid-gig**: after 10 minutes of playing, knob turning and morphing, no file under `~` or `/var` is newer than a marker (Task 7).
2. **Slot bleed**: a value changed in slot 2 must not persist after selecting slot 3 which never set it — `test_select_resets_leftovers` (Task 2).
3. **Button with no page open**: click/double/hold must work when `kiwi-web` holds no mod-host connection — on-device step (Task 7) and `test_slot_endpoints_from_loopback_without_origin` (Task 4).
4. **Morph must never touch excluded parameters**: `test_morph_excludes_non_continuous` (Task 3).
5. **Boot into the last selected slot**, including its reverb and CCs: `test_restore_applies_current_slot` (Task 5).

---

## File Structure

```
web/kiwi_web/state.py        RAM document + slots (select/save/load/migrate)
web/kiwi_web/applier.py      pure: documents → operations; morph interpolation
web/kiwi_web/server.py       HostLink select/save/morph; /slot/* and /morph; restore from current slot
web/kiwi_web/led.py          Pisound LED blink
web/static/index.html        preset bar, slot sheet, morph slider
host/kiwi-btn                button script (action from $0: kiwi-btn-next|prev|save)
system/pisound.conf          button mapping
system/journald-kiwi.conf    journal in RAM, capped
system/kiwi-tmpfiles.conf    LED writable by audio group
install.sh                   journal, button, LED, symlinks
README.md, spec             docs
```

---

### Task 1: State store — RAM document with slots

**Files:** `web/kiwi_web/state.py`, `web/tests/test_state.py`

**Produces:** `StateStore(dir, slots=8)` with `.data` (adds `name`), `.slot`, `.dirty`, `load()` (reads `current`, migrates `state.json` → slot 1), `read_slot(n) -> dict`, `select(n)` (writes `current`, loads slot into RAM), `save()` (writes slot file), `names() -> list`, `set_name(name)`, `clear()` (RAM to empty, keeps name/slot), the existing `set_*` (all mark `dirty`). `due`/`flush` removed. `StateStore.empty()` classmethod returns an empty document.

- [ ] Rewrite `web/tests/test_state.py`: keep the value-setting tests (adapt: `StateStore(dir)`), add `test_migrates_state_json_into_slot_1`, `test_select_writes_current_and_loads_slot`, `test_save_writes_slot_atomically`, `test_missing_slot_is_empty`, `test_names_default`, `test_dirty_flag`, `test_corrupt_slot_loads_empty`, `test_no_writes_outside_save_and_select` (set values, assert the directory mtime/listing unchanged).
- [ ] Run → fail. Implement. Run → pass. Commit `"Presets: RAM state with slots"`.

### Task 2: Applier — documents to operations

**Files:** `web/kiwi_web/applier.py`, `web/tests/test_applier.py`

**Produces:**
```python
class Applier:
    def __init__(self, meta): ...   # baselines, types, cc params, reverb baselines, default reverb
    def ops(self, current, target, replace):
        """[('reverb', id, settings) | ('port', inst, sym, v) | ('patch', inst, uri, v) | ('cc', n, v)]
        current/target are state documents. replace=True also resets keys present in
        current but not in target to their baseline."""
    def effective_reverb(self, doc): ...
    def morph(self, a, b, t, reverb_id): -> target document with continuous keys only
```
Order: reverb first (when it changes), then ports, patch params, CCs. A reverb change carries the target's settings for that reverb, so no separate port ops for instance 6; if the reverb is unchanged, reverb param diffs are port ops on instance 6 (with reverb baselines).

- [ ] Tests: `test_ops_sets_target_values`, `test_select_resets_leftovers` (current has `5:piano_vol`, target not → op to baseline 0.8), `test_reverb_switch_op_when_reverb_differs`, `test_reverb_param_diff_when_same_reverb`, `test_cc_ops`, `test_morph_interpolates_floats` (t=0 → A, t=1 → B, t=0.5 midpoint; missing values use baselines), `test_morph_excludes_non_continuous` (toggle/enum/int/patch/reverb choice untouched), `test_morph_reverb_params_only_when_same_reverb`.
- [ ] Run → fail. Implement. Run → pass. Commit `"Presets: applier"`.

### Task 3: LED

**Files:** `web/kiwi_web/led.py`, `web/tests/test_led.py`

`Led(path='/sys/kernel/pisound/led').blink(n)` writes `str(n)`; unusable path → returns False, logged once. Test with a temp file; missing path.

- [ ] Test → fail → implement → pass. Commit `"Presets: LED"`.

### Task 4: Server — slots, morph, endpoints, permanent monitor

**Files:** `web/kiwi_web/server.py`, `web/tests/test_server.py`, `web/tests/fakehost.py` (no change expected)

- `HostLink`: holds `Applier`, `Led`; `request_select(n)`, `request_save()`, `request_morph(t)`, `request_name(name)`; `_apply_select`: `prev = copy(store.data)`; `store.select(n)`; `ops = applier.ops(prev, store.data, replace=True)`; execute; model `slot:*`; blink n. `_apply_save`: `store.save()`; model `slot:dirty` False; blink 1. `_apply_morph`: `a = store.read_slot(slot)`, `b = store.read_slot(next)`, `target = applier.morph(a, b, t, current_reverb)`; `ops(store.data, target, replace=False)`; execute; write values into store (dirty). `_apply_reset` → `applier.ops(store.data, empty, replace=True)` then `store.clear()`.
- `_execute(ops)`: runs each op through the client / `send_cc`, updates the model (`port:`, `patch:`, `cc:`, `reverb:current`), reads back the reverb after a switch.
- Remove the autosave flush from `run()`; `_needed()` includes pending select/save/morph/name.
- `App`: monitor starts in `__init__` and stays; `open_stream`/`close_stream` only count viewers. `on_midi`: CC 27 (channel 0) → `link.request_morph(value / 127)`.
- `Handler.do_POST`: skip the Origin check when `client_address[0]` is loopback. Routes: `/slot/next`, `/slot/prev`, `/slot/select {n}`, `/slot/save`, `/slot/name {name}` (≤ 40 chars), `/morph {value}`.
- Model keys at start: `slot:current`, `slot:name`, `slot:names`, `slot:dirty`, `morph:value`.

- [ ] Tests: `test_slot_select_applies_slot_and_resets_leftovers` (end to end through the fake host), `test_slot_save_writes_file_and_clears_dirty`, `test_next_prev_wrap`, `test_slot_endpoints_from_loopback_without_origin`, `test_morph_via_cc27` (`app.on_midi` CC 27 → fake host gets the interpolated value), `test_morph_endpoint`, `test_no_file_writes_on_param_change` (set a value, wait, directory listing unchanged), `test_monitor_runs_without_viewers`. Keep the earlier tests that still apply; drop the autosave ones.
- [ ] Run → fail → implement → pass. Commit `"Presets: slots, morph and endpoints"`.

### Task 5: Boot restore from the current slot

**Files:** `web/kiwi_web/server.py` (`restore`), `web/tests/test_server.py`

`restore(args)`: `store.load()`; `ops = applier.ops(empty, store.data, replace=False)`; nothing → "nothing to restore"; else connect, execute (reverb switch + params + CCs), log counts, exit 0.

- [ ] `test_restore_applies_current_slot` (current=2, slot 2 has reverb calf + params + cc; fake host and midi file get them) → fail → implement → pass. Commit `"Presets: boot restore from the current slot"`.

### Task 6: Page

**Files:** `web/static/index.html`

- Preset bar under the header: `◀` `3 · Warm Rhodes` `▶`, **Save** (class `on` when dirty), a `details` sheet listing the 8 slots (tap to select; the current one has an editable name field that posts `/slot/name` on change), a **Morph** slider (POST `/morph`, echo from `morph:value`).
- Apply `slot:*` and `morph:*` keys from the event stream.
- Mobile first: bar wraps to two lines at 360 px; targets ≥ 44 px.

- [ ] Check at 360 and 1280 px in the browser; commit `"Presets: page"`.

### Task 7: Button, journal, install, docs, on-device verification

**Files:** `host/kiwi-btn`, `system/pisound.conf`, `system/journald-kiwi.conf`, `system/kiwi-tmpfiles.conf`, `install.sh`, `README.md`, spec

`system/pisound.conf`:
```
CLICK_1     /usr/local/bin/kiwi-btn-next
CLICK_2     /usr/local/bin/kiwi-btn-prev
CLICK_3     /usr/local/pisound/scripts/pisound-btn/do_nothing.sh
CLICK_OTHER /usr/local/pisound/scripts/pisound-btn/do_nothing.sh
HOLD_1S     /usr/local/pisound/scripts/pisound-btn/do_nothing.sh
HOLD_3S     /usr/local/bin/kiwi-btn-save
HOLD_5S     /usr/local/bin/kiwi-btn-save
HOLD_OTHER  /usr/local/pisound/scripts/pisound-btn/do_nothing.sh
DOWN        /usr/local/pisound/scripts/pisound-btn/system/down.sh
UP          /usr/local/pisound/scripts/pisound-btn/system/up.sh
CLICK_COUNT_LIMIT 2
```
`host/kiwi-btn`: `action=${0##*kiwi-btn-}`; `curl -s -m 3 -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1/slot/$action` → log via `logger -t kiwi-btn`.
`install.sh` "Presets" section: journald drop-in + remove `/var/log/journal` + restart journald; `pisound.conf` + restart `pisound-btn`; symlinks `/usr/local/bin/kiwi-btn-{next,prev,save}` → `~/kiwi/host/kiwi-btn`; tmpfiles rule `z /sys/kernel/pisound/led 0664 root audio -` + `systemd-tmpfiles --create`.

- [ ] Deploy; verify: `journalctl --disk-usage` small and `/var/log/journal` gone; press the button (ask the user) or simulate with the scripts; `cat ~/.local/state/kiwi/current` changes; LED blinks; `find ~ /var -newer /tmp/marker` empty after knob turning + morph + `kiwi-stress 300`; reboot → selected slot restored; `kiwi-check` 0 problems.
- [ ] README (Presets section; button table; journal; what Reset means now; remove autosave text), spec deviations. Commit `"Presets: button, journal, install, docs"`.

### Task 8: Final review

Fresh reviewer on the branch range; fix Critical/Important; ledger the rest.

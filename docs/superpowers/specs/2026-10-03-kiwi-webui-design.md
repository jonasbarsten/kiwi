# Kiwi web UI — design

Date: 2026-10-03
Status: Draft for review
Builds on: `docs/superpowers/specs/2026-09-29-kiwi-design.md` (the instrument).

## 1. Goal

A web page served by the Pi, reachable from a laptop or phone on either network, that
**monitors and controls** the instrument live: MIDI input activity, the seven knob
parameters, the other plugins' parameters, and Pianoteq presets (browse, prev/next,
favourites). It must never disturb audio: the instrument runs at 128 frames and only
just fits its CPU budget.

### Success criteria

- With no browser open, the web service uses no CPU and holds no connection to `mod-host`.
- With one or two pages open, `host/kiwi-stress 600` still reports 0 xruns.
- Moving a physical knob (CC 20–26) is reflected on the page within ~200 ms.
- Moving a slider on the page changes the sound within ~100 ms.
- Changes survive a reboot (autosave); "Reset" returns to `host/kiwi.patch`.
- Prev/next steps through favourite Pianoteq presets without an audible dropout, or the
  feature is not shipped (see §7).

### Decisions taken with the user (2026-10-03)

| Topic | Decision |
|---|---|
| Persistence | **Autosave**: every change is stored and restored at boot. |
| Network | **Both** wired (192.168.1.126) and Wi-Fi (192.168.0.40), port **80**, `http://patchbox.local/`. |
| Preset prev/next | Steps through **favourites** (starred presets). |
| Pianoteq parameters | **Curated** set on the main page + an **advanced page** with all 222. |

### Defaults chosen here (open for the user to change in review)

- No login; LAN-only (requests from non-private source addresses are refused).
- Routing-critical parameters are not exposed: TalkBox `carrier`/`wet`/`dry`, the reverb's
  `dry_level`, Pianoteq's own reverb switch.
- The CC map stays in `host/kiwi.patch`; the UI shows it but does not edit it.
- MIDI Program Change does not switch presets in v1.

## 2. Evidence this design rests on

From `mod-host` source (moddevices/mod-host) and the device (mod-host 1.13.0):

- **One client at a time** on port 5555: a second client's connection opens but is never
  served until the first disconnects. The boot loader (`kiwi-load`) and the web server must
  not hold it at the same time.
- **No pipelining**: one command per send, wait for `resp` before the next.
- **No feedback port** (`-f`): with it, mod-host blocks every command client until a feedback
  client also connects, and a dead feedback client can make mod-host busy-loop. We stay without it.
- `param_get` reads control input ports, including values written by `midi_map` (CC knobs).
  So knob moves can be read back without `-f`.
- `patch_get` (Pianoteq's parameters) only answers through the feedback port, so **Pianoteq
  values cannot be read back**. The UI shows them as unknown until set from the UI (or restored
  from autosave).
- `preset_load` restores plugin state from mod-host's socket thread while the plugin is
  running: **not real-time safe**, must be tested for dropouts.
- Port 5555 is already restricted to local clients (`IPAddressAllow=localhost` on `kiwi-host`).

## 3. Architecture

```
browser ── GET  /            index.html (inline CSS/JS/SVG, no CDN, gzip, ETag)
        ── GET  /events      Server-Sent Events: snapshot, then deltas (≤ 10/s)
        ── POST /set         {instance, symbol | uri, value}
        ── POST /preset      {uri} | {step: +1 | -1}
        ── POST /favourite   {uri, on}
        ── POST /reset
            │
kiwi-web  (python3 stdlib only; SCHED_IDLE, nice 19, idle I/O, MemoryMax=64M)
  ├─ HTTP server: ThreadingHTTPServer on :80, at most 4 event streams
  ├─ host link: owns the single 5555 connection; strict lockstep queue;
  │             coalesces sets per (instance, parameter): latest value wins
  ├─ MIDI monitor: `aseqdump -p "Midi Through"` child process, only while a page is open
  ├─ state store: ~/.local/state/kiwi/state.json (autosave)
  └─ static data: web/params.json (parameter metadata), preset index (generated)
```

### 3.1 mod-host connection rules

- `kiwi-web` connects to 5555 **only while at least one page is open** (or a POST arrives),
  and only when both `kiwi-patch.service` and `kiwi-restore.service` (§3.3) have finished,
  so it never competes with them for the single connection. It disconnects 10 s after the
  last page closes.
- On EOF (mod-host restarted), it marks the state "host restarting", waits for
  `kiwi-restore` to finish again, reconnects and re-reads. Replaying the saved state is
  `kiwi-restore`'s job only.
- Never closes the socket while a reply is outstanding. Response timeout 60 s.

### 3.2 What is read, and when (only while a page is open)

- On connect: `param_get` for every exposed control port (~80 commands, once).
- CC knobs: no polling. The MIDI monitor sees CC 20–26; the server then `param_get`s the
  mapped parameter (coalesced, ≤ 20/s). The CC → parameter map is parsed from the `midi_map`
  lines of `host/kiwi.patch`.
- Safety resync of the seven mapped parameters every 5 s; `cpu_load` once a second;
  temperature every 5 s from `/sys/class/thermal/thermal_zone0/temp`.

### 3.3 Restoring state at boot

The autosaved state must be applied even when no page is open. A small oneshot,
`kiwi-restore.service` (`After=kiwi-patch.service`, `PartOf=kiwi-host.service`), replays
`state.json` through the same lockstep client code (`kiwi-web --restore`) and exits; it is
skipped when the file is absent. It runs once per host start, so restarts restore too.

## 4. Parameters

Metadata is generated once at development time (from `lv2info` on the Pi) into
`web/params.json` and committed; the server never runs lilv.

| Inst | Plugin | Exposed | Not exposed |
|---|---|---|---|
| 5 | kiwi mix | piano/sampler/vocoder volume + reverb (CC 20–25) | — |
| 3 | kiwi carrier | blend 0–2 (CC 26) | — |
| 6 | Dragonfly Plate | decay, wet (`early_level`), predelay, algorithm, width, low cut, high cut, damping | `dry_level` (send bus) |
| 7 | x42 dpl | threshold, gain, release, true-peak | `enable` |
| 4 | TalkBox | quality | carrier / wet / dry |
| 1 | sfizz | volume, tuning frequency, stretched tuning, sustain cancels release, sample quality, oscillator quality | voices, oversampling, preload size |
| 2 | amsynth | all 41 controls, grouped (oscillators, filter, envelopes, LFO, portamento, effects, master) | — |
| 0 | Pianoteq | Curated: Volume, Dynamics, Condition, Post Effect Gain, Unison, Hammer Hardness P/M/F, Direct Sound, Sympathetic Resonance, Stereo Width, Lid Position, Damping Duration, Diapason. Advanced page: all 222 grouped as in its TTL | Reverb switch |

Pianoteq parameters are normalized 0–1 with no units. Sends are throttled to ≤ 10/s per
parameter, and the voicing/design/output groups send on release only (model changes may be
expensive inside the audio thread).

## 5. Pianoteq presets

- **Export** (one-time, `install.sh`): the Pianoteq standalone exports all presets as LV2
  presets into `~/kiwi-data/pianoteq-presets`, **outside** `LV2_PATH` so mod-host's boot does
  not parse ~650 preset files. Skipped when already exported for the installed Pianoteq version.
- **Index**: `kiwi-web` builds a list (URI, name, family) from the exported manifests,
  ordered like `--list-presets` (654 presets, ~60 families today).
- **Loading**: before the first load after each host start, `bundle_add <dir>`. Switch:
  mute the piano channel (`param_set 5 piano_vol 0`) → wait 40 ms → `preset_load 0 <uri>` →
  turn Pianoteq's reverb off again → wait 40 ms → restore the piano volume (re-read first).
- **UI**: current preset name; ◀ prev / next ▶ through favourites; family dropdown; search;
  ★ to add/remove favourites. Favourites are stored in the autosave file.
- The current preset is part of the autosaved state and is restored at boot.

## 6. Autosave

- `~/.local/state/kiwi/state.json`: parameter values that differ from `kiwi.patch`, Pianoteq
  parameters set from the UI, the current preset URI, favourites.
- Written 5 s after the last change (debounced), via a temporary file and atomic rename, at idle
  I/O priority. At most one write per 5 s, nothing while untouched.
- CC knob moves are saved too (the UI learns them through the MIDI monitor while a page is
  open). Knob moves made while no page is open are not saved; at boot the knobs therefore
  return to the last saved state until moved.
- "Reset" deletes the file and reloads the patch by restarting `kiwi-host` (a few seconds of
  silence; the page asks for confirmation).
- The page marks parameters that differ from `kiwi.patch`.

## 7. Phases

1. **Core**: service, page, live state of all non-Pianoteq parameters, MIDI monitor, CPU/temp,
   autosave + restore.
2. **Pianoteq parameters**: curated + advanced page.
3. **Presets**: first a spike: export, `bundle_add`, measure `preset_load` duration and xruns
   with `kiwi-stress` running. Ship prev/next only if switching is clean; otherwise report and
   decide with the user.

## 8. Serving and resources

- systemd unit `kiwi-web.service`: `User=patch`, `Nice=19`, `CPUSchedulingPolicy=idle`,
  `IOSchedulingClass=idle`, `MemoryMax=64M`, `TasksMax=16`,
  `AmbientCapabilities=CAP_NET_BIND_SERVICE`, `ProtectSystem=strict`,
  `ReadWritePaths=/home/patch/.local/state/kiwi`, `PrivateTmp=yes`, `Restart=on-failure`.
- Security: refuse requests from non-RFC1918/link-local addresses; on POST require `Origin`
  to match `Host` (stops other web pages from driving the instrument).
- Page: one `index.html` under ~40 KB, system fonts, no framework. Sliders send at most 20/s;
  the page closes its event stream while the tab is hidden.
- Budget: idle ~0 CPU, ~18 MB RAM; with viewers < 1 % of one core at SCHED_IDLE, ~2–25
  mod-host commands/s, 1–3 KB/s network.

### Theme

Kiwi fruit, dark stage mode: skin brown `#5B3A1E` / fuzz `#8A6A45` frame, flesh green
`#8DC63F`→`#C5E17A` fills, cream core `#F3F0D7` text, seed black `#1E1A14` slider thumbs
(seed-shaped). The carrier blend sits on a kiwi cross-section; the MIDI monitor is a kiwi slice
whose 12 seeds (one per pitch class) light with incoming notes. No blur or shadow filters.

```
┌──────────────────── kiwi ●  cpu 41%  48°C  midi ● ┐
│ PIANO  ◀  NY Steinway D Jazz  ▶   [family ▾] ★ 🔍 │
├───────────────────────────────────────────────────┤
│  piano      sampler     vocoder                    │
│   vol ▮      vol ▮       vol ▮     (CC20/22/24)    │
│   rev ▮      rev ▮       rev ▮     (CC21/23/25)    │
│ carrier  synth ───────●──── piano ───── sampler    │
├────────────── MIDI in ─────────────────────────────┤
│  (kiwi slice, 12 seeds lit by notes)  CC20 ▁▃▅ 98  │
│  last: ch1 note C4 vel 87 · ch1 cc 64 127          │
├────────── ▸ reverb ▸ limiter ▸ sampler ────────────┤
│ ▸ synth ▸ vocoder ▸ pianoteq ▸ pianoteq: all       │
├────────────────────────────────────────────────────┤
│ autosaved ✓   [reset to patch]   ● differs         │
└────────────────────────────────────────────────────┘
```

## 9. Testing

- **Unit tests** (Python `unittest`, run on the Mac and the Pi): the mod-host client against a
  fake lockstep server (one command per send, `resp` parsing, coalescing, never closing
  mid-reply, reconnect on EOF); `kiwi.patch` parsing (instances, `midi_map` lines); aseqdump
  line parsing; state store (debounce, atomic write, diff against the patch); request filters
  (private-address check, Origin check).
- **On device**: `kiwi-stress 600` with two pages open → 0 xruns; `kiwi-check` stays clean;
  CPU/RAM of `kiwi-web` idle and with viewers; reboot restores autosaved state; `kiwi-host`
  restart → page reconnects and restores; knob CC → page within 200 ms.

## 10. Risks

- `preset_load` is not real-time safe (§7 phase 3 gates it).
- mod-host's socket handling is fragile (single client, no pipelining, busy-loop on a dead
  peer); mitigated by one owner, lockstep, short-lived connection, never closing mid-reply.
- If `kiwi-web` held 5555 while `kiwi-load` runs, the boot load would hang; mitigated by
  connecting only when `kiwi-patch` is active and dropping on host restart (EOF).
- The source evidence is upstream HEAD; the device runs 1.13.0 (command set verified on the
  binary, behaviour to be confirmed in phase 1).

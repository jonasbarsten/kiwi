# Kiwi — headless three-mode instrument on Raspberry Pi + Pisound

Date: 2026-09-29
Status: Draft for review

## 1. Goal

One hardware box that takes MIDI and audio in and produces a stereo mix of three
continuously running "modes":

1. **Piano** — Pianoteq 8.
2. **Sampler** — a single audio file, repitched per MIDI key.
3. **Vocoder** — the Pisound audio input (mic) as modulator; the carrier is a
   blend of a synth voice, the piano and the sampler.

Each mode has its own **volume** and **reverb** knob. A seventh knob blends the
vocoder carrier. All modes run at the same time; knobs only set how much of each
is heard.

### Success criteria

- Power on → playable within ~30 s, with no manual steps (no MIDI patching, no
  UI, no module selection). Verified over 10 cold boots.
- 10 minutes of worst-case playing (all modes up, sustained chords, vocoder
  active) with zero xruns at the chosen buffer size.
- A USB MIDI controller plugged in at any time (before or after boot) works
  without configuration.
- RTP-MIDI from a Mac works, for testing before the USB controller exists.
- The existing Pianoteq activation survives; no SD card reformat.

### Why the previous attempt is being replaced

The previous attempt (Pure Data + `vstplugin~` hosting Pianoteq, MODEP hosting
the vocoder, desktop session autostart, `amidiminder` wildcard routing, plus a
background FluidSynth) ran three audio/MIDI systems side by side and
intermittently failed to boot into a working state (MIDI left unconnected, wrong
Patchbox module active). Reliability is the primary driver of this design.

## 2. Hardware and base system (as found on the device)

- Raspberry Pi 4 Model B Rev 1.5, 8 GB RAM, Pisound 2020 v1.1.
- Patchbox OS on Debian 12 (bookworm), kernel `6.6.51+rpt-rpi-v8` (PREEMPT,
  not PREEMPT_RT). CPU governor already `performance`. No throttling observed.
- Host: `patch@192.168.1.126`, SSH key auth.
- `jack.service` runs `jackd -R -P 95 -d alsa -d hw:pisound -r 48000 -p 128 -n 2
  -X seq`, as user `jack`, with `JACK_PROMISCUOUS_SERVER=jack`.
- Pianoteq 8.3.2 installed as LV2 at `~/.vst/Pianoteq 8.lv2`; activation and
  preferences in `~/.config/Modartt/` (user `patch`).
- MODEP plugins in `/var/modep/lv2`: `aether`, `amsynth`, `mod-mda-TalkBox`,
  `FluidDrums`, `FluidPianos`.

The existing OS install is kept. Nothing is reformatted.

## 3. Architecture

### 3.1 Host

A single headless **`mod-host`** process (already installed; the engine under
MODEP, without `mod-ui`) hosts every plugin. It is configured by a plain-text
command file (`kiwi.patch`) sent over its TCP socket at boot. Alternatives
considered and rejected: Carla headless (heavier, GUI-authored projects drift
between machines), a custom JACK/lilv host (weeks of plumbing that `mod-host`
already provides), Pure Data (fragile Pianoteq hosting, hand-built DSP).

### 3.2 Plugins

| Role | Plugin | Source |
|---|---|---|
| Piano | Pianoteq 8 LV2 | already installed |
| Sampler | sfizz LV2 | built from a pinned source release on the Pi (not in apt) |
| Carrier synth | amsynth LV2 | already installed (MODEP) |
| Vocoder | mda TalkBox LV2 | already installed (MODEP) |
| Reverb | Dragonfly Plate (Room overran at 128 frames on this Pi) | apt `dragonfly-reverb` |
| Master limiter | x42-dpl | apt `x42-plugins` |
| Carrier blend | `kiwi-carrier` | this repo |
| Mixer | `kiwi-mix` | this repo |

### 3.3 Signal flow

```
MIDI (Pisound DIN, USB, RTP) ──amidiminder rules──► Midi Through ──────► mod-host
                                                        │ (all plugins, one channel)
            ┌──────────────┬──────────────┬─────────────┘
            ▼              ▼              ▼
       Pianoteq         sfizz          amsynth (fixed patch)
        stereo          stereo           stereo
         │  │            │  │              │
         │  └─────────┐  │  └──────┐       │
         │            ▼  │         ▼       ▼
         │         kiwi-carrier (blend 0..2: synth ↔ piano ↔ sampler)
         │                           │ mono carrier
Pisound in (mic) ────────────────────┼──► mda TalkBox ──► vocoder stereo
         │               │                                   │
         ▼               ▼                                   ▼
       ┌──────────────── kiwi-mix ─────────────────────────────┐
       │ per mode: volume → dry bus; post-fader send → send bus│
       └────────┬──────────────────────────────┬───────────────┘
                │ dry L/R                      │ send L/R
                │                              ▼
                │                      Dragonfly reverb (100% wet)
                ▼                              │
              x42-dpl limiter (−1 dBFS) ◄──────┘  (JACK sums inputs)
                │
                ▼
          system:playback (Pisound out)
```

Design rules:

- **All knobs map to parameters of `kiwi-mix` and `kiwi-carrier` only.**
  Replacing the reverb, synth or sampler never breaks the control mapping.
- `kiwi-carrier` and `kiwi-mix` are separate plugins so the vocoder path does not
  form a cycle through one JACK client (which JACK resolves with an extra period
  of latency).
- Sends are **post-fader**: a mode at volume 0 also contributes no reverb.
- Every plugin processes all the time. CPU budget is the worst case with all
  modes sounding.
- The TalkBox takes carrier and modulator on its left/right inputs; its `carrier`
  parameter selects which side is the carrier. The patch sets it explicitly.

### 3.4 MIDI

- One stable MIDI entry point: the kernel `Midi Through` port (`snd-seq-dummy`,
  already present). JACK's `-X seq` bridge exposes it as a JACK MIDI port with
  the stable alias `Midi-Through:midi/playback_1`; the loader resolves the JACK
  port name from that alias at boot.
- `kiwi.patch` connects that port to every instrument plugin and to `mod-host`'s
  CC-mapping input.
- `amidiminder` runs with **explicit** rules (replacing the default
  `.hw <---> .app` wildcard): every hardware MIDI port (Pisound DIN, any USB
  device, including hot-plugged ones) and every `rtpmidid` session is routed into
  `Midi Through`. Nothing else is interconnected.
- `rtpmidid` 24.12.2 (arm64 `.deb` from its GitHub releases; not in apt; newer
  releases are built for Debian trixie only) advertises over avahi so macOS
  Audio MIDI Setup → Network can connect.
- All instruments receive every MIDI channel for now. A later channel or
  key-range split is configuration: insert an x42 MIDI filter plugin
  (`x42-plugins`) in front of an instrument in `kiwi.patch`, not code changes.

### 3.5 Controls (CC map)

Defaults, defined in `kiwi.patch`, on MIDI channel 1 (`mod-host`'s `midi_map`
binds one channel per mapping; the channel is a single value in `kiwi.patch`):

| CC | Parameter |
|---|---|
| 20 | Piano volume |
| 21 | Piano reverb send |
| 22 | Sampler volume |
| 23 | Sampler reverb send |
| 24 | Vocoder volume |
| 25 | Vocoder reverb send |
| 26 | Carrier blend |

## 4. Custom plugins (`plugins/kiwi`, installed as `kiwi.lv2`)

One LV2 bundle, two plugins. Each plugin is a plain-C DSP core with no LV2
dependency plus a thin LV2 wrapper, so the core is unit-testable on macOS and
Linux.

### 4.1 `kiwi-mix`

- Audio in: `piano_l/r`, `sampler_l/r`, `vocoder_l/r`.
- Audio out: `dry_l/r`, `send_l/r`.
- Control in (0.0–1.0): `piano_vol`, `piano_send`, `sampler_vol`,
  `sampler_send`, `vocoder_vol`, `vocoder_send`.
- Volume taper: audio taper mapping 0.0 → silence (exact 0), 1.0 → 0 dB.
  The same taper is applied to sends.
- `send = input × vol_gain × send_gain` (post-fader).
- Every control is smoothed with a ~20 ms one-pole filter to avoid zipper noise
  from 7-bit CCs. Denormals are flushed.

### 4.2 `kiwi-carrier`

- Audio in: `synth_l/r`, `piano_l/r`, `sampler_l/r` (each pair summed to mono,
  ×0.5).
- Audio out: `carrier` (mono).
- Control in: `blend` 0.0–2.0. In [0,1] it crossfades synth → piano; in [1,2]
  it crossfades piano → sampler, both equal-power (sin/cos). At exactly 0, 1 or
  2, only that source is heard.
- `blend` is smoothed like `kiwi-mix` controls.

## 5. Boot and services

### 5.1 Running

| Unit | Purpose |
|---|---|
| `jack.service` | Unchanged initially (48 kHz, 128 frames, `-X seq`). Buffer tuned last. |
| `kiwi-host.service` (new) | Runs `mod-host` as user `patch` (so Pianoteq finds `~/.config/Modartt`), `Restart=always`, `BindsTo=jack.service`, with `LV2_PATH` covering `/var/modep/lv2`, `/usr/lib/lv2`, the Pianoteq bundle location and this repo's bundle. |
| `kiwi-patch.service` (new, oneshot) | After `kiwi-host`, the loader sends `kiwi.patch` line by line, logs each response to the journal, and continues past failures (e.g. Pianoteq fails → sampler and vocoder still work). `PartOf=kiwi-host.service` so a host restart reloads the patch. |
| `amidiminder` | With the kiwi rules file. |
| `rtpmidid`, `avahi-daemon` | RTP-MIDI testing. |
| `ssh`, `NetworkManager` | Access. |
| `pisound-btn` | Kept; no function assigned yet. |

### 5.2 Disabled (not uninstalled; reversible)

- Desktop: default target → `multi-user.target` (removes lightdm, VNC and the
  `kiwi-pd` autostart). `wayvnc-control`, `patchbox-vnc.target`.
- MODEP: `modep-mod-host`, `modep-mod-ui`, `modep-touchosc2midi`,
  `touchosc2midi`, `modep-update.path`.
- The background FluidSynth (source to be identified during implementation).
- `cups`, `cups-browsed`, `bluetooth`, `hciuart`, `ModemManager`,
  `blokas-telemetry.target`, `wifi-hotspot`, `glamor-test`, `rp1-test`,
  `apt-daily.timer`, `apt-daily-upgrade.timer`.

### 5.3 Preserved

- `~/.config/Modartt/` and the Pianoteq bundle are copied into the repo's
  git-ignored `backup/` before any change.
- `~/Desktop/kiwi-pd` and the MODEP `kiwi` pedalboard are left on disk.

## 6. Pianoteq configuration

Target settings: polyphony capped at 32, reduced internal sample rate, Pianoteq
reverb off, one fixed preset at boot.

Findings on the device:

- Engine settings are global Pianoteq preferences in
  `~/.config/Modartt/Pianoteq83.prefs`, shared by the standalone and the LV2:
  `voices` (currently 64), `engine_rate` (already 24000), `multicore` (2).
  `install.sh` sets `voices` to 32; the rest is kept.
- The LV2 exposes its sound parameters as LV2 `patch` parameters (e.g.
  `Volume`, reverb switch) and saves its preset through the LV2 state
  interface. The first version runs the plugin's default preset with its
  reverb switched off via `patch_set`. Pinning a different preset later uses
  `mod-host`'s `state_save`/`state_load` for the instance state.

## 7. Sampler configuration

`sfizz` loads `sampler/kiwi.sfz`:

```
<region> sample=sample.wav lokey=0 hikey=127 pitch_keycenter=60
```

Root key, envelope and loop settings are changed in this file, not in code. The
sample file lives next to it in the repo; the first version uses the previous
attempt's `kiwi-pd/piano.wav`, copied to `sampler/sample.wav`.

## 8. Repository layout

```
kiwi/
  README.md                  setup, CC map, operations
  install.sh                 idempotent; run on the Pi to apply everything
  host/kiwi.patch            mod-host commands (plugins, connections, CC map)
  host/kiwi-load             loader script used by kiwi-patch.service
  host/kiwi-check            health check (services, plugins, connections, xruns, CPU)
  plugins/kiwi/              custom LV2 bundle source: src/, ttl/, Makefile, tests/
  sampler/kiwi.sfz, sample.wav
  system/                    systemd units, amidiminder rules
  docs/superpowers/specs/    this document
  legacy/                    previous attempt's own files (Pd patches, C, Swift)
  backup/                    git-ignored; Pianoteq prefs backup
```

Third-party clones in the working directory (`pure-data/`, `Cookbook/`,
`SwiftCrossCompilers/`, ~5.2 GB) are not committed; they are git-ignored.
Deployment is from this repo to the Pi over SSH (`rsync` then `install.sh`).

## 9. Testing

1. **Unit tests** for the DSP cores (C, run on macOS and on the Pi):
   blend at 0/1/2 yields only the matching source; equal-power sum constant
   across the crossfade; volume 0 yields exact silence; post-fader send is 0
   when volume is 0; smoothing converges to target; no NaN/denormals on silence.
2. **`kiwi-check`** on the Pi: expected services active, every plugin instance
   loaded, every expected JACK connection present, xrun count, JACK DSP load.
3. **Acceptance** on the device: the success criteria in section 1.

## 10. Later / optional (not in the first implementation)

- Read-only root filesystem (overlayfs) against SD corruption on power loss.
- Pisound button function (e.g. safe shutdown).
- Physical USB MIDI controller setup (map its CCs to section 3.5).
- Custom knob hardware (RP2040 USB-MIDI), as a separate project.

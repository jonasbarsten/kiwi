# Kiwi

A headless instrument on a Raspberry Pi 4 + Pisound. MIDI and audio in, stereo out.
Three modes run at the same time and are blended with knobs:

- **Piano** — Pianoteq 8.
- **Sampler** — one audio file (`sampler/sample.wav`), repitched per MIDI key by sfizz.
- **Vocoder** — the Pisound input (a mic) talks through a carrier that blends a synth
  voice (amsynth), the piano and the sampler.

Each mode has a volume knob and a reverb knob (a send into one shared reverb). A
seventh knob blends the vocoder carrier. Everything is hosted by a single headless
`mod-host` that systemd starts at boot.

Design: [`docs/superpowers/specs/2026-09-29-kiwi-design.md`](docs/superpowers/specs/2026-09-29-kiwi-design.md).
Implementation plan: [`docs/superpowers/plans/2026-09-29-kiwi.md`](docs/superpowers/plans/2026-09-29-kiwi.md).

## Hardware

- Raspberry Pi 4 Model B (8 GB), Pisound 2020 v1.1, Patchbox OS (Debian 12 bookworm).
- Host name `kiwi` (`kiwi.local` over mDNS); SSH `patch@192.168.1.126` on the wired LAN.
- MIDI from the Pisound DIN input, any USB MIDI device, or RTP-MIDI over the network.
  All of them are merged automatically; nothing needs patching by hand.
- A mic for the vocoder needs a preamp in front of the Pisound input (line/instrument level).

## Controls

Knobs are MIDI CCs on MIDI channel 1. They are defined in `host/kiwi.patch`.

| CC | Control |
|---|---|
| 20 | Piano volume |
| 21 | Piano reverb |
| 22 | Sampler volume |
| 23 | Sampler reverb |
| 24 | Vocoder volume |
| 25 | Vocoder reverb |
| 26 | Vocoder carrier blend (synth → piano → sampler) |

## Deploy

From this repo on your machine:

```bash
rsync -a --delete --exclude backup --exclude legacy --exclude pure-data --exclude Cookbook \
  --exclude SwiftCrossCompilers --exclude .git --exclude .superpowers --exclude plugins/kiwi/build \
  --exclude web/params.json --exclude __pycache__ \
  ./ patch@192.168.1.126:kiwi/
ssh patch@192.168.1.126 'cd ~/kiwi && ./install.sh'
```

`install.sh` is safe to re-run. It builds on Patchbox OS: `mod-host`, amsynth, mda TalkBox
(from the MODEP module) and `jack.service` come from Patchbox, not from `install.sh`, and
Pianoteq 8 must already be installed and activated in `~/.vst/Pianoteq 8.lv2`.

## How the patch works

`host/kiwi.patch` is the single place that defines the instrument: which plugins load,
their settings, every audio/MIDI connection, and the knob (CC) mapping. It is plain
[mod-host](https://github.com/moddevices/mod-host) commands, sent line by line by
`host/kiwi-load` when `kiwi-patch.service` starts. `@MIDI_IN@` stands for the JACK port
of the kernel `Midi Through` device, where all MIDI sources are merged.

| Instance | Plugin | Role |
|---|---|---|
| 0 | Pianoteq 8 | piano |
| 1 | sfizz (`sampler/kiwi.sfz`) | sampler |
| 2 | amsynth | synth voice for the vocoder carrier |
| 3 | kiwi carrier | blends synth / piano / sampler into the carrier |
| 4 | mda TalkBox | vocoder: mic on Pisound input 1 (left), carrier on right |
| 5 | kiwi mix | per-mode volume and post-fader reverb send |
| 6 | Zita Rev1 (default; switchable, see Web UI) | shared reverb, fully wet |
| 7 | x42 dpl | limiter at −1 dBFS on the dry bus (the reverb return bypasses it, see below) |

To change the MIDI channel of the knobs, edit the third number of the `midi_map` lines
(0 = channel 1). To change the sampler's root key or the envelope ranges, edit
`sampler/kiwi.sfz` (the envelope itself is set from the web UI).

Pianoteq engine settings are its global preferences (`~/.config/Modartt/Pianoteq83.prefs`):
`install.sh` caps polyphony at 24 voices and uses two engine threads (`multicore=2`). The
internal engine rate (24 kHz on this Pi) is left as Pianoteq has it; `install.sh` only
prints it. The plugin runs its default preset.

## Latency and performance

JACK runs at 48 kHz, 128 frames, 2 periods (about 5.3 ms output latency); `install.sh`
sets this through `patchbox jack config`. Findings from tuning on this Pi:

- **Dragonfly Room is not usable at 128 frames here**: whenever signal reaches it, it
  overruns its deadline every 32768 frames (a steady glitch at ~88 bpm). Measured under
  `kiwi-stress` (45 s, average JACK DSP load; the instrument without a reverb sits at ~50 %):
  MDA Ambience 50.5 % and Guitarix Reverb 52.1 % and Calf Reverb 52.5 % and Zita Rev1 53.7 %
  and Dragonfly Plate 56.6 % all with 0 xruns; Dragonfly Hall 60 % with a few; Aether 66 %
  and ZamVerb 60 % with many. The first five are the choices offered in the web UI;
  Zita Rev1 is the default in `kiwi.patch`.
- `mod-host`'s `bypass` does not stop every plugin's processing, so bypassing is not a
  reliable way to find which plugin is expensive; disconnecting its input is.
- Pianoteq with `multicore=2` absorbs dense chords with the sustain pedal held;
  with `multicore=1` the same chords overran.
- `host/kiwi-stress [seconds]` plays a worst case (sustain held, alternating 8-note
  chords every 0.4 s) through a temporary virtual MIDI port and counts JACK errors.
  At 128 frames: 32 voices → about 6 short overruns in 5 minutes; 24 voices → 1–2 per
  2 minutes; 24 voices with the reverb running in parallel to the limiter → none.
- Every plugin in `mod-host` is its own JACK client, so each step in a serial chain
  costs time. The reverb return goes straight to the outputs instead of through the
  limiter, which keeps the chain one client shorter.

## Web UI

Open **http://kiwi.local/** (or `http://192.168.1.126/` on the wired LAN, or `http://10.42.0.1/`
on the kiwi hotspot) on a phone or computer. It is mobile first; on a phone, "Add to Home Screen" makes it
a full-screen app.

- **Mix**: volume and reverb per mode, and the vocoder carrier blend, with their CC numbers.
  Moving a physical knob moves the slider.
- **MIDI in**: a kiwi slice whose seeds light up per pitch class, the last event, and bars for
  CC 20–26. Header: audio host status, JACK DSP load, CPU temperature, MIDI activity.
- **Reverb**: a picker swaps the shared reverb live (Zita Rev1, Calf Reverb, MDA Ambience,
  Guitarix Reverb, Dragonfly Plate: `web/kiwi_web/reverbs.py`). The tail cuts for a moment;
  the dry sound is untouched. Each reverb keeps its own settings; the choice is part of the
  preset. Reset returns to the `kiwi.patch` reverb.
- **Sampler envelope**: attack, decay, sustain and release live in `sampler/kiwi.sfz` as
  CC-driven opcodes (CC 102–105). The page sends those CCs through the kernel's virtual
  MIDI device (`snd-virmidi`, routed into Midi Through), so they reach sfizz like any
  knob; the values are part of the preset.
- **Sections**: limiter, vocoder, sampler, synth (all amsynth controls), Pianoteq
  (curated) and Pianoteq (all parameters). Routing-critical settings are not offered.
  Pianoteq values show "–" until set from the page (mod-host cannot read them back).
  Sliders apply while you drag: Pianoteq and reverb changes at most every 150 ms (they
  recompute inside the audio thread), everything else every 50 ms.
- **Presets**: see the next section. A dot marks values that differ from `host/kiwi.patch`;
  **Reset to patch** puts them back live (RAM only; the saved preset is untouched).
- Only private-network addresses are served, and changes are only accepted from the page
  itself (or from the Pi itself, for the button). No login.

It is built not to disturb audio: `kiwi-web.service` runs at idle CPU and I/O priority with a
64 MB memory cap, connects to mod-host only while a page is open or a change is pending
(and only after `kiwi-patch` and `kiwi-restore` finished), and disconnects 10 s after the
last need. A hidden browser tab disconnects by itself. Its MIDI monitor (`aseqdump`, idle
priority) runs permanently so the morph CC and the button work with no page open.
The parameter list (`web/params.json`) is generated by `install.sh` from the installed plugins.

## Presets

Everything you change lives in RAM, in one of **eight preset slots**. Nothing is written to
the SD card while you play, so the plug can be pulled at any moment. Verified: 90 s of playing,
knob turning and morphing under `kiwi-stress` wrote no file.

| Action | Pisound button | Page |
|---|---|---|
| Next preset | single click | ▶ |
| Previous preset | double click | ◀ |
| Save RAM into the current slot | hold 3 s (saves the moment 3 s pass; no need to release) | **Save** (orange when there are unsaved changes) |
| Pick any slot, rename | — | ☰ sheet; the name field (saved with the slot) |

The LED stays dark while holding, flashes the slot number on a selection and gives a rapid
burst of 8 flashes when a save happens. (`pisound-btn` only reports holds on release, so the
hold-to-save timer lives in the `DOWN`/`UP` scripts, `host/kiwi-btn`.) The box boots into the
**last selected** slot: selecting writes the one-line `~/.local/state/kiwi/current`, the only
write outside saving; slots are `~/.local/state/kiwi/presets/<n>.json`. Selecting a slot
discards unsaved RAM edits and resets anything the previous slot had changed, so sounds do not
bleed between slots. A `kiwi-host` restart or reboot reloads the slot (`kiwi-restore`).

**Morph**: CC 27 (or the page's morph slider) crossfades between the current slot (0) and the
next one (127) for continuous parameters: volumes, sends, blend, limiter, vocoder quality,
sampler ports and envelope, the synth's float parameters, and the current reverb's settings
when both slots use that reverb. Toggles, enums, integers, the reverb choice and Pianoteq are
not morphed. Morphing only changes RAM; save to keep it. Selecting a slot re-anchors the
morph, and CC knobs are absolute, so the next move jumps to the knob's position.

The pre-preset autosave file (`state.json`) is migrated into slot 1 once.

Web UI tests (macOS or the Pi): `python3 -m unittest discover -s web/tests -t web`.

## Operations

```bash
systemctl status kiwi-host kiwi-patch     # is the instrument running?
journalctl -u kiwi-patch -b               # what the patch loader did this boot
journalctl -u kiwi-restore -b             # which preset slot was applied at boot
journalctl -t kiwi-btn -b                 # button presses that failed to reach kiwi-web
~/kiwi/host/kiwi-check                    # full health check
```

The system journal lives in RAM (`system/journald-kiwi.conf`: 32 MB cap, gone at reboot); the
persistent journal under `/var/log/journal` was removed by `install.sh`. To keep a copy of a
session's logs, run `~/kiwi/host/kiwi-log` **after** playing: it writes this boot's audio,
web and button logs to `~/kiwi-logs/<date>.log` (the one deliberate SD write) and prints an
xrun summary; `kiwi-log xruns` prints only the summary, `kiwi-log follow` watches live.
`kiwi-stress` now also lists when its xruns happened and which plugins were late.

## What `install.sh` turns off

The Pi boots to a console (`multi-user.target`), not a desktop. Nothing is uninstalled:

- The Patchbox MODEP module is deactivated (`patchbox module deactivate`); otherwise
  `patchbox-init` re-enables MODEP's services on every boot.
- Disabled: `lightdm`, `wayvnc-control`, `patchbox-vnc.target`, MODEP services,
  `touchosc2midi`, `cups`, `cups-browsed`, `bluetooth`, `hciuart`, `ModemManager`,
  `blokas-telemetry.target`, `wifi-hotspot`, `glamor-test`, `rp1-test`, `pisound-ctl`
  (Bluetooth link to the Pisound phone app), `triggerhappy`, and the `apt-daily`,
  `man-db`, `dpkg-db-backup`, `e2scrub_all` and `fstrim` timers (so no maintenance job
  fires mid-performance; run `sudo fstrim -av` by hand now and then), and `cron` (hourly
  `fake-hwclock` and daily apt/dpkg/logrotate/man-db jobs). `systemd-timesyncd` no longer
  saves its clock file every minute (`system/timesyncd-kiwi.conf`). Together with the
  journal in RAM, nothing writes to the card while playing.
- The Pisound button runs `system/pisound.conf` (presets, see above) instead of Patchbox's
  defaults (which toggled the hotspot on a 3 s hold and shut down on 5 s).
- Masked: `packagekit`, `rtkit-daemon` (D-Bus activated), and for all users FluidSynth,
  PipeWire, PipeWire-Pulse, WirePlumber and PulseAudio.
- rtpmidid runs with `system/rtpmidid.ini` (via a systemd drop-in): it receives network
  MIDI but does not export the Pi's own MIDI ports back to the network.

To undo: `sudo systemctl set-default graphical.target`, `sudo patchbox module activate modep`,
`sudo systemctl enable <unit>` for anything wanted back, and
`sudo systemctl --global unmask fluidsynth.service pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket wireplumber.service`.

## Network

- **Wired LAN**: `192.168.1.126` (SSH, web UI, RTP-MIDI). It never takes the default route
  (`ipv4.never-default` on "Wired connection 1"); that LAN has no internet.
- **Wi-Fi hotspot (default)**: SSID **kiwi**, WPA2. Phones join it directly and open
  `http://kiwi.local/` or `http://10.42.0.1/`. NetworkManager connection `kiwi-hotspot`
  (`ipv4.method shared`: the Pi hands out `10.42.0.x` addresses). In this mode the Pi has no
  internet.
- **Wi-Fi client (for development)**: joins an existing network ("internett") for internet,
  e.g. to install packages. The radio does one or the other.

Switch with `host/kiwi-wifi`:

```bash
~/kiwi/host/kiwi-wifi client      # join "internett" (internet for installs); persists across reboots
~/kiwi/host/kiwi-wifi hotspot     # back to the kiwi hotspot (the default)
~/kiwi/host/kiwi-wifi status
```

Do this over the wired LAN: switching drops whatever is connected over Wi-Fi.
`install.sh` only needs internet when a package or the sfizz build is missing.

These connections were created by hand (the passwords are not in the repo), e.g.:

```bash
sudo nmcli connection add type wifi ifname wlan0 con-name kiwi-hotspot autoconnect no ssid kiwi \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  802-11-wireless.powersave 2 ipv4.method shared ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.psk '<password>'
```

The Patchbox hotspot (`pb-hotspot`) is left in place with autoconnect off.

## Development

The custom plugins' DSP core is plain C with unit tests that run on macOS and on the Pi:

```bash
make -C plugins/kiwi test
```

## Repository layout

```
install.sh              idempotent installer, run on the Pi
host/                   mod-host patch file, loader, health check
plugins/kiwi/           custom LV2 plugins (kiwi mix, kiwi carrier) with tests
sampler/                SFZ file and the sample
web/                    web UI: server (kiwi_web/), page (static/), metadata generator, tests
system/                 systemd units, MIDI routing rules, sfizz build script
docs/superpowers/       design spec and implementation plan
legacy/                 the previous Pure Data / MODEP attempt, kept for reference
backup/                 local only (git-ignored): Pianoteq preferences and bundle
```

## Legacy

The previous attempt (Pure Data patch hosting Pianoteq, MODEP for the vocoder) lives in
`legacy/`; its notes are in `legacy/README-old.md`.

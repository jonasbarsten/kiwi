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
| 6 | Dragonfly Plate | shared reverb, fully wet |
| 7 | x42 dpl | limiter at −1 dBFS on the dry bus (the reverb return bypasses it, see below) |

To change the MIDI channel of the knobs, edit the third number of the `midi_map` lines
(0 = channel 1). To change the sampler's root key or envelope, edit `sampler/kiwi.sfz`.

Pianoteq engine settings are its global preferences (`~/.config/Modartt/Pianoteq83.prefs`):
`install.sh` caps polyphony at 24 voices and uses two engine threads (`multicore=2`). The
internal engine rate (24 kHz on this Pi) is left as Pianoteq has it; `install.sh` only
prints it. The plugin runs its default preset.

## Latency and performance

JACK runs at 48 kHz, 128 frames, 2 periods (about 5.3 ms output latency); `install.sh`
sets this through `patchbox jack config`. Findings from tuning on this Pi:

- **Dragonfly Room is not usable at 128 frames here**: whenever signal reaches it, it
  overruns its deadline every 32768 frames (a steady glitch at ~88 bpm). Dragonfly Plate
  does not, so the shared reverb is a Plate.
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
- **Sections**: reverb, limiter, vocoder, sampler, synth (all amsynth controls), Pianoteq
  (curated) and Pianoteq (all parameters). Routing-critical settings are not offered.
  Pianoteq values show "–" until set from the page (mod-host cannot read them back).
  Sliders apply while you drag: Pianoteq and reverb changes at most every 150 ms (they
  recompute inside the audio thread), everything else every 50 ms.
- **Autosave**: changes (including knob moves seen while a page is open) are saved 5 s after
  the last change to `~/.local/state/kiwi/state.json` and restored by `kiwi-restore.service`
  after every patch load. A dot marks values that differ from `host/kiwi.patch`;
  **Reset to patch** puts them back live.
- Only private-network addresses are served, and changes are only accepted from the page
  itself. No login.

It is built not to disturb audio: `kiwi-web.service` runs at idle CPU and I/O priority with a
64 MB memory cap, connects to mod-host only while a page is open (and only after
`kiwi-patch` and `kiwi-restore` finished), and stops its MIDI monitor and disconnects
10 s after the last page closes. A hidden browser tab disconnects by itself.
The parameter list (`web/params.json`) is generated by `install.sh` from the installed plugins.

Web UI tests (macOS or the Pi): `python3 -m unittest discover -s web/tests -t web`.

## Operations

```bash
systemctl status kiwi-host kiwi-patch     # is the instrument running?
journalctl -u kiwi-patch -b               # what the patch loader did this boot
journalctl -u kiwi-restore -b             # what autosaved state was restored
~/kiwi/host/kiwi-check                    # full health check
```

## What `install.sh` turns off

The Pi boots to a console (`multi-user.target`), not a desktop. Nothing is uninstalled:

- The Patchbox MODEP module is deactivated (`patchbox module deactivate`); otherwise
  `patchbox-init` re-enables MODEP's services on every boot.
- Disabled: `lightdm`, `wayvnc-control`, `patchbox-vnc.target`, MODEP services,
  `touchosc2midi`, `cups`, `cups-browsed`, `bluetooth`, `hciuart`, `ModemManager`,
  `blokas-telemetry.target`, `wifi-hotspot`, `glamor-test`, `rp1-test`, and the
  `apt-daily` timers (so it never upgrades itself mid-gig).
- Masked for all users: FluidSynth, PipeWire, PipeWire-Pulse and WirePlumber.
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

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
- Host `patch@192.168.1.126`.
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
  ./ patch@192.168.1.126:kiwi/
ssh patch@192.168.1.126 'cd ~/kiwi && ./install.sh'
```

`install.sh` is safe to re-run.

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
| 6 | Dragonfly Room | shared reverb, fully wet |
| 7 | x42 dpl | limiter at −1 dBFS before the Pisound output |

To change the MIDI channel of the knobs, edit the third number of the `midi_map` lines
(0 = channel 1). To change the sampler's root key or envelope, edit `sampler/kiwi.sfz`.

Pianoteq engine settings are its global preferences (`~/.config/Modartt/Pianoteq83.prefs`):
`install.sh` caps polyphony at 32 voices; the internal engine rate is 24 kHz. The plugin
runs its default preset with its own reverb switched off.

## Operations

```bash
systemctl status kiwi-host kiwi-patch     # is the instrument running?
journalctl -u kiwi-patch -b               # what the patch loader did this boot
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

Network: the Pi reaches the internet over Wi-Fi; the wired LAN (`192.168.1.126`) is kept
for SSH only (`ipv4.never-default` on the wired connection). The Patchbox hotspot
(`pb-hotspot`) has autoconnect off. These were set by hand, not by `install.sh`.

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
system/                 systemd units, MIDI routing rules, sfizz build script
docs/superpowers/       design spec and implementation plan
legacy/                 the previous Pure Data / MODEP attempt, kept for reference
backup/                 local only (git-ignored): Pianoteq preferences and bundle
```

## Legacy

The previous attempt (Pure Data patch hosting Pianoteq, MODEP for the vocoder) lives in
`legacy/`; its notes are in `legacy/README-old.md`.

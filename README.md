# kiwi 🥝

**A headless, gig-proof instrument on a Raspberry Pi 4 + Pisound: Pianoteq piano, a
one-sample sampler and a vocoder, all playing at once, mixed with seven knobs, controlled
from your phone, and safe to unplug mid-chord.**

Plug in power and a MIDI keyboard. Thirty seconds later it is a piano. Twist a knob and a
sample you chose plays under it, repitched across the keyboard. Sing into the mic and the
vocoder talks through the piano, the sample, or a synth voice. Everything lives in eight
preset slots you step through with the Pisound button, morph between with a CC, and edit
from a kiwi-green page on your phone. Nothing is ever written to the SD card while you play.

```
                 ┌──────────────┐
 MIDI (DIN /     │  Pianoteq 8  │──────────────┐
 USB / RTP) ───┬▶│   (piano)    │              │
               │ └──────────────┘   ┌──────────▼──────────┐    ┌──────────┐
               │ ┌──────────────┐   │      kiwi mix       │───▶│ limiter  │─┐
               ├▶│    sfizz     │──▶│  volume + reverb    │    └──────────┘ │
               │ │  (sampler)   │   │   send per mode     │    ┌──────────┐ ├──▶ out L/R
               │ └──────────────┘   │                     │───▶│  reverb  │─┘
               │ ┌──────────────┐   └──────────▲──────────┘    └──────────┘
               └▶│   amsynth    │──┐           │
                 │ (synth voice)│  │  ┌────────┴────────┐
                 └──────────────┘  └─▶│  kiwi carrier   │   ┌─────────────┐
 mic ─────────────────────────────────│ synth/piano/    │──▶│ mda TalkBox │
                                      │ sampler blend   │   │  (vocoder)  │──▶ mix
                                      └─────────────────┘   └─────────────┘
```

One `mod-host` process hosts the whole chain; everything else is systemd, a 1,000-line
Python server with no dependencies, and one HTML file.

---

## Contents

- [What it does](#what-it-does)
- [Hardware](#hardware)
- [Build one](#build-one)
- [Playing it](#playing-it)
  - [Knobs](#knobs)
  - [Presets, the button and the LED](#presets-the-button-and-the-led)
  - [Morph](#morph)
  - [The web UI](#the-web-ui)
  - [CC mapping](#cc-mapping)
  - [Network](#network)
- [How it is built](#how-it-is-built)
  - [The patch](#the-patch)
  - [Latency and headroom](#latency-and-headroom)
  - [Pianoteq presets: the key nobody reads](#pianoteq-presets-the-key-nobody-reads)
  - [Nothing touches the SD card](#nothing-touches-the-sd-card)
  - [The web service](#the-web-service)
- [Operations](#operations)
- [Development](#development)
- [Caveats and honest limits](#caveats-and-honest-limits)
- [Credits and license](#credits-and-license)

---

## What it does

- **Three modes at once.** Piano (Pianoteq 8), sampler (one WAV, repitched per key by
  sfizz), vocoder (the Pisound mic input modulating a carrier blended from a synth voice,
  the piano and the sampler). Each has a volume and a reverb send into one shared reverb.
- **Seven knobs, any controller.** Mix, sends and carrier blend on CC 20–26 out of the
  box; map anything (any Pianoteq parameter, the sampler envelope, panic, next/previous
  preset, morph) to any CC from the phone in a few taps.
- **Eight preset slots** in RAM. Button click = next, double click = previous, hold 3 s =
  save. The box boots into the last slot you chose. A CC morphs between the current slot
  and the next one, live.
- **A phone page** at `http://kiwi.local/` over the Pi's own Wi‑Fi hotspot: every
  parameter, the Pianoteq preset browser with favourites, five switchable reverbs, a MIDI
  monitor, panic, mapping. Mobile first, zero dependencies, and built so it can never
  disturb the audio.
- **Gig-proof.** Boots to the instrument in ~30 s with no desktop, no services you don't
  need, logs in RAM, and no disk writes while playing — pull the plug whenever you like.
- **Low latency.** 48 kHz, 128 frames, two periods: about 5 ms out, zero xruns under a
  worst-case stress test for ten minutes.

## Hardware

- Raspberry Pi 4 Model B (8 GB used here; 4 GB should do) with [Pisound](https://blokas.io/pisound/).
- [Patchbox OS](https://blokas.io/patchbox-os/) (Debian 12 bookworm) with the MODEP module.
- A licensed **Pianoteq 8** for Linux ARM64 (any edition; instruments you don't own play in
  Pianoteq's demo mode).
- MIDI from the Pisound DIN input, any USB MIDI device, or RTP‑MIDI over the network — all
  merged automatically.
- A mic with a preamp (line/instrument level) into Pisound input 1 for the vocoder.
- A phone for the page. Optional: a MIDI controller with knobs.

## Build one

Everything after the four prerequisites is one script, safe to re-run.

1. **Flash Patchbox OS**, run its setup, and install the MODEP module once
   (`sudo patchbox module activate modep`). That brings `mod-host`, amsynth and mda TalkBox;
   `install.sh` will later deactivate MODEP's own services.
2. **Install Pianoteq 8** so that `~/.vst/Pianoteq 8` (the standalone) and
   `~/.vst/Pianoteq 8.lv2` exist, activate it (`"~/.vst/Pianoteq 8" --activate SERIAL` or
   in its window) and run it once so it writes its preferences.
3. **Clone this repo on your computer** and create `kiwi.env` from
   [`kiwi.env.example`](kiwi.env.example): the Pi's address, the hotspot password, and
   optionally a Wi‑Fi network with internet for development. It is git-ignored.
4. **Give the Pi internet for the first install** (Ethernet is easiest: the hotspot, once
   up, has none).

Then:

```bash
./deploy.sh
```

which `rsync`s the repo to `patch@<KIWI_HOST>:kiwi/` and runs `install.sh` there. The
installer first checks what it cannot provide (Patchbox, MODEP, Pianoteq, activation,
version) and says exactly what is missing; then it installs the plugins, builds sfizz and
the kiwi LV2 plugins, sets up MIDI routing, JACK, the hotspot, all services, exports and
converts the Pianoteq presets and generates the page's parameter list. Re-run it after any
change; it only touches what differs.

Reboot. The LED blinks the slot number, the page is at `http://kiwi.local/` on the `kiwi`
Wi‑Fi, and you have a piano.

## Playing it

### Knobs

| CC | Control |
|---|---|
| 20 | Piano volume |
| 21 | Piano reverb |
| 22 | Sampler volume |
| 23 | Sampler reverb |
| 24 | Vocoder volume |
| 25 | Vocoder reverb |
| 26 | Vocoder carrier blend (synth → piano → sampler) |
| 27 | Morph to the next preset |

These are only defaults: see [CC mapping](#cc-mapping). Pianoteq, the sampler and the synth
listen on all channels; the sustain pedal and pitch bend go where you expect.

### Presets, the button and the LED

Everything you change lives in RAM, in one of **eight slots**.

| Action | Pisound button | Page |
|---|---|---|
| Next preset | click | ▶ |
| Previous preset | double click | ◀ |
| Save RAM into the current slot | hold 3 s (saves the moment 3 s pass) | **Save** (amber when there is something to save) |
| Pick any slot, rename it | — | ☰ sheet; the name field |

The LED stays dark while you hold, flashes the slot number (0.3 s apart) on a selection, and
gives a rapid burst of eight flashes when a save lands. Selecting a slot discards unsaved
edits and resets whatever the previous slot had changed, so sounds never bleed between
slots. The box boots into the **last selected** slot; a `kiwi-host` restart reloads it.

Each slot holds: the mix, every plugin parameter you touched, the reverb choice and its
settings, the sampler envelope, and the Pianoteq preset (with your parameter tweaks on top).

### Morph

CC 27 (or the page's morph slider) crossfades between the current slot (0) and the next one
(127) for every continuous parameter: volumes, sends, blend, limiter, vocoder quality,
sampler ports and envelope, the synth's float parameters, and the reverb's settings when
both slots use the same reverb. Toggles, enums, the reverb choice and the Pianoteq preset
are not morphed. Morphing is RAM only; save to keep where you landed.

### The web UI

Open **http://kiwi.local/** on the `kiwi` Wi‑Fi (or the Pi's wired address). On a phone,
"Add to Home Screen" makes it a full-screen app.

- **Mix** — volume and reverb per mode, the carrier blend, the current Pianoteq preset with
  ◀ ▶. A control that differs from the patch shows ↺; tap it to go back. Moving a knob
  moves the slider.
- **Header** — host state, JACK DSP load, CPU temperature, MIDI activity, **Map**, **Panic**.
  A banner appears when the audio host is starting, down, or unreachable.
- **Panic** — All Sound Off + All Notes Off on all 16 channels, into every instrument.
- **MIDI in** — a kiwi slice whose seeds light per pitch class, the last event, and a level
  bar per mapped CC named after what it drives.
- **Reverb** — swap the shared reverb live between Zita Rev1 (default), Calf Reverb, MDA
  Ambience, Guitarix Reverb and Dragonfly Plate; each keeps its own settings.
- **Sampler** — attack, decay, sustain and release (CC-driven opcodes in `sampler/kiwi.sfz`,
  sent through a virtual MIDI device so sfizz sees them like any knob).
- **Pianoteq** — the current preset, ◀ ▶ through your ★ favourites (or the family), a
  browser with family filter and search, a curated parameter set and a second card with all
  of them. Presets are part of the slot.
- **Sections** carry their status on the summary line when closed; on a wide screen the
  cards in a row open together; the browser remembers what you had open.
- Sliders apply while you drag (every 50 ms; 150 ms for Pianoteq and the reverb, which
  recompute in the audio thread). One request in flight at a time, so values never arrive
  out of order.
- Only private-network addresses are served, and changes are only accepted from the page
  itself. No login: whoever is on your hotspot is in the band.

### CC mapping

Tap **Map**. The page turns amber, controls freeze and become targets. Tap a control (or
◀ ▶, Save, the morph slider, Panic), then move a knob or press a button on your
controller: the next control change, on any channel, binds to it. Tap empty space or Map
again to finish. Mapped controls show `CC n`; in map mode a ✕ removes a mapping.

- One control has one CC; one CC may drive **any number** of controls.
- Mix, carrier, synth, limiter and reverb controls are bound *inside mod-host*
  (`midi_map`): audio-thread, no added latency. Pianoteq parameters and the sampler
  envelope have no mod-host ports, so the service forwards those CCs itself (the same path
  the sliders use, a few tens of milliseconds).
- The map is global, not part of a slot: `~/.local/state/kiwi/ccmap.json`, written only when
  a mapping changes, applied at boot. To get the defaults back, delete the file and restart
  `kiwi-host` (`sudo systemctl restart kiwi-host`; the page and mod-host both reload it).
- The sampler envelope CCs (102–105) and the panic CCs are what the service itself sends
  on channel 1, so it never learns from or forwards those; a controller on channel 1 drives
  the envelope directly, which is what you want anyway.
- Actions (panic, next, previous, save) fire when a CC crosses 64 upwards: a button, or a
  knob turned past the middle, not every tick above it. A learn that nobody completes
  expires after 30 s, and ends when the last page closes.

### Network

- **Wi‑Fi hotspot (default)** — SSID from `kiwi.env` (`kiwi`), WPA2. Phones join it and open
  `http://kiwi.local/` or `http://10.42.0.1/`. No internet in this mode.
- **Wired LAN** — SSH, the page and RTP‑MIDI; never the default route.
- **Wi‑Fi client (development)** — `host/kiwi-wifi client` joins the network named in
  `kiwi.env` for package installs; `host/kiwi-wifi hotspot` goes back. Do this over the
  wire: the radio does one or the other.

RTP‑MIDI: the Pi advertises itself as `kiwi`; it receives sessions but does not export its
own ports back, so you never get MIDI loops.

## How it is built

### The patch

[`host/kiwi.patch`](host/kiwi.patch) is the whole instrument in one readable file: plain
[mod-host](https://github.com/moddevices/mod-host) commands, sent line by line at boot by
`host/kiwi-load`.

| Instance | Plugin | Role |
|---|---|---|
| 0 | Pianoteq 8 | piano; loads *Ant. Petrof Warm* as the baseline preset |
| 1 | sfizz (`sampler/kiwi.sfz`) | sampler, one region spanning the keyboard |
| 2 | amsynth | synth voice for the vocoder carrier |
| 3 | **kiwi carrier** | blends synth / piano / sampler into the carrier (custom LV2) |
| 4 | mda TalkBox | vocoder: mic on input 1, carrier on the right input |
| 5 | **kiwi mix** | per-mode volume and post-fader reverb send (custom LV2) |
| 6 | Zita Rev1 (switchable) | shared reverb, fully wet, returned straight to the outputs |
| 7 | x42 dpl | limiter at −1 dBFS on the dry bus |

All MIDI sources are merged in the kernel's `Midi Through` port by amidiminder rules, so a
USB controller plugged in on stage just works. The two custom plugins are ~200 lines of C
each with unit tests that run on macOS and the Pi.

### Latency and headroom

JACK runs at 48 kHz, 128 frames, 2 periods (≈5.3 ms output latency). What it took to get
there with zero xruns, measured with `host/kiwi-stress` (sustain held, alternating 8-note
chords every 0.4 s, all modes up):

- Pianoteq: 24 voices, two engine threads (`multicore=2`); with one thread the same chords
  overran.
- The reverb return bypasses the limiter: every plugin in mod-host is its own JACK client,
  and one client fewer in the longest chain was the difference between occasional and zero
  overruns.
- Not every reverb survives 128 frames on a Pi 4. Dragonfly Room overran every 32768 frames
  (a metronomic glitch at ~88 bpm); Aether and ZamVerb were simply too heavy. The five on
  offer all run at 50–57 % DSP load with none.
- `mod-host`'s bypass does not stop a plugin's processing; disconnect its input to measure.
- Switching presets and morphing under load cost nothing measurable; the *preset* can.
  Plain pianos (NY Steinway D Classical, Ant. Petrof Warm) run the full stress test with
  zero xruns; "effects" presets such as NY Steinway D Bowed (mallet bounce, note effects)
  overrun steadily at 24 voices and 128 frames. Try a preset under `kiwi-stress` before
  trusting it on stage.

### Pianoteq presets: the key nobody reads

Pianoteq can export its presets as LV2 preset bundles (`--export-lv2-presets`), and every
LV2 host loads them without complaint — and nothing changes. The export stores the state
under `urn:juce:stateBinary`; the 8.3.2 plugin only ever reads
`https://www.modartt.com/lv2/Pianoteq8:StateString`, a JUCE base64 string of the same
blob behind a 28-byte header. `install.sh` therefore rewrites every exported preset into
that form ([`web/kiwi_web/pianoteq_state.py`](web/kiwi_web/pianoteq_state.py)); the two
constant words in the header were learned from a state the plugin saved itself, verified
with 8.3.2, and the installer warns on any other version. The rewritten bundles also get
ASCII, space-free paths, which is what mod-host's `bundle_add` needs anyway. 654 presets,
all loading.

### Nothing touches the SD card

While playing, nothing writes to the card. Edits live in RAM; selecting a slot writes one
line (`~/.local/state/kiwi/current`); saving writes one small JSON file; starring a preset or
changing a mapping writes one file. The journal lives in RAM (32 MB cap), cron and every
maintenance timer are off, `timesyncd` no longer saves its clock every minute, and the
desktop, VNC, Bluetooth, CUPS, PipeWire/PulseAudio, telemetry and MODEP's UI are disabled or
masked (never uninstalled; the list and how to undo it are in `install.sh`). Verified with
`find -newer` under the stress test: no file changed.

### The web service

`kiwi-web` is Python 3 standard library only, one HTML file, Server‑Sent Events. It runs at
idle CPU and I/O priority with a 64 MB memory cap, talks to mod-host only while a page is
open or a change is pending (and only after the patch loader and the preset restore have
finished, since mod-host serves one client at a time), and lets go 10 s after the last
need — unless a Pianoteq or sampler-envelope CC mapping exists, in which case it stays
connected so a knob move is never delayed by a reconnect. Its MIDI monitor (`aseqdump`) runs permanently so mapped CCs and the button work with
no page open. The parameter list (`web/params.json`) is generated at install time from the
installed plugins' `lv2info`, so the page never guesses a range.

## Operations

```bash
systemctl status kiwi-host kiwi-patch kiwi-web   # is the instrument running?
~/kiwi/host/kiwi-check                            # full health check
~/kiwi/host/kiwi-stress 120                       # 2 min worst case; prints xruns and who was late
~/kiwi/host/kiwi-log                              # save this boot's logs to ~/kiwi-logs (one deliberate write)
~/kiwi/host/kiwi-log xruns                        # just the xrun summary
journalctl -u kiwi-patch -b                       # what the patch loader did this boot
journalctl -u kiwi-restore -b                     # which slot and mappings were applied
```

## Development

```bash
python3 -m unittest discover -s web/tests -t web   # ~30 s, no Pi needed
make -C plugins/kiwi test                          # DSP unit tests for the custom plugins
./deploy.sh                                        # rsync + install.sh on the Pi
./deploy.sh --no-install                           # rsync only
```

```
install.sh        idempotent installer, run on the Pi (preflight, packages, services, presets)
deploy.sh         rsync the repo to the Pi and run install.sh
host/             kiwi.patch, the loader, button scripts, kiwi-check / -stress / -log / -wifi
plugins/kiwi/     custom LV2 plugins (kiwi mix, kiwi carrier) with tests
sampler/          the SFZ instrument and its sample
web/              kiwi_web/ server package, static/index.html, tools/ generators, tests/
system/           systemd units, journald/timesyncd drop-ins, MIDI rules, sfizz build script
docs/superpowers/ design specs and implementation plans, as the thing was built
```

The web server is deliberately framework-free: it must start in a second on a Pi, use no
CPU while idle, and never need a package update on tour.

## Caveats and honest limits

- Built on **Patchbox OS + Pisound**. Another audio HAT means editing `/etc/jackdrc`, the
  port names in `kiwi.patch`, and dropping the button/LED bits.
- **Pianoteq 8.3.2** is what the preset conversion was verified with. A newer Pianoteq may
  change the state header; the installer warns, and the procedure for re-learning it is in
  `pianoteq_state.py`'s docstring (save a state from a fresh instance, read the header).
- No RT kernel: Patchbox's `PREEMPT` kernel with JACK at `-P 95` was enough for zero xruns
  here, but every system is its own.
- Loading a Pianoteq preset reloads the instrument: the piano goes quiet for about a second.
  That is Pianoteq, not kiwi.
- The page has no login. The hotspot password is the access control.

## Credits and license

Built on the shoulders of [mod-host](https://github.com/moddevices/mod-host),
[sfizz](https://sfz.tools/sfizz/), [amsynth](https://amsynth.github.io/),
[mda-lv2](https://drobilla.net/software/mda-lv2.html), [Guitarix](https://guitarix.org/)
(Zita Rev1), [Calf](https://calf-studio-gear.org/), [Dragonfly Reverb](https://michaelwillis.github.io/dragonfly-reverb/),
[x42 plugins](https://x42-plugins.com/), [rtpmidid](https://github.com/davidmoreno/rtpmidid),
[amidiminder](https://github.com/mzero/amidiminder), [Patchbox OS and Pisound](https://blokas.io/) by Blokas,
and [Pianoteq](https://www.modartt.com/pianoteq) by Modartt (not included; bring your own licence).

MIT — see [LICENSE](LICENSE).

#!/usr/bin/env bash
# Kiwi installer. Run on the Pi as user patch from ~/kiwi. Safe to re-run.
set -euo pipefail

KIWI_DIR=$(cd "$(dirname "$0")" && pwd)
# The v24.12 release asset rtpmidid_24.12.2_arm64.deb reports this package version.
RTPMIDID_DEB_VERSION="24.12~1~g66d57"

section() { printf '\n== %s\n' "$1"; }

section "Hostname"
# The device answers as kiwi.local (avahi follows the hostname; rtpmidid announces it).
if [ "$(hostnamectl --static)" != "kiwi" ]; then
    # /etc/hosts first, so sudo can resolve the new name.
    sudo sed -i 's/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t\tkiwi/' /etc/hosts
    sudo hostnamectl set-hostname kiwi
    sudo systemctl restart avahi-daemon
fi
hostnamectl --static

section "Packages"
# Only touch apt (which needs internet) when something is missing: in hotspot
# mode the Pi has no internet connection.
packages="dragonfly-reverb guitarix-lv2 mda-lv2 calf-plugins x42-plugins lilv-utils cmake build-essential lv2-dev"
# shellcheck disable=SC2086
if ! dpkg -s $packages >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq $packages
fi

section "sfizz"
"$KIWI_DIR/system/build-sfizz.sh"

section "rtpmidid"
if [ "$(dpkg-query -W -f='${Version}' rtpmidid 2>/dev/null || true)" != "$RTPMIDID_DEB_VERSION" ]; then
    deb=/tmp/rtpmidid_24.12.2_arm64.deb
    curl -sSL -o "$deb" "https://github.com/davidmoreno/rtpmidid/releases/download/v24.12/rtpmidid_24.12.2_arm64.deb"
    sudo apt-get install -y -qq "$deb"
fi
sudo install -m 644 "$KIWI_DIR/system/rtpmidid.ini" /etc/rtpmidid/kiwi.ini
sudo install -D -m 644 "$KIWI_DIR/system/rtpmidid-kiwi.conf" /etc/systemd/system/rtpmidid.service.d/kiwi.conf
sudo systemctl daemon-reload
sudo systemctl enable rtpmidid
sudo systemctl restart rtpmidid

section "Virtual MIDI device"
sudo install -m 644 "$KIWI_DIR/system/kiwi-modules.conf" /etc/modules-load.d/kiwi.conf
sudo install -m 644 "$KIWI_DIR/system/kiwi-modprobe.conf" /etc/modprobe.d/kiwi.conf
lsmod | grep -q '^snd_virmidi' || sudo modprobe snd-virmidi

section "MIDI routing"
# Replaces the /etc/amidiminder.rules symlink (to /etc/default/amidiminder.rules)
# with the kiwi rules; the Patchbox default file is left untouched.
sudo install -m 644 "$KIWI_DIR/system/amidiminder.rules" /etc/amidiminder.rules
sudo systemctl restart amidiminder

section "Kiwi plugins"
make -C "$KIWI_DIR/plugins/kiwi" clean test install

section "Pianoteq"
# Symlink so the original bundle in ~/.vst stays where it is.
mkdir -p "$HOME/.lv2"
ln -sfn "$HOME/.vst/Pianoteq 8.lv2" "$HOME/.lv2/Pianoteq 8.lv2"
prefs="$HOME/.config/Modartt/Pianoteq83.prefs"
if [ -f "$prefs" ] && grep -q '<VALUE name="voices" val="' "$prefs"; then
    sed -i 's/<VALUE name="voices" val="[0-9]*"\/>/<VALUE name="voices" val="24"\/>/' "$prefs"
    # Two engine threads spread the attack work of chords across cores.
    sed -i 's/<VALUE name="multicore" val="[0-9]*"\/>/<VALUE name="multicore" val="2"\/>/' "$prefs"
    grep -E 'name="(voices|multicore|engine_rate)"' "$prefs" || true
else
    echo "Pianoteq prefs not found or without a voices setting: $prefs (run Pianoteq once, then re-run install.sh)"
fi

section "Wi-Fi power save"
# Wi-Fi power saving causes latency spikes on the Pi; disable it on every Wi-Fi profile.
nmcli -t -f NAME,TYPE connection show | awk -F: '$2 == "802-11-wireless" { print $1 }' |
    while IFS= read -r wifi; do
        sudo nmcli connection modify "$wifi" 802-11-wireless.powersave 2
        echo "power save off: $wifi"
    done

section "JACK"
# Patchbox owns /etc/jackdrc; set it through its CLI, and only when it differs,
# since that restarts JACK (and with it the kiwi host).
if ! grep -q -- "-r 48000 -p 128 -n 2 " /etc/jackdrc; then
    sudo patchbox jack config --rate 48000 --buffer 128 --period 2
fi
tail -1 /etc/jackdrc

section "Host"
chmod +x "$KIWI_DIR/host/kiwi-load"
sudo install -m 644 "$KIWI_DIR/system/kiwi-host.service" "$KIWI_DIR/system/kiwi-patch.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable --now modep-mod-ui modep-mod-host 2>/dev/null || true
sudo systemctl enable kiwi-host kiwi-patch
sudo systemctl restart kiwi-host

section "System trim"
# Disabled or masked, never uninstalled: see README for how to undo.
sudo systemctl set-default multi-user.target
# patchbox-init re-activates the active Patchbox module (MODEP) on every boot,
# re-enabling its services; deactivate the module instead of fighting it.
if [ -n "$(sudo patchbox module active 2>/dev/null)" ]; then
    sudo patchbox module deactivate
fi
for unit in lightdm wayvnc-control patchbox-vnc.target modep-mod-host modep-mod-ui \
            modep-touchosc2midi touchosc2midi modep-update.path cups cups-browsed \
            bluetooth hciuart ModemManager blokas-telemetry.target wifi-hotspot \
            glamor-test rp1-test apt-daily.timer apt-daily-upgrade.timer \
            pisound-ctl triggerhappy triggerhappy.socket \
            man-db.timer dpkg-db-backup.timer e2scrub_all.timer fstrim.timer; do
    # pisound-ctl: Bluetooth link to the Pisound phone app (Bluetooth is off).
    # Timers: periodic maintenance that could fire mid-performance.
    sudo systemctl disable --now "$unit" 2>/dev/null || true
done
# D-Bus activated, so disabling is not enough: mask them.
for unit in packagekit rtkit-daemon; do
    sudo systemctl mask --now "$unit"
done
for unit in fluidsynth.service pipewire.service pipewire.socket pipewire-pulse.service \
            pipewire-pulse.socket wireplumber.service pulseaudio.service pulseaudio.socket; do
    sudo systemctl --global mask "$unit"
done
systemctl --user stop pulseaudio.service pulseaudio.socket 2>/dev/null || true

section "Journal in RAM"
sudo install -D -m 644 "$KIWI_DIR/system/journald-kiwi.conf" /etc/systemd/journald.conf.d/kiwi.conf
if [ -d /var/log/journal ]; then
    # The persistent journal (2.8 GB of xrun lines at one point) goes; logs now live in RAM.
    sudo systemctl restart systemd-journald
    sudo rm -rf /var/log/journal
fi
sudo systemctl restart systemd-journald
journalctl --disk-usage

section "No periodic disk writes"
# cron ran fake-hwclock hourly and apt/dpkg/logrotate/man-db daily; timesyncd saved
# its clock file every minute. Nothing on this box needs them.
sudo systemctl disable --now cron 2>/dev/null || true
sudo install -D -m 644 "$KIWI_DIR/system/timesyncd-kiwi.conf" /etc/systemd/timesyncd.conf.d/kiwi.conf
sudo systemctl restart systemd-timesyncd

section "Presets: button and LED"
for action in next prev save; do
    sudo ln -sfn "$KIWI_DIR/host/kiwi-btn" "/usr/local/bin/kiwi-btn-$action"
done
chmod +x "$KIWI_DIR/host/kiwi-btn"
sudo install -m 644 "$KIWI_DIR/system/pisound.conf" /etc/pisound.conf
sudo systemctl restart pisound-btn
sudo install -m 644 "$KIWI_DIR/system/kiwi-tmpfiles.conf" /etc/tmpfiles.d/kiwi.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/kiwi.conf

section "Web UI"
python3 "$KIWI_DIR/web/tools/gen_params.py" > "$KIWI_DIR/web/params.json.tmp"
mv "$KIWI_DIR/web/params.json.tmp" "$KIWI_DIR/web/params.json"
mkdir -p "$HOME/.local/state/kiwi"
sudo install -m 644 "$KIWI_DIR/system/kiwi-web.service" "$KIWI_DIR/system/kiwi-restore.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiwi-restore kiwi-web
sudo systemctl start kiwi-restore
sudo systemctl restart kiwi-web

echo
echo "kiwi install done"

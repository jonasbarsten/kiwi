#!/usr/bin/env bash
# Kiwi installer. Run on the Pi as user patch from ~/kiwi. Safe to re-run.
set -euo pipefail

KIWI_DIR=$(cd "$(dirname "$0")" && pwd)
# The v24.12 release asset rtpmidid_24.12.2_arm64.deb reports this package version.
RTPMIDID_DEB_VERSION="24.12~1~g66d57"

section() { printf '\n== %s\n' "$1"; }

section "Packages"
sudo apt-get update -qq
sudo apt-get install -y -qq dragonfly-reverb x42-plugins lilv-utils cmake build-essential lv2-dev

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
            glamor-test rp1-test apt-daily.timer apt-daily-upgrade.timer; do
    sudo systemctl disable "$unit" 2>/dev/null || true
done
for unit in fluidsynth.service pipewire.service pipewire.socket pipewire-pulse.service \
            pipewire-pulse.socket wireplumber.service; do
    sudo systemctl --global mask "$unit"
done

echo
echo "kiwi install done"

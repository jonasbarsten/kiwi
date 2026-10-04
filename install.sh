#!/usr/bin/env bash
# Kiwi installer. Run on the Pi as user patch from ~/kiwi. Safe to re-run.
set -euo pipefail

KIWI_DIR=$(cd "$(dirname "$0")" && pwd)
# The v24.12 release asset rtpmidid_24.12.2_arm64.deb reports this package version.
RTPMIDID_DEB_VERSION="24.12~1~g66d57"

section() { printf '\n== %s\n' "$1"; }
fail() { printf 'install.sh: %s\n' "$1" >&2; exit 1; }

section "Preflight"
# What this installer builds on and cannot provide itself.
[ "$(id -un)" = "patch" ] || fail "run as user patch (Patchbox OS's user; the patch file uses /home/patch)"
command -v jackd >/dev/null || fail "jackd not found: this is built for Patchbox OS"
command -v mod-host >/dev/null && [ -d /var/modep/lv2 ] ||
    fail "mod-host / MODEP plugins not found: install the MODEP module first (sudo patchbox module activate modep), then re-run"
pianoteq="$HOME/.vst/Pianoteq 8"
[ -x "$pianoteq" ] && [ -d "$pianoteq.lv2" ] ||
    fail "Pianoteq 8 not found: unpack the Linux ARM64 build so that '$pianoteq' and '$pianoteq.lv2' exist"
pianoteq_version=$("$pianoteq" --version 2>/dev/null | head -1 || true)
echo "${pianoteq_version:-Pianoteq: version unknown}"
prefs="$HOME/.config/Modartt/Pianoteq83.prefs"
if [ ! -f "$prefs" ]; then
    echo "WARNING: $prefs missing: run Pianoteq once (it writes its preferences), then re-run install.sh"
elif ! grep -q '<VALUE name="serial"' "$prefs"; then
    echo "WARNING: Pianoteq is not activated (\"$pianoteq\" --activate SERIAL, or activate it in its window)"
fi
case "$pianoteq_version" in
    *"8.3.2"*) ;;
    *) echo "WARNING: the LV2 preset conversion was verified with Pianoteq 8.3.2; check that presets change the sound (README: Pianoteq presets)" ;;
esac

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
ln -sfn "$pianoteq.lv2" "$HOME/.lv2/Pianoteq 8.lv2"
if [ -f "$prefs" ] && grep -q '<VALUE name="voices" val="' "$prefs"; then
    sed -i 's/<VALUE name="voices" val="[0-9]*"\/>/<VALUE name="voices" val="24"\/>/' "$prefs"
    # Two engine threads spread the attack work of chords across cores.
    sed -i 's/<VALUE name="multicore" val="[0-9]*"\/>/<VALUE name="multicore" val="2"\/>/' "$prefs"
    grep -E 'name="(voices|multicore|engine_rate)"' "$prefs" || true
else
    echo "Pianoteq prefs not found or without a voices setting: $prefs (run Pianoteq once, then re-run install.sh)"
fi

section "Wi-Fi connections"
# From kiwi.env (git-ignored, see kiwi.env.example): the kiwi hotspot phones join, and
# optionally a network with internet for development. Existing connections are kept.
env_file="$KIWI_DIR/kiwi.env"
if [ -f "$env_file" ]; then
    chmod 600 "$env_file"       # it holds Wi-Fi passwords
    # shellcheck source=/dev/null
    . "$env_file"
    if [ -n "${HOTSPOT_PASSWORD:-}" ] && [ "${#HOTSPOT_PASSWORD}" -lt 8 ]; then
        fail "HOTSPOT_PASSWORD in kiwi.env must be at least 8 characters (WPA2)"
    fi
    if ! nmcli -t -f NAME connection show | grep -Fqx kiwi-hotspot; then
        if [ -n "${HOTSPOT_PASSWORD:-}" ]; then
            sudo nmcli connection add type wifi ifname wlan0 con-name kiwi-hotspot autoconnect yes \
                ssid "${HOTSPOT_SSID:-kiwi}" 802-11-wireless.mode ap 802-11-wireless.band bg \
                802-11-wireless.channel 6 ipv4.method shared ipv6.method disabled \
                wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
                wifi-sec.psk "$HOTSPOT_PASSWORD" >/dev/null
            echo "created hotspot ${HOTSPOT_SSID:-kiwi}"
        else
            echo "no HOTSPOT_PASSWORD in kiwi.env: hotspot not created"
        fi
    fi
    if [ -n "${CLIENT_WIFI_SSID:-}" ] && ! nmcli -t -f NAME connection show | grep -Fqx "$CLIENT_WIFI_SSID"; then
        sudo nmcli connection add type wifi ifname wlan0 con-name "$CLIENT_WIFI_SSID" autoconnect no \
            ssid "$CLIENT_WIFI_SSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${CLIENT_WIFI_PASSWORD:-}" >/dev/null
        echo "created client connection $CLIENT_WIFI_SSID (switch with host/kiwi-wifi)"
    fi
    # Patchbox's own hotspot must not fight ours for the radio.
    sudo nmcli connection modify pb-hotspot connection.autoconnect no 2>/dev/null || true
else
    echo "no kiwi.env: Wi-Fi connections left as they are (see kiwi.env.example)"
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
for action in next prev save down up; do
    sudo ln -sfn "$KIWI_DIR/host/kiwi-btn" "/usr/local/bin/kiwi-btn-$action"
done
chmod +x "$KIWI_DIR/host/kiwi-btn"
sudo install -m 644 "$KIWI_DIR/system/pisound.conf" /etc/pisound.conf
sudo systemctl restart pisound-btn
sudo rm -f /etc/tmpfiles.d/kiwi.conf   # the LED permission is now set by kiwi-web.service

section "Pianoteq presets"
# Exported as LV2 preset bundles, outside LV2_PATH so mod-host's boot does not parse
# them; re-exported when the Pianoteq version changes. ~2 s, 3 MB. gen_presets (below)
# rewrites them into $presets_dir-lv2, the form the plugin actually restores.
presets_dir="$HOME/kiwi-data/pianoteq-presets"
rm -rf "$presets_dir-bundles"   # symlinks an earlier version used
if [ "$(cat "$presets_dir/.pianoteq-version" 2>/dev/null)" != "$pianoteq_version" ]; then
    mkdir -p "$presets_dir"
    (cd /tmp && env -u JACK_PROMISCUOUS_SERVER nice -n 19 ionice -c3 \
        "$pianoteq" --headless --export-lv2-presets "$presets_dir" --export-presets-filter all > /dev/null 2>&1)
    printf '%s\n' "$pianoteq_version" > "$presets_dir/.pianoteq-version"
fi
echo "$(ls -d "$presets_dir"/*.lv2 2>/dev/null | wc -l) preset bundles"

section "Web UI"
python3 "$KIWI_DIR/web/tools/gen_params.py" > "$KIWI_DIR/web/params.json.tmp"
mv "$KIWI_DIR/web/params.json.tmp" "$KIWI_DIR/web/params.json"
python3 "$KIWI_DIR/web/tools/gen_presets.py" "$presets_dir" "$presets_dir-lv2" > "$KIWI_DIR/web/presets.json.tmp"
mv "$KIWI_DIR/web/presets.json.tmp" "$KIWI_DIR/web/presets.json"
mkdir -p "$HOME/.local/state/kiwi"
sudo install -m 644 "$KIWI_DIR/system/kiwi-web.service" "$KIWI_DIR/system/kiwi-restore.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiwi-restore kiwi-web
sudo systemctl start kiwi-restore
sudo systemctl restart kiwi-web

echo
echo "kiwi install done"

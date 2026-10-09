#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ $(uname -s) != Linux || $EUID -ne 0 ]]; then
  echo 'À exécuter sur le Pi avec : sudo bash install.sh' >&2
  exit 1
fi
if ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
  echo 'Debian / Raspberry Pi OS avec systemd est nécessaire.' >&2
  exit 1
fi
package_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for file in app.py audio.py routing.json bureau-audio.service bureau-audio-dbus.conf bureau-audio-radio.service bureau-audio-radio.rules static/index.html static/app.js static/style.css static/icon.svg; do
  [[ -f "$package_dir/$file" ]] || { echo "Dossier incomplet : $file" >&2; exit 1; }
done
if [[ -f /opt/bureau-audio/app.py ]]; then
  echo 'Bureau Audio est déjà installé. Utilise sudo bash upgrade.sh.' >&2
  exit 1
fi
echo 'Installation des composants Debian disponibles…'
apt-get update
apt-get install -y python3-aiohttp python3-dbus-next alsa-utils bluez avahi-daemon polkitd rfkill
if { ! command -v bluealsa >/dev/null && ! command -v bluealsad >/dev/null; } || ! command -v bluealsa-aplay >/dev/null; then
  apt-get install -y bluez-alsa-utils
fi
for executable in python3 aplay amixer bluealsa-aplay busctl rfkill; do
  command -v "$executable" >/dev/null || { echo "Composant manquant : $executable. Consulte README.md." >&2; exit 1; }
done
if command -v bluealsad >/dev/null; then daemon_binary=$(command -v bluealsad)
elif command -v bluealsa >/dev/null; then daemon_binary=$(command -v bluealsa)
else echo 'Le daemon BlueALSA (bluealsa ou bluealsad) est manquant.' >&2; exit 1; fi
player_help=$(bluealsa-aplay --help 2>&1)
if [[ $player_help != *--volume* ]]; then
  echo 'Le lecteur BlueALSA doit prendre en charge --volume=software. Consulte README.md.' >&2
  exit 1
fi
routes_file="$package_dir/routing.json"
if [[ -f /etc/bureau-audio-routes.json ]]; then routes_file=/etc/bureau-audio-routes.json; fi
PYTHONDONTWRITEBYTECODE=1 python3 - "$package_dir" "$routes_file" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from app import VERSION, routing_config
from audio import AudioEngine
assert VERSION == '3.0'
routing_config(json.load(open(sys.argv[2])))
PY
install_metadata=/var/lib/bureau-audio-install
install_daemon_unit=/etc/systemd/system/bureau-audio-bluealsa.service
install_backup="/var/backups/bureau-audio/install-3.0-$(date +%Y%m%d-%H%M%S)-$$"
install -d -m 0700 "$install_backup/files"
install_complete=false
install_saved=false
install_daemon_created=false
install_player_active=false
if systemctl is-active --quiet bluealsa-aplay.service; then install_player_active=true; fi
install_player_enabled=$(systemctl is-enabled bluealsa-aplay.service 2>/dev/null || true)
if [[ -z $install_player_enabled ]]; then install_player_enabled=not-found; fi
install_files=(/etc/bureau-audio.env /etc/bureau-audio-routes.json /etc/systemd/system/bureau-audio.service /etc/systemd/system/bureau-audio-radio.service /etc/systemd/system/bluealsa-aplay.service "$install_daemon_unit" /etc/dbus-1/system.d/bureau-audio.conf /etc/polkit-1/rules.d/49-bureau-audio-radio.rules /var/lib/bureau-audio/settings.json)
rollback() {
  if [[ $install_complete != true && $install_saved == true ]]; then
    echo 'Installation interrompue : restauration de la configuration précédente…' >&2
    systemctl disable --now bureau-audio.service || true
    if [[ $install_daemon_created == true ]]; then systemctl disable --now bureau-audio-bluealsa.service || true; fi
    rm -rf /opt/bureau-audio
    if [[ -d "$install_backup/bureau-audio" ]]; then cp -a "$install_backup/bureau-audio" /opt/bureau-audio; fi
    for file in "${install_files[@]}"; do
      if [[ -e "$install_backup/files/$(basename "$file")" || -L "$install_backup/files/$(basename "$file")" ]]; then
        cp -a "$install_backup/files/$(basename "$file")" "$file"
      else rm -f "$file"; fi
    done
    rm -rf "$install_metadata"
    if [[ -d "$install_backup/install-metadata" ]]; then cp -a "$install_backup/install-metadata" "$install_metadata"; fi
    systemctl daemon-reload || true
    systemctl reload dbus.service || true
    case "$install_player_enabled" in
      enabled) systemctl enable bluealsa-aplay.service || true ;;
      enabled-runtime) systemctl enable --runtime bluealsa-aplay.service || true ;;
      masked|masked-runtime|linked|linked-runtime|not-found) : ;;
      *) systemctl disable bluealsa-aplay.service || true ;;
    esac
    if [[ $install_player_active == true ]]; then systemctl start bluealsa-aplay.service || true; fi
    echo "Sauvegarde : $install_backup" >&2
  fi
}
trap rollback EXIT
for file in "${install_files[@]}"; do
  if [[ -e "$file" || -L "$file" ]]; then cp -a "$file" "$install_backup/files/"; fi
done
if [[ -d /opt/bureau-audio ]]; then cp -a /opt/bureau-audio "$install_backup/bureau-audio"; fi
if [[ -d "$install_metadata" ]]; then cp -a "$install_metadata" "$install_backup/install-metadata"; fi
install_saved=true
if ! id bureau-audio >/dev/null 2>&1; then
  useradd --system --user-group --create-home --home-dir /var/lib/bureau-audio --shell /usr/sbin/nologin bureau-audio
fi
usermod -aG audio bureau-audio
install -d -m 0700 -o bureau-audio -g bureau-audio /var/lib/bureau-audio
install -d -m 0700 -o root -g root "$install_metadata"
if [[ ! -f "$install_metadata/legacy-player.json" ]]; then
  python3 - "$install_metadata/legacy-player.json" "$install_player_enabled" "$install_player_active" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'unit': 'bluealsa-aplay.service', 'enabled': sys.argv[2], 'active': sys.argv[3] == 'true'}))
PY
  if [[ -e /etc/systemd/system/bluealsa-aplay.service || -L /etc/systemd/system/bluealsa-aplay.service ]]; then
    cp -a /etc/systemd/system/bluealsa-aplay.service "$install_metadata/legacy-player.service"
  fi
fi
if [[ $install_player_enabled != not-found || $install_player_active == true ]]; then
  systemctl stop bluealsa-aplay.service
  case "$install_player_enabled" in linked|linked-runtime|masked|masked-runtime) : ;; *) systemctl disable bluealsa-aplay.service ;; esac
fi
if ! busctl --system status org.bluealsa >/dev/null 2>&1; then
  if systemctl is-active --quiet bluealsa.service || systemctl is-active --quiet bluealsad.service; then
    echo 'Un daemon BlueALSA est actif mais org.bluealsa est indisponible. Sa configuration est conservée ; vérifie son nom D-Bus.' >&2
    exit 1
  fi
  if [[ -e "$install_daemon_unit" && ! -f "$install_metadata/owned-daemon.json" ]]; then
    echo 'L’unité bureau-audio-bluealsa.service existe déjà et n’appartient pas à cette installation.' >&2
    exit 1
  fi
  install_daemon_created=true
  install -d -m 0755 /etc/systemd/system
  cat > "$install_daemon_unit" <<UNIT
[Unit]
Description=Bureau Audio - reception Bluetooth A2DP
Wants=bluetooth.service
After=bluetooth.service
[Service]
Type=simple
ExecStart="$daemon_binary" -p a2dp-sink --initial-volume=30
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
  chmod 0644 "$install_daemon_unit"
  python3 - "$install_metadata/owned-daemon.json" <<'PY'
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'unit': 'bureau-audio-bluealsa.service', 'created': True}))
PY
  systemctl daemon-reload
  systemctl enable --now bureau-audio-bluealsa.service
fi
install -d -m 0755 /opt/bureau-audio /opt/bureau-audio/static
for file in app.py audio.py routing.json; do install -m 0644 "$package_dir/$file" "/opt/bureau-audio/$file"; done
for asset in index.html app.js style.css icon.svg; do install -m 0644 "$package_dir/static/$asset" "/opt/bureau-audio/static/$asset"; done
if [[ ! -f /etc/bureau-audio-routes.json ]]; then install -m 0644 "$package_dir/routing.json" /etc/bureau-audio-routes.json; fi
if [[ ! -f /etc/bureau-audio.env ]]; then
  access_key=$(python3 -c 'import secrets; print(secrets.token_hex(12))')
  cat > /etc/bureau-audio.env <<CONFIG
BUREAU_KEY=$access_key
BUREAU_HOST=0.0.0.0
BUREAU_PORT=8088
BUREAU_STATE=/var/lib/bureau-audio
BUREAU_ROUTES=/etc/bureau-audio-routes.json
CONFIG
fi
chmod 0600 /etc/bureau-audio.env
install -m 0644 "$package_dir/bureau-audio.service" /etc/systemd/system/bureau-audio.service
install -m 0644 "$package_dir/bureau-audio-radio.service" /etc/systemd/system/bureau-audio-radio.service
install -d -m 0755 /etc/polkit-1/rules.d /etc/dbus-1/system.d
install -m 0644 "$package_dir/bureau-audio-radio.rules" /etc/polkit-1/rules.d/49-bureau-audio-radio.rules
install -m 0644 "$package_dir/bureau-audio-dbus.conf" /etc/dbus-1/system.d/bureau-audio.conf
systemctl reload dbus.service
systemctl daemon-reload
systemctl enable --now bluetooth.service avahi-daemon.service bureau-audio.service
install_port=$(sed -n 's/^BUREAU_PORT=//p' /etc/bureau-audio.env | tail -n 1)
if [[ ! $install_port =~ ^[0-9]+$ ]]; then install_port=8088; fi
ready=false
for attempt in {1..20}; do
  if python3 - "$install_port" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
result = json.load(urllib.request.urlopen('http://127.0.0.1:' + sys.argv[1] + '/health', timeout=1))
assert result['version'] == '3.0'
PY
  then ready=true; break; fi
  sleep 1
done
if [[ $ready != true ]]; then journalctl -u bureau-audio -n 40 --no-pager >&2; exit 1; fi
install_complete=true
echo "Bureau Audio 3.0 installé : http://$(hostname).local:$install_port"
echo 'Code d’accès :'
sed -n 's/^BUREAU_KEY=//p' /etc/bureau-audio.env
echo 'Choisis ta sortie et les récepteurs dans la page de configuration.'
echo 'L’application possède le lecteur audio ; aucun fichier ALSA global n’a été modifié.'
echo "Sauvegarde : $install_backup"

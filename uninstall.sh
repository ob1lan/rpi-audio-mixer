#!/usr/bin/env bash
set -euo pipefail
if [[ $(uname -s) != Linux || $EUID -ne 0 ]]; then
  echo 'À exécuter sur le Pi avec : sudo bash uninstall.sh' >&2
  exit 1
fi
uninstall_metadata=/var/lib/bureau-audio-install
uninstall_player_enabled=not-found
uninstall_player_active=false
if [[ -f "$uninstall_metadata/legacy-player.json" ]]; then
  uninstall_player_state=$(python3 - "$uninstall_metadata/legacy-player.json" <<'PY'
import json, sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
assert record['unit'] == 'bluealsa-aplay.service'
assert record['enabled'] in ('enabled', 'enabled-runtime', 'disabled', 'static', 'indirect', 'generated', 'transient', 'linked', 'linked-runtime', 'masked', 'masked-runtime', 'not-found')
assert isinstance(record['active'], bool)
print(record['enabled'])
print('true' if record['active'] else 'false')
PY
  )
  uninstall_player_enabled=${uninstall_player_state%%$'\n'*}
  uninstall_player_active=${uninstall_player_state##*$'\n'}
fi
systemctl disable --now bureau-audio.service || true
if [[ -f /opt/bureau-audio/app.py && -f /etc/bureau-audio-routes.json ]]; then
  if ! BUREAU_ROUTES=/etc/bureau-audio-routes.json BUREAU_STATE=/var/lib/bureau-audio python3 /opt/bureau-audio/app.py --restore-routing; then
    echo 'Rebranche les récepteurs absents puis relance la désinstallation. Le programme est conservé pour restaurer leurs réglages.' >&2
    exit 1
  fi
fi
systemctl stop bureau-audio-radio.service 2>/dev/null || true
if [[ -f "$uninstall_metadata/owned-daemon.json" ]]; then
  python3 - "$uninstall_metadata/owned-daemon.json" <<'PY'
import json, sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
assert record == {'unit': 'bureau-audio-bluealsa.service', 'created': True}
PY
  systemctl disable --now bureau-audio-bluealsa.service
  rm -f /etc/systemd/system/bureau-audio-bluealsa.service
  rm -f /etc/dbus-1/system.d/bureau-audio-bluealsa.conf
  rm -f "$uninstall_metadata/owned-daemon.json"
fi
rm -f /etc/systemd/system/bureau-audio.service /etc/systemd/system/bureau-audio-radio.service
rm -f /etc/dbus-1/system.d/bureau-audio.conf /etc/polkit-1/rules.d/49-bureau-audio-radio.rules
rm -rf /opt/bureau-audio
if [[ -f "$uninstall_metadata/meter-managed" ]]; then
  if [[ -f "$uninstall_metadata/meter.conf.before" ]]; then
    cp -a "$uninstall_metadata/meter.conf.before" /etc/modules-load.d/bureau-audio-meter.conf
  else
    rm -f /etc/modules-load.d/bureau-audio-meter.conf
  fi
  rm -f "$uninstall_metadata/meter-managed" "$uninstall_metadata/meter.conf.before"
fi
if [[ -f "$uninstall_metadata/daemon-boot.json" ]]; then
  uninstall_boot_units=$(python3 - "$uninstall_metadata/daemon-boot.json" <<'PY'
import json, sys
from pathlib import Path
units = json.loads(Path(sys.argv[1]).read_text())
assert isinstance(units, list) and all(u in ('bluealsa.service', 'bluealsad.service') for u in units)
print('\n'.join(units))
PY
  )
  for unit in $uninstall_boot_units; do systemctl disable "$unit"; done
  rm -f "$uninstall_metadata/daemon-boot.json"
fi
if [[ ! -e /etc/systemd/system/bluealsa-aplay.service && -e "$uninstall_metadata/legacy-player.service" ]]; then
  cp -a "$uninstall_metadata/legacy-player.service" /etc/systemd/system/bluealsa-aplay.service
fi
systemctl reload dbus.service
systemctl daemon-reload
case "$uninstall_player_enabled" in
  enabled) systemctl enable bluealsa-aplay.service ;;
  enabled-runtime) systemctl enable --runtime bluealsa-aplay.service ;;
  masked|masked-runtime|linked|linked-runtime|not-found) : ;;
  *) systemctl disable bluealsa-aplay.service ;;
esac
if [[ $uninstall_player_active == true ]]; then systemctl start bluealsa-aplay.service
elif [[ $uninstall_player_enabled != not-found ]]; then systemctl stop bluealsa-aplay.service; fi
rm -f "$uninstall_metadata/legacy-player.json" "$uninstall_metadata/legacy-player.service"
rmdir "$uninstall_metadata" 2>/dev/null || true
echo 'Interface retirée, anciens réglages Bluetooth et état du lecteur audio restaurés.'
echo 'Les préférences, le code d’accès et la configuration ALSA sont conservés.'

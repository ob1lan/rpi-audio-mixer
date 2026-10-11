"""Install, handover, rollback and uninstall in a completely temporary fake Pi."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScriptFixture:
    def __init__(self, owner, installed=True, daemon=True, ready=True, modern_daemon=False):
        self.temporary = tempfile.TemporaryDirectory()
        owner.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / 'package'
        shutil.copytree(ROOT, self.package, ignore=shutil.ignore_patterns('__pycache__', '.venv', '.git', 'demo-state*'))
        for folder in ('opt', 'etc/systemd/system', 'etc/dbus-1/system.d', 'etc/polkit-1/rules.d', 'var/lib/bureau-audio'):
            (self.root / folder).mkdir(parents=True)
        self.original = json.dumps({'master': {'volume': 42, 'gain': 1, 'muted': False}})
        (self.root / 'var/lib/bureau-audio/settings.json').write_text(self.original)
        shutil.copyfile(ROOT / 'routing.json', self.root / 'etc/bureau-audio-routes.json')
        (self.root / 'etc/bureau-audio.env').write_text('BUREAU_KEY=conserver\nBUREAU_CARD=Headphones\n')
        if installed:
            target = self.root / 'opt/bureau-audio'
            (target / 'static').mkdir(parents=True)
            (target / 'app.py').write_text('# ancienne version\n')
            (target / 'static/index.html').write_text('ancienne interface')
            (self.root / 'etc/systemd/system/bureau-audio.service').write_text('ancienne unité web')
        (self.root / 'etc/systemd/system/bluealsa-aplay.service').write_text('ancien lecteur personnalisé')
        services = {
            'bureau-audio.service': {'active': installed, 'enabled': 'enabled' if installed else 'not-found'},
            'bluealsa-aplay.service': {'active': True, 'enabled': 'enabled'},
            'bluealsa.service': {'active': daemon, 'enabled': 'enabled' if daemon else 'not-found'},
            'bluealsad.service': {'active': False, 'enabled': 'not-found'},
            'bureau-audio-bluealsa.service': {'active': False, 'enabled': 'not-found'},
        }
        (self.root / 'services.json').write_text(json.dumps(services))
        for name in ('upgrade.sh', 'install.sh', 'prepare-bluealsa.sh', 'uninstall.sh'):
            script = (self.package / name).read_text()
            for prefix in ('/opt/', '/etc/', '/var/lib/', '/var/backups/'):
                script = script.replace(prefix, str(self.root) + prefix)
            script = script.replace('$EUID -ne 0', '0 -ne 0')
            (self.package / name).write_text(script)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        def executable(name, body):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        for name in ('sleep', 'apt-get', 'useradd', 'usermod', 'rfkill', 'aplay', 'amixer', 'modprobe'):
            executable(name, '#!/bin/sh\nexit 0\n')
        executable('id', '#!/bin/sh\nexit 0\n')
        executable('uname', '#!/bin/sh\necho Linux\n')
        executable('journalctl', '#!/bin/sh\necho diagnostic-simule\n')
        executable('bluealsad' if modern_daemon else 'bluealsa', '#!/bin/sh\nexit 0\n')
        executable('bluealsa-aplay', '#!/bin/sh\necho --volume=software\n')
        executable('python3', '#!/bin/sh\n'
                   'case "$1" in */configure.py) printf "%s\\n" "$@" > "$FIXTURE_ROOT/configure.log"; exit "${FIXTURE_CONFIGURE:-0}" ;; esac\n'
                   'if [ "$#" = "2" ] && [ "$1" = "-" ]; then\n'
                   ' case "$2" in *[!0-9]*) ;; *) cat >/dev/null; exit "$FIXTURE_READY" ;; esac\n'
                   'fi\n'
                   'if [ "$#" = "2" ] && [ "$2" = "--restore-routing" ]; then exit 0; fi\n'
                   'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
        installer = shutil.which('install')
        executable('install', '#!' + sys.executable + '\n'
                   'import os, sys\n'
                   'arguments=[]; i=1\n'
                   'while i<len(sys.argv):\n'
                   ' if sys.argv[i] in ("-o", "-g"): i+=2; continue\n'
                   ' arguments.append(sys.argv[i]); i+=1\n'
                   'os.execv(' + repr(installer) + ', [' + repr(installer) + ']+arguments)\n')
        executable('systemctl', '#!' + sys.executable + '\n'
                   'import os, sys, json\nfrom pathlib import Path\n'
                   'base=Path(os.environ["FIXTURE_ROOT"]); args=sys.argv[1:]\n'
                   'with (base/"service.log").open("a") as log: log.write(" ".join(args)+"\\n")\n'
                   'path=base/"services.json"; services=json.loads(path.read_text())\n'
                   'units=[a for a in args[1:] if not a.startswith("-")]\n'
                   'for unit in units:\n'
                   ' state=services.setdefault(unit,{"enabled":"not-found","active":False})\n'
                   ' if args[0]=="is-active":\n'
                   '  if "--quiet" not in args: print("active" if state["active"] else "inactive")\n'
                   '  sys.exit(0 if state["active"] else 3)\n'
                   ' if args[0]=="is-enabled":\n'
                   '  print(state["enabled"] if state["enabled"]!="not-found" else "")\n'
                   '  sys.exit(0 if state["enabled"] in ("enabled","enabled-runtime") else 1)\n'
                   ' if args[0] in ("enable","disable"):\n'
                   '  state["enabled"]=("enabled-runtime" if "--runtime" in args else "enabled") if args[0]=="enable" else "disabled"\n'
                   ' if args[0] in ("start","stop") or "--now" in args:\n'
                   '  state["active"]=args[0] in ("start","enable")\n'
                   ' if unit=="bureau-audio.service" and state["active"] and args[0] in ("start","enable"):\n'
                   '  source=base/"opt/bureau-audio/app.py"\n'
                   '  if source.exists() and "VERSION" in source.read_text():\n'
                   '   settings=base/"var/lib/bureau-audio/settings.json"\n'
                   '   record=json.loads(settings.read_text()) if settings.exists() else {}\n'
                   '   record["test_migrated"]=True; settings.write_text(json.dumps(record))\n'
                   'path.write_text(json.dumps(services))\n')
        executable('busctl', '#!' + sys.executable + '\n'
                   'import os, sys, json\nfrom pathlib import Path\n'
                   'states=json.loads((Path(os.environ["FIXTURE_ROOT"])/"services.json").read_text())\n'
                   'sys.exit(0 if any(states.get(unit,{}).get("active") for unit in ("bluealsa.service","bluealsad.service","bureau-audio-bluealsa.service")) else 1)\n')
        self.environment = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                            'FIXTURE_ROOT': str(self.root), 'FIXTURE_READY': '0' if ready else '1',
                            'PYTHONDONTWRITEBYTECODE': '1'}

    def run(self, script='upgrade.sh', ready=None, arguments=()):
        environment = dict(self.environment)
        if ready is not None: environment['FIXTURE_READY'] = '0' if ready else '1'
        return subprocess.run(['bash', str(self.package / script), *arguments], env=environment,
                              capture_output=True, text=True, timeout=20)

    def states(self):
        return json.loads((self.root / 'services.json').read_text())


class UpgradeTests(unittest.TestCase):
    def test_success_transfers_player_and_preserves_routes_and_key(self):
        fixture = ScriptFixture(self)
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = fixture.root / 'opt/bureau-audio'
        self.assertIn("VERSION = '3.0'", (target / 'app.py').read_text())
        self.assertTrue((target / 'audio.py').exists())
        self.assertEqual((fixture.root / 'etc/bureau-audio.env').read_text(), 'BUREAU_KEY=conserver\nBUREAU_CARD=Headphones\n')
        self.assertEqual(json.loads((fixture.root / 'etc/bureau-audio-routes.json').read_text()), json.loads((ROOT / 'routing.json').read_text()))
        state = fixture.states()
        self.assertEqual(state['bluealsa-aplay.service'], {'active': False, 'enabled': 'disabled'})
        self.assertTrue(state['bluealsa.service']['active'])
        metadata = json.loads((fixture.root / 'var/lib/bureau-audio-install/legacy-player.json').read_text())
        self.assertEqual(metadata, {'unit': 'bluealsa-aplay.service', 'enabled': 'enabled', 'active': True})
        unit = (fixture.root / 'etc/systemd/system/bureau-audio.service').read_text()
        self.assertNotIn('bluealsa-aplay.service', unit)
        self.assertFalse((fixture.root / 'etc/systemd/system/bureau-audio-bluealsa.service').exists())

    def test_failed_start_restores_code_state_unit_and_player(self):
        fixture = ScriptFixture(self, ready=False)
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((fixture.root / 'opt/bureau-audio/app.py').read_text(), '# ancienne version\n')
        self.assertFalse((fixture.root / 'opt/bureau-audio/audio.py').exists())
        self.assertEqual((fixture.root / 'var/lib/bureau-audio/settings.json').read_text(), fixture.original)
        self.assertEqual((fixture.root / 'etc/systemd/system/bureau-audio.service').read_text(), 'ancienne unité web')
        self.assertEqual(fixture.states()['bluealsa-aplay.service'], {'active': True, 'enabled': 'enabled'})
        self.assertFalse((fixture.root / 'var/lib/bureau-audio-install').exists())

    def test_second_upgrade_rollback_restores_immediate_disabled_player_state(self):
        fixture = ScriptFixture(self)
        self.assertEqual(fixture.run().returncode, 0)
        original_metadata = (fixture.root / 'var/lib/bureau-audio-install/legacy-player.json').read_text()
        result = fixture.run(ready=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.states()['bluealsa-aplay.service'], {'active': False, 'enabled': 'disabled'})
        self.assertEqual((fixture.root / 'var/lib/bureau-audio-install/legacy-player.json').read_text(), original_metadata)

    def test_owned_new_daemon_is_removed_after_failed_upgrade(self):
        fixture = ScriptFixture(self, daemon=False, ready=False, modern_daemon=True)
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((fixture.root / 'etc/systemd/system/bureau-audio-bluealsa.service').exists())
        self.assertFalse(fixture.states()['bureau-audio-bluealsa.service']['active'])
        self.assertTrue(fixture.states()['bluealsa-aplay.service']['active'])

    def test_uninstall_restores_original_player_and_preserves_existing_daemon(self):
        fixture = ScriptFixture(self)
        self.assertEqual(fixture.run().returncode, 0)
        result = fixture.run('uninstall.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((fixture.root / 'opt/bureau-audio').exists())
        self.assertTrue(fixture.states()['bluealsa.service']['active'])
        self.assertEqual(fixture.states()['bluealsa-aplay.service'], {'active': True, 'enabled': 'enabled'})
        self.assertTrue((fixture.root / 'etc/bureau-audio.env').exists())
        self.assertTrue((fixture.root / 'var/lib/bureau-audio/settings.json').exists())

    def test_fresh_install_accepts_bluealsad_and_creates_owned_sink(self):
        fixture = ScriptFixture(self, installed=False, daemon=False, modern_daemon=True)
        result = fixture.run('install.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((fixture.root / 'opt/bureau-audio/audio.py').exists())
        unit = (fixture.root / 'etc/systemd/system/bureau-audio-bluealsa.service').read_text()
        self.assertIn('bluealsad" -p a2dp-sink', unit)
        self.assertTrue(fixture.states()['bureau-audio-bluealsa.service']['active'])
        self.assertEqual((fixture.root / 'etc/bureau-audio.env').read_text(), 'BUREAU_KEY=conserver\nBUREAU_CARD=Headphones\n')
        self.assertEqual(fixture.run('uninstall.sh').returncode, 0)
        self.assertFalse((fixture.root / 'etc/systemd/system/bureau-audio-bluealsa.service').exists())
        self.assertFalse(fixture.states()['bureau-audio-bluealsa.service']['active'])
        self.assertTrue(fixture.states()['bluealsa-aplay.service']['active'])

    def test_fresh_install_failure_restores_player_and_existing_preferences(self):
        fixture = ScriptFixture(self, installed=False, daemon=False, ready=False)
        result = fixture.run('install.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((fixture.root / 'opt/bureau-audio').exists())
        self.assertEqual((fixture.root / 'var/lib/bureau-audio/settings.json').read_text(), fixture.original)
        self.assertEqual(fixture.states()['bluealsa-aplay.service'], {'active': True, 'enabled': 'enabled'})
        self.assertFalse((fixture.root / 'etc/systemd/system/bureau-audio-bluealsa.service').exists())

    def test_unsupported_player_is_rejected_before_stopping_existing_audio(self):
        fixture = ScriptFixture(self)
        (fixture.bin / 'bluealsa-aplay').write_text('#!/bin/sh\necho ancien-lecteur-sans-option\n')
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--volume=software', result.stderr)
        self.assertTrue(fixture.states()['bluealsa-aplay.service']['active'])
        self.assertEqual((fixture.root / 'opt/bureau-audio/app.py').read_text(), '# ancienne version\n')
        self.assertFalse((fixture.root / 'var/lib/bureau-audio-install').exists())

    def test_linked_manual_player_remains_linked_and_restores_active_state(self):
        fixture = ScriptFixture(self)
        services = fixture.states()
        services['bluealsa-aplay.service']['enabled'] = 'linked'
        (fixture.root / 'services.json').write_text(json.dumps(services))
        self.assertEqual(fixture.run().returncode, 0)
        self.assertEqual(fixture.states()['bluealsa-aplay.service'], {'enabled': 'linked', 'active': False})
        self.assertEqual(fixture.run('uninstall.sh').returncode, 0)
        self.assertEqual(fixture.states()['bluealsa-aplay.service'], {'enabled': 'linked', 'active': True})

    def test_configure_loads_meter_at_boot_and_forwards_output(self):
        fixture = ScriptFixture(self, installed=False, daemon=False)
        result = fixture.run('install.sh', arguments=('--configure', '--non-interactive', '--output', 'USB:0'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((fixture.root / 'etc/modules-load.d/bureau-audio-meter.conf').read_text(), 'snd-aloop\n')
        self.assertIn('USB:0', (fixture.root / 'configure.log').read_text())
        self.assertTrue((fixture.root / 'opt/bureau-audio/static/knob-scale.svg').is_file())
        self.assertEqual(fixture.run('uninstall.sh').returncode, 0)
        self.assertFalse((fixture.root / 'etc/modules-load.d/bureau-audio-meter.conf').exists())

    def test_failed_install_restores_existing_meter_configuration(self):
        fixture = ScriptFixture(self, installed=False, daemon=False, ready=False)
        meter = fixture.root / 'etc/modules-load.d/bureau-audio-meter.conf'
        meter.parent.mkdir()
        meter.write_text('# original\nsnd-aloop\n')
        result = fixture.run('install.sh', arguments=('--configure',))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(meter.read_text(), '# original\nsnd-aloop\n')

    def test_incomplete_configuration_keeps_service_but_reports_failure(self):
        fixture = ScriptFixture(self, installed=False, daemon=False)
        fixture.environment['FIXTURE_CONFIGURE'] = '1'
        result = fixture.run('install.sh', arguments=('--configure',))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(fixture.states()['bureau-audio.service']['active'])
        self.assertTrue((fixture.root / 'opt/bureau-audio/app.py').exists())
        self.assertIn('http://', result.stdout)

    def test_uninstall_restores_previous_meter_file_after_repeated_setup(self):
        fixture = ScriptFixture(self, installed=False, daemon=False)
        meter = fixture.root / 'etc/modules-load.d/bureau-audio-meter.conf'
        meter.parent.mkdir()
        meter.write_text('# previous custom file\n')
        self.assertEqual(fixture.run('install.sh', arguments=('--configure',)).returncode, 0)
        self.assertEqual(fixture.run('upgrade.sh', arguments=('--configure',)).returncode, 0)
        self.assertEqual(fixture.run('uninstall.sh').returncode, 0)
        self.assertEqual(meter.read_text(), '# previous custom file\n')

    def test_setup_enables_running_daemon_for_boot_and_uninstall_restores_state(self):
        fixture = ScriptFixture(self, installed=False)
        states = fixture.states()
        states['bluealsa.service']['enabled'] = 'disabled'
        (fixture.root / 'services.json').write_text(json.dumps(states))
        self.assertEqual(fixture.run('install.sh', arguments=('--configure',)).returncode, 0)
        self.assertEqual(fixture.states()['bluealsa.service'], {'active': True, 'enabled': 'enabled'})
        self.assertEqual(fixture.run('uninstall.sh').returncode, 0)
        self.assertEqual(fixture.states()['bluealsa.service'], {'active': True, 'enabled': 'disabled'})

    def test_failed_setup_restores_daemon_boot_state(self):
        fixture = ScriptFixture(self, installed=False, ready=False)
        states = fixture.states()
        states['bluealsa.service']['enabled'] = 'disabled'
        (fixture.root / 'services.json').write_text(json.dumps(states))
        self.assertNotEqual(fixture.run('install.sh', arguments=('--configure',)).returncode, 0)
        self.assertEqual(fixture.states()['bluealsa.service'], {'active': True, 'enabled': 'disabled'})

    def test_missing_loopback_does_not_claim_complete_setup(self):
        fixture = ScriptFixture(self, installed=False)
        (fixture.bin / 'modprobe').write_text('#!/bin/sh\nexit 1\n')
        result = fixture.run('install.sh', arguments=('--configure',))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(fixture.states()['bureau-audio.service']['active'])
        self.assertFalse((fixture.root / 'etc/modules-load.d/bureau-audio-meter.conf').exists())
        self.assertIn('snd-aloop indisponible', result.stderr)


if __name__ == '__main__': unittest.main()

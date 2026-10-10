"""Download/setup failures and first configuration, without touching a real Pi."""
import copy
import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from configure import Terminal, choose_output, configure, read_environment
from aiohttp.test_utils import TestServer
from app import DemoBackend, StateFile, create_app

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.archive = self.root / 'repo.tar.gz'
        self.log = self.root / 'curl.json'
        self.calls = self.root / 'installed'
        self.environment = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                            'ARCHIVE': str(self.archive), 'CURL_LOG': str(self.log), 'CALL_FILE': str(self.calls)}
        self.environment.pop('GH_TOKEN', None)
        self.environment.pop('GITHUB_TOKEN', None)
        for name, body in {
            'uname': '#!/bin/sh\necho Linux\n',
            'id': '#!/bin/sh\necho 0\n',
            'curl': '#!' + sys.executable + '\nimport json, os, shutil, sys\n'
                    'args=sys.argv[1:]\n'
                    'if "--config" in args: sys.stdin.read()\n'
                    'open(os.environ["CURL_LOG"],"w").write(json.dumps(args))\n'
                    'if os.environ.get("DOWNLOAD_FAIL"): sys.exit(22)\n'
                    'shutil.copyfile(os.environ["ARCHIVE"], args[args.index("-o")+1])\n',
        }.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.make_archive()

    def make_archive(self, unsafe=None):
        script = ('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALL_FILE"\n'
                  'printf "%s%s" "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}" > "$CALL_FILE.credentials"\n'
                  'exit "${INSTALL_FAIL:-0}"\n')
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name in ('install.sh', 'configure.py', 'prepare-bluealsa.sh', 'app.py', 'audio.py'):
                content = (script if name == 'install.sh' else '# fixture\n').encode()
                entry = tarfile.TarInfo('repo-sha/' + name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            if unsafe:
                entry = tarfile.TarInfo(unsafe)
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b'x'))

    def run_bootstrap(self, *args):
        return subprocess.run(['sh', str(ROOT / 'bootstrap.sh'), *args], env=self.environment,
                              capture_output=True, text=True, timeout=10)

    def test_downloads_snapshot_and_forwards_setup_options(self):
        result = self.run_bootstrap('--ref', 'v3.0', '--non-interactive', '--output', 'USB:0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(),
                         ['--upgrade', '--configure', '--non-interactive', '--output', 'USB:0'])
        self.assertIn('https://api.github.com/repos/ob1lan/rpi-audio-mixer/tarball/v3.0', self.log.read_text())

    def test_download_failure_never_runs_setup(self):
        self.environment['DOWNLOAD_FAIL'] = '1'
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_archive_traversal_never_runs_setup(self):
        self.make_archive('repo-sha/../../escape')
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_archive_symlink_never_runs_setup(self):
        with tarfile.open(self.archive, 'w:gz') as archive:
            entry = tarfile.TarInfo('repo-sha/install.sh')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/etc/passwd'
            archive.addfile(entry)
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_repository_argument_rejected_before_download(self):
        self.assertNotEqual(self.run_bootstrap('--repo', 'owner/repo?other').returncode, 0)
        self.assertFalse(self.log.exists())

    def test_private_credential_not_in_process_arguments_or_setup_environment(self):
        self.environment['GH_TOKEN'] = 'github_pat_test'
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('github_pat_test', self.log.read_text())
        self.assertEqual(Path(str(self.calls) + '.credentials').read_text(), '')
        self.assertIn('--config', self.log.read_text())

    def test_installer_failure_is_returned(self):
        self.environment['INSTALL_FAIL'] = '17'
        self.assertEqual(self.run_bootstrap().returncode, 17)


def choice(card='USB', device=0):
    return {'card': card, 'device': device, 'name': card, 'pcm': 'plughw:CARD={},DEV={}'.format(card, device),
            'controls': [{'name': 'Hardware'}, {'name': 'Bureau Audio Master', 'software': True}]}


class FakeClient:
    def __init__(self, ready=True, blocked=False):
        self.calls = []
        self.blocked = blocked
        self.status = {'master_ready': ready, 'configuration': {'output': None, 'output_choices': [choice()], 'diagnostics': []},
                       'adapters': [{'address': '02:BA:00:00:00:01', 'alias': 'Internal', 'present': True, 'powered': False},
                                    {'address': '02:BA:00:00:00:02', 'alias': 'USB', 'present': True, 'powered': False}]}

    def request(self, path, data=None):
        self.calls.append((path, data))
        if path == '/api/radio':
            if self.blocked:
                raise RuntimeError('Operation blocked through rfkill')
            next(a for a in self.status['adapters'] if a['address'] == data['adapter'])['powered'] = True
        return copy.deepcopy(self.status)


class ConfigurationTests(unittest.TestCase):
    def test_single_output_uses_real_software_control(self):
        output = choose_output({'output_choices': [choice()]}, non_interactive=True)
        self.assertEqual(output['control'], 'Bureau Audio Master')
        self.assertEqual(output['pcm'], 'plughw:CARD=USB,DEV=0')

    def test_ambiguous_headless_output_requires_explicit_destination(self):
        with self.assertRaisesRegex(RuntimeError, '--output'):
            choose_output({'output_choices': [choice(), choice('HDMI')]}, non_interactive=True)
        output = choose_output({'output_choices': [choice(), choice('HDMI')]}, 'HDMI:0', True)
        self.assertEqual(output['card'], 'HDMI')

    @unittest.skipUnless(hasattr(os, 'openpty'), 'Requires a POSIX terminal')
    def test_output_selection_reads_from_a_non_seekable_terminal(self):
        master, slave = os.openpty()
        try:
            terminal = Terminal(os.ttyname(slave))
            try:
                os.write(master, b'2\n')
                output = choose_output({'output_choices': [choice(), choice('HDMI')]}, terminal=terminal)
                self.assertEqual(output['card'], 'HDMI')
            finally:
                terminal.close()
        finally:
            os.close(master)
            os.close(slave)

    def test_existing_destination_is_preserved(self):
        configuration = {'output': {'card': 'Saved', 'pcm': 'custom_pcm'}, 'output_choices': [choice()]}
        self.assertIsNone(choose_output(configuration, non_interactive=True))

    def test_missing_output_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'absente'):
            choose_output({'output_choices': [choice()]}, 'Missing:0')

    def test_all_present_radios_are_powered(self):
        client = FakeClient()
        configure(client, non_interactive=True)
        self.assertEqual(len([c for c in client.calls if c[0] == '/api/radio']), 2)
        self.assertTrue(all(a['powered'] for a in client.status['adapters']))

    def test_remaining_hardware_block_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'rfkill'):
            configure(FakeClient(blocked=True), non_interactive=True)

    def test_unready_audio_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'pas prête'):
            configure(FakeClient(ready=False), non_interactive=True)

    def test_environment_is_read_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'env'
            path.write_text('# comment\nBUREAU_KEY="example"\nBUREAU_PORT=8088\nOTHER=$(false)\n')
            self.assertEqual(read_environment(path)['OTHER'], '$(false)')
            self.assertEqual(read_environment(path)['BUREAU_KEY'], 'example')


class ConfigurationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_authenticates_configures_and_preserves_settings_on_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            state = StateFile(folder)
            backend = DemoBackend(state)
            server = TestServer(create_app(backend, 'test-secret'))
            await server.start_server()
            try:
                environment = Path(folder) / 'env'
                environment.write_text('BUREAU_KEY=test-secret\nBUREAU_HOST=127.0.0.1\nBUREAU_PORT={}\n'.format(server.port))
                async def run(*arguments):
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(ROOT / 'configure.py'), '--env', str(environment),
                        '--non-interactive', *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    return stdout.decode()
                await run('--output', 'DemoUSB:0')
                self.assertEqual(backend.output['card'], 'DemoUSB')
                self.assertEqual(backend.output['control'], 'Bureau Audio Master')
                backend.master['volume'] = 42
                await run()
                self.assertEqual(backend.output['card'], 'DemoUSB')
                self.assertEqual(backend.master['volume'], 42)
            finally:
                await server.close()


class BlueAlsaPreparationTests(unittest.TestCase):
    def test_wrong_source_checksum_stops_before_compilation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            commands = root / 'bin'
            commands.mkdir()
            script = root / 'prepare.sh'
            script.write_text((ROOT / 'prepare-bluealsa.sh').read_text().replace('/opt/', str(root) + '/opt/'))
            for name, body in {
                'bluealsa': 'exit 0', 'bluealsa-aplay': 'echo unsupported-player', 'apt-get': 'exit 0',
                'curl': 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then echo invalid > "$2"; exit 0; fi; shift; done',
                'autoreconf': 'touch "$COMPILED"', 'make': 'touch "$COMPILED"',
            }.items():
                path = commands / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            environment = {**os.environ, 'PATH': str(commands) + ':' + os.environ['PATH'], 'COMPILED': str(root / 'compiled')}
            result = subprocess.run(['bash', str(script)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'compiled').exists())
            self.assertIn('Échec de compilation', result.stderr)

    def test_compatible_existing_player_needs_no_download_or_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, body in {'bluealsa': 'exit 0', 'bluealsa-aplay': 'echo --volume=software',
                               'apt-get': 'touch "$UNEXPECTED"; exit 1', 'curl': 'touch "$UNEXPECTED"; exit 1'}.items():
                path = root / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            environment = {**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'], 'UNEXPECTED': str(root / 'unexpected')}
            result = subprocess.run(['bash', str(ROOT / 'prepare-bluealsa.sh')], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'unexpected').exists())


if __name__ == '__main__':
    unittest.main()

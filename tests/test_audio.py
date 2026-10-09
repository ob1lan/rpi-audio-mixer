"""Owned playback routing, mixer capability and rollback contracts without hardware."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import (AudioEngine, CommandResult, SOFTWARE_CONTROL, alsa_config,
                   checked_settings, fallback_controls, hardware_diagnostics, parse_outputs)

DEVICES = '''**** List of PLAYBACK Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
card 1: USB [USB Audio DAC], device 0: USB Audio [USB Audio]
card 2: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 2: vc4hdmi0 [vc4-hdmi-0], device 1: MAI PCM i2s-hifi-1 [MAI PCM i2s-hifi-1]
'''
MIXER = """Simple mixer control 'PCM',0
  Capabilities: pvolume pvolume-joined pswitch pswitch-joined
  Mono: Playback 120 [97%] [1.20dB] [on]
"""


def controls(card):
    if card != 'Headphones':
        return []
    return [{'name': 'PCM', 'gain_max': 4, 'has_db': True, 'has_switch': True,
             'db_min': -102.39, 'software': False}]


class FakeRunner:
    def __init__(self):
        self.calls, self.active, self.probe_failure = [], False, False
        self.devices = DEVICES
        self.mixer = MIXER

    async def __call__(self, arguments, input=None, env=None):
        self.calls.append((list(arguments), input, env))
        if arguments[:2] == ['/usr/bin/aplay', '-l']:
            return CommandResult(0, self.devices)
        if arguments[:2] == ['/usr/bin/systemctl', 'is-active']:
            return CommandResult(0 if self.active else 3, 'active\n' if self.active else 'inactive\n')
        if arguments[0] == '/usr/bin/amixer' and 'sget' in arguments:
            return CommandResult(0, MIXER)
        if arguments[0] == '/usr/bin/amixer' and 'scontents' in arguments:
            return CommandResult(0, self.mixer)
        if arguments[0] == '/usr/bin/aplay' and self.probe_failure:
            return CommandResult(1, stderr='device cannot play stereo')
        return CommandResult(0)


class FakeProcess:
    def __init__(self, exit_code=None):
        self.returncode, self.stderr = exit_code, None
        self.terminated, self.killed = False, False

    def terminate(self):
        self.terminated, self.returncode = True, 0

    def kill(self):
        self.killed, self.returncode = True, -9

    async def wait(self):
        return self.returncode


class FakeSpawn:
    def __init__(self):
        self.calls, self.processes, self.fail_next = [], [], False

    async def __call__(self, *arguments, **keywords):
        self.calls.append((arguments, keywords))
        process = FakeProcess(1 if self.fail_next else None)
        self.fail_next = False
        self.processes.append(process)
        return process


class ConfigTests(unittest.TestCase):
    def test_discovery_keeps_card_identity_and_multiple_devices(self):
        values = parse_outputs(DEVICES)
        self.assertEqual(len(values), 4)
        self.assertEqual(values[1]['pcm'], 'plughw:CARD=USB,DEV=0')
        self.assertEqual(values[3]['device'], 1)
        self.assertEqual(values[0]['card'], 'Headphones')

    def test_fallback_does_not_invent_positive_gain_or_capture_control(self):
        values = fallback_controls(MIXER + "Simple mixer control 'Capture',0\n  Capabilities: cvolume cswitch\n")
        self.assertEqual(len(values), 1)
        self.assertTrue(values[0]['has_db'])
        self.assertTrue(values[0]['has_switch'])
        self.assertEqual(values[0]['gain_max'], 0)

    def test_hardware_warning_ignores_capture_and_software_controls(self):
        text = """Simple mixer control 'Capture',0
  Capabilities: cvolume cswitch
  Mono: Capture 0 [0%] [off]
Simple mixer control 'Mixed',0
  Capabilities: pvolume cvolume
  Mono: Playback 50 [50%] [on]
  Mono: Capture 0 [0%] [off]
Simple mixer control 'Bureau Audio Master',0
  Capabilities: pvolume
  Mono: Playback 0 [0%]
"""
        self.assertEqual(hardware_diagnostics(text), [])
        self.assertIn('PCM', hardware_diagnostics(MIXER.replace('[on]', '[off]'))[0])
        self.assertIn('PCM', hardware_diagnostics(MIXER.replace('[97%]', '[0%]'))[0])

    def test_managed_chain_is_stereo_shared_and_per_user(self):
        text = alsa_config({'card': 'USB', 'device': 0, 'mode': 'managed', 'control': SOFTWARE_CONTROL})
        for token in ('</usr/share/alsa/alsa.conf>', 'type dmix', 'type softvol', 'channels 2',
                      'count 1', 'ipc_key_add_uid true', 'rate 48000', 'period_time 40000', 'periods 5'):
            self.assertIn(token, text)
        self.assertIn('hw:CARD=USB,DEV=0', text)
        self.assertIn('max_dB 4.0', text)

    def test_config_interpolation_rejects_injection(self):
        for card in ['USB"; type null; #', '../USB', 'USB\n', 'USB space']:
            with self.assertRaises(ValueError):
                alsa_config({'card': card, 'mode': 'managed', 'control': SOFTWARE_CONTROL})
        with self.assertRaises(ValueError):
            alsa_config({'card': 'USB', 'device': True, 'mode': 'managed', 'control': SOFTWARE_CONTROL})

    def test_existing_pcm_does_not_rewrite_native_mixing(self):
        self.assertEqual(alsa_config({'card': 'Headphones', 'device': 0, 'mode': 'existing', 'control': 'PCM'}),
                         '</usr/share/alsa/alsa.conf>\n')

    def test_gain_and_mute_inputs_are_strict(self):
        with self.assertRaises(ValueError): checked_settings({'gain': 0.1}, 0)
        for value in [True, float('nan'), float('inf'), '50', -1, 101]:
            with self.assertRaises(ValueError): checked_settings({'volume': value}, 4)
        with self.assertRaises(ValueError): checked_settings({'muted': 1}, 4)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runner, self.spawn = FakeRunner(), FakeSpawn()
        self.engine = AudioEngine(SimpleNamespace(path=Path(self.temporary.name) / 'settings.json'),
                                  runner=self.runner, spawn=self.spawn, controls_provider=controls)
        self.usb = {'card': 'USB', 'control': SOFTWARE_CONTROL, 'pcm': 'plughw:CARD=USB,DEV=0',
                    'label': 'USB DAC', 'mode': 'managed'}
        self.native = {'card': 'Headphones', 'control': 'PCM', 'pcm': 'hub_mix', 'mode': 'existing'}

    async def asyncTearDown(self):
        await self.engine.close()
        self.temporary.cleanup()

    async def test_discover_has_real_limits_and_software_control_for_hdmi(self):
        choices = await self.engine.discover()
        physical = next(item for item in choices[0]['controls'] if item['name'] == 'PCM')
        self.assertEqual(physical['gain_max'], 4)
        self.assertEqual(choices[2]['controls'][0]['name'], SOFTWARE_CONTROL)
        self.assertTrue(choices[2]['controls'][0]['software'])

    async def test_software_master_reports_muted_hardware_without_changing_it(self):
        self.runner.mixer = MIXER.replace('[on]', '[off]')
        await self.engine.configure(self.usb)
        self.assertIn('PCM', self.engine.output_choices[1]['diagnostics'][0])
        self.assertIn('PCM', self.engine.diagnostics(self.engine.output)[0])
        self.assertTrue(all(call[0][5] == SOFTWARE_CONTROL for call in self.runner.calls if 'sset' in call[0]))
        self.assertEqual(self.engine.diagnostics(self.native), [])

    async def test_hardware_warning_clears_after_mixer_read_recovers(self):
        self.runner.mixer = MIXER.replace('[97%]', '[0%]')
        await self.engine.configure(self.usb)
        self.assertTrue(self.engine.diagnostics(self.engine.output))
        self.runner.mixer = MIXER
        await self.engine.discover()
        self.assertEqual(self.engine.diagnostics(self.engine.output), [])

    async def test_managed_configuration_routes_actual_child_and_probes(self):
        result = await self.engine.configure(self.usb, {'volume': 50, 'gain': 2, 'muted': False})
        self.assertEqual(result['gain_max'], 4)
        arguments, keywords = self.spawn.calls[-1]
        self.assertEqual(arguments[1:3], ('-D', 'bureau_audio'))
        self.assertIn('--volume=software', arguments)
        self.assertNotIn('--single-audio', arguments)
        self.assertEqual(keywords['env']['ALSA_CONFIG_PATH'], str(self.engine.config_path))
        probe = [call for call in self.runner.calls if call[0][0] == '/usr/bin/aplay' and '-q' in call[0]][0]
        self.assertEqual(len(probe[1]), 19200)
        master = [call[0] for call in self.runner.calls if 'sset' in call[0]][-1]
        self.assertEqual(master[-2:], [SOFTWARE_CONTROL, '-4.02dB'])
        self.assertEqual(await self.engine.get_master(), {'volume': 50, 'gain': 2, 'muted': False})
        self.assertTrue((await self.engine.poll())['applied'])

    async def test_native_configuration_preserves_current_gain_and_route(self):
        await self.engine.configure(self.native)
        self.assertEqual(self.spawn.calls[-1][0][1:3], ('-D', 'hub_mix'))
        self.assertEqual(await self.engine.get_master(), {'volume': 100, 'gain': 1.2, 'muted': False})
        self.assertEqual([c[0] for c in self.runner.calls if 'sset' in c[0]][-1][-3:], ['PCM', '1.20dB', 'unmute'])
        self.assertNotIn('dmix', self.engine.config_path.read_text())

    async def test_mute_without_switch_is_zero_and_unmute_restores_level(self):
        await self.engine.configure(self.usb, {'volume': 75, 'gain': 1, 'muted': False})
        await self.engine.set_master({'muted': True})
        self.assertEqual(self.runner.calls[-1][0][-1], '0')
        self.assertEqual((await self.engine.get_master())['volume'], 75)
        await self.engine.set_master({'muted': False})
        self.assertEqual(self.runner.calls[-1][0][-1], '-1.50dB')
        await self.engine.set_master({'volume': 0})
        self.assertEqual(self.runner.calls[-1][0][-1], '0')

    async def test_existing_external_service_conflict_has_no_side_effect(self):
        self.runner.active = True
        with self.assertRaisesRegex(RuntimeError, 'possède déjà'):
            await self.engine.configure(self.usb)
        self.assertFalse(self.engine.config_path.exists())
        self.assertEqual(self.spawn.calls, [])

    async def test_invalid_route_does_not_stop_existing_player(self):
        await self.engine.configure(self.usb)
        previous = self.engine.process
        with self.assertRaises(ValueError):
            await self.engine.configure({**self.usb, 'pcm': 'plughw:CARD=OTHER,DEV=0'})
        self.assertFalse(previous.terminated)
        self.assertIs(self.engine.process, previous)

    async def test_probe_failure_restores_previous_route_and_configuration(self):
        await self.engine.configure(self.native)
        previous, config = self.engine.process, self.engine.config_path.read_text()
        self.runner.probe_failure = True
        with self.assertRaisesRegex(RuntimeError, 'stereo'):
            await self.engine.configure(self.usb)
        self.assertIsNot(self.engine.process, previous)
        self.assertTrue(previous.terminated)
        self.assertEqual(self.spawn.calls[-1][0][1:3], ('-D', 'hub_mix'))
        self.assertEqual(self.engine.config_path.read_text(), config)
        self.assertTrue(self.engine.applied)

    async def test_start_failure_restores_previous_route_and_level(self):
        await self.engine.configure(self.native)
        previous, config = self.engine.process, self.engine.config_path.read_text()
        self.spawn.fail_next = True
        with self.assertRaisesRegex(RuntimeError, 'arrêté'):
            await self.engine.configure(self.usb)
        self.assertTrue(previous.terminated)
        self.assertEqual(self.engine.config_path.read_text(), config)
        self.assertEqual(self.spawn.calls[-1][0][1:3], ('-D', 'hub_mix'))
        self.assertEqual(self.engine.output['pcm'], 'hub_mix')
        self.assertTrue(self.engine.applied)

    async def test_unexpected_exit_is_reported_and_blocks_master(self):
        await self.engine.configure(self.usb)
        self.engine.process.returncode = 23
        snapshot = await self.engine.poll()
        self.assertFalse(snapshot['applied'])
        self.assertFalse(snapshot['running'])
        self.assertIn('23', snapshot['message'])
        with self.assertRaises(RuntimeError):
            await self.engine.set_master({'volume': 10})

    async def test_unsupported_gain_rejected_before_hardware_write(self):
        await self.engine.configure(self.usb)
        calls = len(self.runner.calls)
        with self.assertRaises(ValueError):
            await self.engine.set_master({'gain': 4.1})
        self.assertEqual(len(self.runner.calls), calls)

    async def test_fallback_controls_disable_unknown_hardware_boost(self):
        self.engine.controls_provider = lambda card: None
        choices = await self.engine.discover()
        physical = next(item for item in choices[0]['controls'] if item['name'] == 'PCM')
        self.assertEqual(physical['gain_max'], 0)

    async def test_label_change_does_not_restart_audio(self):
        await self.engine.configure(self.usb)
        process, calls = self.engine.process, len(self.spawn.calls)
        await self.engine.configure({**self.usb, 'label': 'Nouveau nom'})
        self.assertIs(self.engine.process, process)
        self.assertEqual(len(self.spawn.calls), calls)
        self.assertEqual(self.engine.output['label'], 'Nouveau nom')

    async def test_physical_control_without_real_mute_is_not_offered(self):
        self.engine.controls_provider = lambda card: [
            {'name': 'Attenuator', 'gain_max': 0, 'has_db': True, 'has_switch': False,
             'db_min': -30, 'software': False}]
        choices = await self.engine.discover()
        self.assertTrue(all([item['name'] for item in choice['controls']] == [SOFTWARE_CONTROL] for choice in choices))

    async def test_unplugged_card_is_unavailable_even_with_live_player(self):
        await self.engine.configure(self.usb)
        self.runner.devices = DEVICES.replace('card 1: USB [USB Audio DAC], device 0: USB Audio [USB Audio]\n', '')
        await self.engine.discover()
        status = self.engine.snapshot()
        self.assertTrue(status['running'])
        self.assertFalse(status['applied'])
        self.assertIn('déconnectée', status['message'])

    async def test_initial_start_failure_leaves_no_fake_applied_output(self):
        self.spawn.fail_next = True
        with self.assertRaises(RuntimeError):
            await self.engine.configure(self.usb)
        self.assertIsNone(self.engine.process)
        self.assertFalse(self.engine.snapshot()['applied'])
        self.assertFalse(self.engine.config_path.exists())

    async def test_close_terminates_and_reaps_owned_player(self):
        await self.engine.configure(self.usb)
        process = self.engine.process
        await self.engine.close()
        self.assertTrue(process.terminated)
        self.assertIsNone(self.engine.process)
        self.assertFalse(self.engine.snapshot()['applied'])


if __name__ == '__main__':
    unittest.main()

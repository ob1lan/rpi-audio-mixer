"""Contrats de routage, audio et accès web, avec deux contrôleurs simulés."""
import asyncio
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from dbus_next import DBusError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (ADAPTER, DEVICE, OBJECTS, PCM, PROPERTIES, A2DP_SOURCE,
                 DemoBackend, PairingAgent, PiBackend, StateFile, create_app,
                 decode_volume, encode_volume, master_db, number, parse_mixer,
                 routing_config, VERSION, demo_config)

CONFIG = demo_config()
INTERNAL, USB = [v['address'] for v in CONFIG['adapters']]
PHONE, WORK, PERSONAL = [v['address'] for v in CONFIG['sources']]

def device_path(controller, address):
    return '/org/bluez/' + controller + '/dev_' + address.replace(':', '_')

A, B, C = [device_path('hci0', a) for a in (PHONE, WORK, PERSONAL)]
D = device_path('hci1', PERSONAL)
P, Q, R, S = ['/org/bluealsa' + v[len('/org/bluez'):] + '/a2dpsnk/source' for v in (A, B, C, D)]

class AudioMathTests(unittest.TestCase):
    def test_stereo_volume_and_mute_bits(self):
        self.assertEqual(decode_volume(0x7F7F), (100, False))
        self.assertEqual(decode_volume(0xFFFF), (100, True))
        self.assertEqual(encode_volume(0x8019, muted=False), 0x0019)
        self.assertEqual(encode_volume(0x8019, muted=True), 0x8099)
        self.assertEqual(encode_volume(0x8019, volume=50), 0xC040)
        self.assertEqual(encode_volume(0xFFFF, volume=0), 0x8080)

    def test_master_gain_is_additive(self):
        self.assertEqual(master_db(100, 4), 4)
        self.assertAlmostEqual(master_db(50, 0), -6.020599913)
        self.assertAlmostEqual(master_db(50, 2), -4.020599913)
        self.assertEqual(master_db(0, 0), -102.39)

    def test_invalid_input_never_reaches_hardware(self):
        for value in [True, '100', float('nan'), float('inf'), -1, 101, None]:
            with self.assertRaises(ValueError):
                number(value, 0, 100)
        with self.assertRaises(ValueError):
            master_db(100, 4.1)
        with self.assertRaises(ValueError):
            decode_volume([127, 256])

    def test_new_byte_array_api_preserves_channel_levels_and_mutes(self):
        self.assertEqual(decode_volume(bytes([127, 127])), (100, False))
        self.assertEqual(decode_volume(bytes([255, 255])), (100, True))
        self.assertEqual(encode_volume(bytes([0x80, 0x19]), muted=False), bytes([0, 0x19]))
        self.assertEqual(encode_volume(bytes([0x80, 0x19]), volume=50), bytes([0xC0, 0x40]))
        for value in (b'', True, [True, 127], [255, -1], 65536):
            with self.assertRaises(ValueError): decode_volume(value)

    def test_initial_volume_preserves_existing_positive_gain(self):
        data = parse_mixer("Mono: Playback 120 [97%] [1.20dB] [on]")
        self.assertEqual(data, {'volume': 100, 'gain': 1.2, 'muted': False})
        data = parse_mixer("Mono: Playback -2000 [80%] [-20.00dB] [off]")
        self.assertEqual(data, {'volume': 10, 'gain': 0, 'muted': True})


class RoutingConfigTests(unittest.TestCase):
    def test_fresh_configuration_has_no_hardware_or_source_assumptions(self):
        self.assertEqual(routing_config({'adapters': [], 'sources': []}), {'adapters': [], 'sources': []})
    def test_reject_unknown_receiver_and_missing_alias(self):
        config = copy.deepcopy(CONFIG)
        config['sources'][0]['adapter'] = '00:00:00:00:00:00'
        with self.assertRaises(ValueError): routing_config(config)
        config = copy.deepcopy(CONFIG)
        del config['adapters'][0]['alias']
        with self.assertRaises(ValueError): routing_config(config)

    def test_case_normalization_and_duplicate_rejection(self):
        config = copy.deepcopy(CONFIG)
        config['adapters'][0]['address'] = INTERNAL.lower()
        self.assertEqual(routing_config(config)['adapters'][0]['address'], INTERNAL)
        config['sources'].append(config['sources'][0])
        with self.assertRaises(ValueError): routing_config(config)


class FakeAudioEngine:
    def __init__(self, pi):
        self.pi, self.output, self.master = pi, None, None
        self.choices = [{'card': 'TestAudio', 'name': 'Carte simulée', 'device': 0, 'pcm': 'hub_mix', 'mode': 'existing',
                         'controls': [{'name': 'PCM', 'gain_max': 4, 'has_switch': True, 'has_db': True}]}]
    async def discover(self): return copy.deepcopy(self.choices)
    async def configure_inputs(self, inputs): self.inputs = dict(inputs)
    def source_gain_ready(self, address): return self.output is not None and self.output.get('mode') == 'managed' and address in self.inputs
    async def set_source_gain(self, address, gain):
        if not self.source_gain_ready(address): raise RuntimeError('Gain indisponible.')
        self.inputs[address] = gain
    async def configure(self, output, current_master=None):
        choice = next(v for v in self.choices if v['card'] == output['card'] and v['pcm'] == output['pcm'])
        limit = next(v['gain_max'] for v in choice['controls'] if v['name'] == output['control'])
        self.output = {**output, 'gain_max': limit}
        if current_master is None:
            self.master = parse_mixer(await self.pi.amixer('sget', 'PCM'))
        else:
            await self.set_master(current_master)
        return self.output
    async def set_master(self, current):
        await self.pi.amixer('sset', 'PCM', f"{master_db(current['volume'], current['gain']):.2f}dB", 'mute' if current['muted'] or current['volume'] == 0 else 'unmute')
        self.master = dict(current)
    async def get_master(self): return self.master
    async def poll(self): pass
    async def close(self): self.output = None
    def diagnostics(self, output): return []
    def snapshot(self): return {'applied': self.output is not None, 'message': 'Lecteur simulé'}
    def equalizer_snapshot(self):
        return self.pi.state.data.get('equalizer', {})


class FakePi(PiBackend):
    def __init__(self, state):
        super().__init__(state, CONFIG)
        self.calls, self.mixer_calls = [], []
        self.audio_engine = FakeAudioEngine(self)
        self.output = state.data.get('output') or {'card': 'TestAudio', 'control': 'PCM', 'pcm': 'hub_mix', 'mode': 'existing', 'label': 'Sortie test'}
        self.connect_gate = None
        self.blue_objects = {
            '/org/bluez/hci0': {ADAPTER: {'Address': INTERNAL, 'Powered': True, 'Alias': CONFIG['adapters'][0]['alias'], 'Pairable': False, 'Discoverable': False}},
            '/org/bluez/hci1': {ADAPTER: {'Address': USB, 'Powered': True, 'Alias': CONFIG['adapters'][1]['alias'], 'Pairable': False, 'Discoverable': False}},
        }
        for path, address in ((A, PHONE), (B, WORK), (D, PERSONAL)):
            self.blue_objects[path] = {DEVICE: {'Address': address, 'Alias': address, 'Paired': True, 'UUIDs': [A2DP_SOURCE], 'Trusted': True, 'Blocked': False, 'Connected': True}}
        self.audio_objects = {}
        for path, pcm, volume in ((A, P, 0x7F7F), (B, Q, 0x4040), (D, S, 0x3030)):
            self.audio_objects[pcm] = {PCM: {'Device': path, 'Transport': 'A2DP-sink', 'Mode': 'source', 'Volume': volume, 'Running': True, 'Channels': 2, 'Sampling': 44100, 'Codec': 'SBC'}}
        self.audio_objects['/sco'] = {PCM: {'Transport': 'HFP-AG', 'Mode': 'source'}}

    async def call(self, destination, path, interface, member, signature='', body=None, timeout=8):
        self.calls.append((destination, path, interface, member, signature, body))
        if member == 'GetManagedObjects':
            return [copy.deepcopy(self.blue_objects if destination == 'org.bluez' else self.audio_objects)]
        if member == 'Set':
            objects = self.blue_objects if destination == 'org.bluez' else self.audio_objects
            objects[path][body[0]][body[1]] = body[2].value
        if member == 'Disconnect': self.blue_objects[path][DEVICE]['Connected'] = False
        if member == 'ConnectProfile':
            if self.connect_gate: await self.connect_gate.wait()
            self.blue_objects[path][DEVICE]['Connected'] = True
        return []

    async def amixer(self, *arguments):
        self.mixer_calls.append(arguments)
        return 'Mono: Playback 120 [97%] [1.20dB] [on]'

    def swap_controllers(self):
        def swap(path):
            return path.replace('hci0', 'temporary').replace('hci1', 'hci0').replace('temporary', 'hci1')
        self.blue_objects = {swap(p): v for p, v in self.blue_objects.items()}
        self.audio_objects = {swap(p): v for p, v in self.audio_objects.items()}
        for v in self.audio_objects.values():
            if 'Device' in v[PCM]: v[PCM]['Device'] = swap(v[PCM]['Device'])


class PiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pi = FakePi(StateFile(self.tmp.name))
        await self.pi.refresh()
        self.pi.calls.clear()
        self.pi.mixer_calls.clear()

    async def asyncTearDown(self): self.tmp.cleanup()

    async def test_discovery_from_empty_config_and_controller_replacement(self):
        self.pi.state.data = {}
        self.pi.init_registry(self.pi.state, routing_config({}))
        self.pi.output = None
        self.pi.master_ready = False
        self.pi.audio_engine.output = None
        await self.pi.refresh()
        self.assertEqual(set(self.pi.specs), {INTERNAL, USB})
        self.assertEqual(self.pi.routes, {PHONE: INTERNAL, WORK: INTERNAL, PERSONAL: USB})
        self.assertFalse(self.pi.master_ready)
        third = '02:BA:00:00:00:03'
        self.pi.blue_objects['/org/bluez/hci9'] = {ADAPTER: {'Address': third, 'Powered': True, 'Alias': 'Nouvelle radio'}}
        del self.pi.blue_objects['/org/bluez/hci0']
        await self.pi.refresh()
        self.assertIn(third, self.pi.specs)
        self.assertEqual(self.pi.routes[PHONE], INTERNAL)
        saved = StateFile(self.tmp.name)
        restored = PiBackend(saved, routing_config({}))
        self.assertEqual(restored.specs[third]['alias'], 'Nouvelle radio')
        self.assertEqual(restored.routes[PHONE], INTERNAL)

    async def test_non_audio_peripherals_are_not_adopted_as_sources(self):
        keyboard, headset = '02:BA:20:00:00:01', '02:BA:20:00:00:02'
        for address, uuids in ((keyboard, []), (headset, ['0000110b-0000-1000-8000-00805f9b34fb'])):
            self.pi.blue_objects[device_path('hci0', address)] = {DEVICE: {'Address': address, 'Paired': True, 'Connected': True, 'UUIDs': uuids}}
        await self.pi.refresh()
        self.assertNotIn(keyboard, self.pi.sources)
        self.assertNotIn(headset, self.pi.sources)

    async def test_byte_array_volume_and_rate_api_reaches_dbus(self):
        self.pi.audio_objects[P][PCM]['Volume'] = bytes([0x7F, 0x7F])
        del self.pi.audio_objects[P][PCM]['Sampling']
        self.pi.audio_objects[P][PCM]['Rate'] = 48000
        await self.pi.refresh()
        self.pi.calls.clear()
        await self.pi.set_device(PHONE, {'volume': 50, 'muted': True})
        variant = self.pi.calls[-1][-1][2]
        self.assertEqual(variant.signature, 'ay')
        self.assertEqual(variant.value, bytes([0xC0, 0xC0]))
        phone = next(v for v in self.pi.snapshot()['devices'] if v['address'] == PHONE)
        self.assertEqual(phone['sampling'], 48000)

    async def test_output_configuration_changes_engine_route_and_limits_gain(self):
        self.pi.audio_engine.choices.append({'card': 'USBAudio', 'name': 'Carte USB', 'pcm': 'plughw:CARD=USBAudio,DEV=0', 'device': 0, 'mode': 'managed',
                                             'controls': [{'name': 'PCM', 'gain_max': 0, 'has_switch': True, 'has_db': True}]})
        await self.pi.configuration_action({'output': {'card': 'USBAudio', 'pcm': 'plughw:CARD=USBAudio,DEV=0', 'control': 'PCM', 'label': 'Mon DAC'}})
        self.assertEqual(self.pi.audio_engine.output['card'], 'USBAudio')
        self.assertEqual(self.pi.output['label'], 'Mon DAC')
        self.assertEqual(self.pi.gain_max, 0)
        self.assertEqual(StateFile(self.tmp.name).data['output']['card'], 'USBAudio')
        with self.assertRaises(ValueError): await self.pi.set_master({'gain': 1})

    async def test_receiver_alias_saved_and_applied_to_bluez(self):
        await self.pi.configuration_action({'adapter': {'address': USB, 'alias': 'Salon USB'}})
        await self.pi.refresh()
        self.assertEqual(self.pi.adapters[USB]['Alias'], 'Salon USB')
        self.assertEqual(StateFile(self.tmp.name).data['adapter_registry'][USB]['alias'], 'Salon USB')

    async def test_legacy_environment_selects_existing_pcm_without_card_default(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'BUREAU_CARD': 'LegacyAudio', 'BUREAU_PCM': 'custom_mix'}, clear=True):
            migrated = PiBackend(StateFile(tmp), routing_config({}))
            self.assertEqual(migrated.output['mode'], 'existing')
            self.assertEqual(migrated.output['pcm'], 'custom_mix')
            self.assertEqual(migrated.output['card'], 'LegacyAudio')
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(PiBackend(StateFile(tmp), routing_config({})).output)

    async def test_device_volume_isolated_and_uses_uint16(self):
        await self.pi.set_device(PHONE, {'volume': 25})
        self.assertEqual(self.pi.pcms[P]['Volume'], 0x2020)
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0x4040)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)
        self.assertEqual(len(self.pi.calls), 2)
        self.assertEqual(self.pi.calls[-1][1], P)
        self.assertEqual(self.pi.calls[-1][-1][2].signature, 'q')
        self.assertTrue(self.pi.pcms[P]['SoftVolume'])
        self.assertEqual(self.pi.mixer_calls, [])

    async def test_device_mute_retains_its_volume(self):
        await self.pi.set_device(WORK, {'muted': True})
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0xC0C0)
        await self.pi.set_device(WORK, {'muted': False})
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0x4040)

    async def test_general_volume_independent_and_persistent(self):
        await self.pi.set_master({'volume': 50, 'gain': 2})
        self.assertEqual(self.pi.calls, [])
        self.assertEqual(self.pi.mixer_calls, [('sset', 'PCM', '-4.02dB', 'unmute')])
        saved = StateFile(self.tmp.name).data['master']
        self.assertEqual(saved, {'volume': 50, 'gain': 2, 'muted': False})
        replacement = FakePi(StateFile(self.tmp.name))
        await replacement.refresh()
        self.assertEqual(replacement.master, saved)
        self.assertIn(('sset', 'PCM', '-4.02dB', 'unmute'), replacement.mixer_calls)

    async def test_zero_volume_mutes_jack(self):
        await self.pi.set_master({'volume': 0})
        self.assertEqual(self.pi.mixer_calls[-1][-1], 'mute')

    async def test_routes_survive_swapped_hci_indices(self):
        self.pi.swap_controllers()
        await self.pi.refresh()
        self.assertEqual(self.pi.device_path(PERSONAL), device_path('hci0', PERSONAL))
        self.assertEqual(self.pi.device_path(PHONE), device_path('hci1', PHONE))
        await self.pi.device_action(PERSONAL, 'connect')
        self.assertTrue(any(c[1] == device_path('hci0', PERSONAL) and c[3] == 'ConnectProfile' and c[-1] == [A2DP_SOURCE] for c in self.pi.calls))

    async def test_obsolete_pairing_blocked_but_correct_one_trusted(self):
        self.pi.blue_objects[C] = {DEVICE: {'Address': PERSONAL, 'Paired': True, 'Trusted': True, 'Blocked': False, 'Connected': True}}
        self.pi.blue_objects[D][DEVICE].update(Trusted=False, Blocked=True)
        await self.pi.refresh()
        self.assertTrue(self.pi.blue_objects[C][DEVICE]['Blocked'])
        self.assertFalse(self.pi.blue_objects[C][DEVICE]['Connected'])
        self.assertFalse(self.pi.blue_objects[D][DEVICE]['Blocked'])
        self.assertTrue(self.pi.blue_objects[D][DEVICE]['Trusted'])
        self.assertTrue(self.pi.blue_objects[C][DEVICE]['Paired'])
        records = StateFile(self.tmp.name).data['routing_originals']
        self.assertEqual(records[INTERNAL + '/' + PERSONAL]['original']['Blocked'], False)

    async def test_volume_targets_only_assigned_receiver(self):
        self.pi.audio_objects[R] = {PCM: {**self.pi.audio_objects[S][PCM], 'Device': C, 'Volume': 0x7F7F}}
        await self.pi.refresh()
        await self.pi.set_device(PERSONAL, {'volume': 50})
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x4040)
        self.assertEqual(self.pi.pcms[R]['Volume'], 0x7F7F)

    async def test_missing_usb_never_falls_back_to_internal(self):
        del self.pi.blue_objects['/org/bluez/hci1']
        del self.pi.blue_objects[D]
        self.pi.blue_objects[C] = {DEVICE: {'Address': PERSONAL, 'Paired': True, 'Connected': True}}
        await self.pi.refresh()
        with self.assertRaises(RuntimeError): await self.pi.device_action(PERSONAL, 'connect')
        source = next(v for v in self.pi.snapshot()['devices'] if v['address'] == PERSONAL)
        self.assertFalse(source['receiver_present'])
        self.assertFalse(source['connected'])
        self.assertTrue(self.pi.blue_objects[C][DEVICE]['Blocked'])

    async def test_long_connect_does_not_lock_master(self):
        self.pi.connect_gate = asyncio.Event()
        task = asyncio.create_task(self.pi.device_action(PERSONAL, 'connect'))
        await asyncio.sleep(0)
        try:
            self.assertIn(PERSONAL, self.pi.connecting)
            with self.assertRaises(RuntimeError): await self.pi.device_action(PERSONAL, 'connect')
            await asyncio.wait_for(self.pi.set_master({'volume': 20}), 0.5)
            self.assertEqual(self.pi.master['volume'], 20)
            self.assertFalse(task.done())
        finally:
            self.pi.connect_gate.set()
            await task
        self.assertNotIn(PERSONAL, self.pi.connecting)

    async def test_pairing_windows_independent_and_expire(self):
        await self.pi.pair(True, INTERNAL)
        await self.pi.pair(True, USB)
        self.pi.authorized.add(device_path('hci0', 'AA:BB:CC:DD:EE:01'))
        self.pi.deadlines[INTERNAL] = time.monotonic() - 1
        await self.pi.refresh()
        self.assertFalse(self.pi.pairing_active_for(INTERNAL))
        self.assertTrue(self.pi.pairing_active_for(USB))
        self.assertFalse(self.pi.blue_objects['/org/bluez/hci0'][ADAPTER]['Discoverable'])
        self.assertTrue(self.pi.blue_objects['/org/bluez/hci1'][ADAPTER]['Discoverable'])
        self.assertEqual(self.pi.authorized, set())
        self.assertEqual(self.pi.blue_objects['/org/bluez/hci1'][ADAPTER]['PairableTimeout'], 180)

    async def test_new_source_remembers_receiver_across_restart(self):
        await self.pi.pair(True, USB)
        address = 'AA:BB:CC:DD:EE:03'
        new = device_path('hci1', address)
        self.pi.blue_objects[new] = {DEVICE: {'Paired': True, 'Trusted': False, 'Address': address, 'UUIDs': [A2DP_SOURCE]}}
        await self.pi.refresh()
        self.assertTrue(self.pi.devices[new]['Trusted'])
        replacement = FakePi(StateFile(self.tmp.name))
        self.assertEqual(replacement.routes[address], USB)

    async def test_agent_rejects_other_receiver_and_closed_window(self):
        agent = PairingAgent(self.pi)
        unknown = device_path('hci0', 'AA:BB:CC:DD:EE:03')
        agent.authorize(A)
        with self.assertRaises(DBusError): agent.authorize(unknown)
        await self.pi.pair(True, USB)
        with self.assertRaises(DBusError): agent.authorize(unknown)
        with self.assertRaises(DBusError): agent.authorize(C)
        await self.pi.pair(True, INTERNAL)
        agent.authorize(unknown)
        await self.pi.pair(False, INTERNAL)
        with self.assertRaises(DBusError): agent.authorize(unknown)

    async def test_power_and_alias_reapplied_after_boot(self):
        self.pi.blue_objects['/org/bluez/hci1'][ADAPTER].update(Powered=False, Alias='Récepteur par défaut')
        await self.pi.refresh()
        props = self.pi.blue_objects['/org/bluez/hci1'][ADAPTER]
        self.assertTrue(props['Powered'])
        self.assertEqual(props['Alias'], CONFIG['adapters'][1]['alias'])

    async def test_blocked_radio_reported_without_claiming_power(self):
        self.pi.blue_objects['/org/bluez/hci1'][ADAPTER].update(Powered=False, PowerState='off-blocked')
        await self.pi.refresh()
        self.assertFalse(self.pi.adapters[USB]['Powered'])
        self.assertTrue(any('Activer' in v for v in self.pi.errors))
        with self.assertRaises(RuntimeError): await self.pi.pair(True, USB)

    async def test_restore_originals_after_controller_renumbering(self):
        self.pi.blue_objects[C] = {DEVICE: {'Address': PERSONAL, 'Paired': True, 'Trusted': True, 'Blocked': False}}
        await self.pi.refresh()
        self.pi.swap_controllers()
        await self.pi.restore_routing()
        self.assertFalse(self.pi.blue_objects[device_path('hci1', PERSONAL)][DEVICE]['Blocked'])
        self.assertEqual(self.pi.state.data['routing_originals'], {})

    async def test_restore_retains_records_for_absent_receiver(self):
        self.pi.blue_objects[D][DEVICE]['Trusted'] = False
        await self.pi.refresh()
        del self.pi.blue_objects['/org/bluez/hci1']
        with self.assertRaises(RuntimeError): await self.pi.restore_routing()
        self.assertIn(USB + '/' + PERSONAL, self.pi.state.data['routing_originals'])

    async def test_no_global_volume_fallback_for_unknown_source(self):
        with self.assertRaises(ValueError): await self.pi.set_device('AA:BB:CC:DD:EE:99', {'volume': 100})
        self.assertEqual(self.pi.mixer_calls, [])

    async def test_remote_name_wins_over_custom_alias_and_changes_live(self):
        self.pi.blue_objects[B][DEVICE].update(Name='MacBook de Léa', Alias='Mac travail')
        await self.pi.refresh()
        source = next(v for v in self.pi.snapshot()['devices'] if v['address'] == WORK)
        self.assertEqual(source['name'], 'MacBook de Léa')
        self.pi.blue_objects[B][DEVICE]['Name'] = '<img src=x onerror=alert(1)>'
        await self.pi.refresh()
        self.assertEqual(self.pi.sources[WORK]['bluetooth_name'], '<img src=x onerror=alert(1)>')
        del self.pi.blue_objects[B]
        await self.pi.refresh()
        replacement = FakePi(StateFile(self.tmp.name))
        self.assertEqual(replacement.source_name(WORK), '<img src=x onerror=alert(1)>')

    async def test_no_custom_seed_name_used_when_remote_name_missing(self):
        source = next(v for v in self.pi.snapshot()['devices'] if v['address'] == WORK)
        self.assertEqual(source['name'], WORK)

    async def test_migration_keeps_old_routes_and_added_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateFile(tmp)
            new = 'AA:BB:CC:DD:EE:11'
            state.data['added_sources'] = {new: {'adapter': USB, 'name': 'Ancien alias'}}
            state.save()
            migrated = FakePi(StateFile(tmp))
            self.assertEqual(migrated.routes[PERSONAL], USB)
            self.assertEqual(migrated.routes[WORK], INTERNAL)
            self.assertEqual(migrated.routes[new], USB)
            self.assertEqual(migrated.source_name(new), new)

    async def test_assign_route_persistent_and_unblocks_unpaired_target(self):
        target = device_path('hci1', PHONE)
        self.pi.blue_objects[target] = {DEVICE: {'Address': PHONE, 'Paired': False, 'Blocked': True}}
        await self.pi.refresh()
        await self.pi.source_action(PHONE, 'assign', USB)
        self.assertTrue(self.pi.blue_objects[A][DEVICE]['Blocked'])
        self.assertFalse(self.pi.blue_objects[A][DEVICE]['Connected'])
        self.assertFalse(self.pi.blue_objects[target][DEVICE]['Blocked'])
        source = next(v for v in self.pi.snapshot()['devices'] if v['address'] == PHONE)
        self.assertEqual(source['adapter'], USB)
        self.assertFalse(source['paired'])
        replacement = FakePi(StateFile(self.tmp.name))
        self.assertEqual(replacement.routes[PHONE], USB)
        replacement.swap_controllers()
        await replacement.refresh()
        self.assertEqual(replacement.desired_path(PHONE), device_path('hci0', PHONE))

    async def test_retire_prevents_readoption_and_reconnection_after_restart(self):
        self.pi.blue_objects[B][DEVICE]['Name'] = 'Mac à remplacer'
        await self.pi.refresh()
        await self.pi.source_action(WORK, 'retire')
        self.assertNotIn(WORK, self.pi.routes)
        self.assertTrue(self.pi.blue_objects[B][DEVICE]['Blocked'])
        self.assertFalse(self.pi.blue_objects[B][DEVICE]['Connected'])
        self.assertTrue(self.pi.blue_objects[B][DEVICE]['Paired'])
        replacement = FakePi(StateFile(self.tmp.name))
        await replacement.refresh()
        self.assertNotIn(WORK, replacement.routes)
        self.assertTrue(replacement.blue_objects[B][DEVICE]['Blocked'])
        await replacement.pair(True, INTERNAL)
        with self.assertRaises(DBusError): PairingAgent(replacement).authorize(B)
        retired = replacement.snapshot()['retired_devices'][0]
        self.assertEqual(retired['name'], 'Mac à remplacer')

    async def test_restore_reuses_pairing_on_selected_receiver(self):
        await self.pi.source_action(PERSONAL, 'retire')
        await self.pi.source_action(PERSONAL, 'restore', USB)
        self.assertFalse(self.pi.blue_objects[D][DEVICE]['Blocked'])
        self.assertTrue(self.pi.blue_objects[D][DEVICE]['Paired'])
        self.assertNotIn(PERSONAL, self.pi.retired)
        await self.pi.device_action(PERSONAL, 'connect')
        self.assertTrue(self.pi.blue_objects[D][DEVICE]['Connected'])

    async def test_retiring_all_devices_does_not_reseed_fixed_list(self):
        for address in list(self.pi.sources):
            await self.pi.source_action(address, 'retire')
        replacement = FakePi(StateFile(self.tmp.name))
        await replacement.refresh()
        self.assertEqual(replacement.snapshot()['devices'], [])
        self.assertEqual(len(replacement.snapshot()['retired_devices']), 3)

    async def test_more_than_three_devices_receive_independent_stereo_controls(self):
        for i in range(4):
            address = f'AA:BB:CC:DD:EE:{20 + i:02X}'
            path = device_path('hci1', address)
            pcm = '/org/bluealsa/extra' + str(i)
            self.pi.blue_objects[path] = {DEVICE: {'Address': address, 'Name': 'Source ' + str(i), 'Paired': True, 'UUIDs': [A2DP_SOURCE], 'Connected': True}}
            self.pi.audio_objects[pcm] = {PCM: {**self.pi.audio_objects[S][PCM], 'Device': path, 'Volume': 0x5050}}
        await self.pi.refresh()
        self.assertEqual(len(self.pi.snapshot()['devices']), 7)
        await self.pi.set_device('AA:BB:CC:DD:EE:14', {'volume': 25})
        self.assertEqual(self.pi.pcms['/org/bluealsa/extra0']['Volume'], 0x2020)
        self.assertEqual(self.pi.pcms['/org/bluealsa/extra1']['Volume'], 0x5050)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)

    async def test_route_change_rejected_during_pending_connection(self):
        self.pi.connect_gate = asyncio.Event()
        task = asyncio.create_task(self.pi.device_action(PERSONAL, 'connect'))
        await asyncio.sleep(0)
        try:
            with self.assertRaises(RuntimeError): await self.pi.source_action(PERSONAL, 'assign', INTERNAL)
            with self.assertRaises(RuntimeError): await self.pi.source_action(PERSONAL, 'retire')
            self.assertEqual(self.pi.routes[PERSONAL], USB)
        finally:
            self.pi.connect_gate.set()
            await task


class RadioHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_service_and_failed_result_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pi = PiBackend(StateFile(tmp), CONFIG)
            calls = []
            async def fake(destination, path, interface, member, signature='', body=None, timeout=8):
                calls.append((member, body))
                if member == 'RestartUnit': return ['/org/freedesktop/systemd1/job/1']
                if member == 'Get': raise RuntimeError('org.freedesktop.DBus.Error.UnknownObject')
                if member == 'GetUnit': return ['/org/freedesktop/systemd1/unit/radio']
                if member == 'GetAll': return [{'Result': 'exit-code', 'ExecMainStatus': 1}]
            pi.call = fake
            with self.assertRaises(RuntimeError): await pi.unblock_radio()
            self.assertEqual(calls[0], ('RestartUnit', ['bureau-audio-radio.service', 'replace']))


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = DemoBackend(StateFile(self.tmp.name))
        self.client = TestClient(TestServer(create_app(self.backend, 'test-key')), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def post(self, path, data, origin=None):
        return await self.client.post(path, json=data, headers={'Origin': origin or self.origin})

    async def login(self):
        result = await self.post('/api/login', {'key': 'test-key'})
        self.assertEqual(result.status, 200)
        self.assertIn('HttpOnly', result.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', result.headers['Set-Cookie'])

    async def test_authentication_required(self):
        self.assertEqual((await self.client.get('/api/status')).status, 401)
        self.assertEqual((await self.post('/api/login', {'key': 'wrong'})).status, 401)
        await self.login()
        response = await self.client.get('/api/status')
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data['demo'])
        self.assertEqual(data['version'], VERSION)

    async def test_foreign_origin_cannot_change_volume(self):
        await self.login()
        response = await self.post('/api/master', {'volume': 0}, origin='http://foreign.invalid')
        self.assertEqual(response.status, 403)
        self.assertEqual(self.backend.master['volume'], 78)

    async def test_invalid_gain_rejected(self):
        await self.login()
        self.assertEqual((await self.post('/api/master', {'gain': 12})).status, 400)
        self.assertEqual((await self.post('/api/master', {'volume': True})).status, 400)
        self.assertEqual(self.backend.master['gain'], 1.2)

    async def test_web_controls_keep_stereo_and_independent_sources(self):
        await self.login()
        response = await self.post('/api/device', {'address': PHONE, 'volume': 20})
        data = (await response.json())['state']
        self.assertEqual(data['devices'][0]['volume'], 20)
        self.assertEqual(data['devices'][1]['volume'], 72)
        self.assertEqual(data['devices'][0]['channels'], 2)
        await self.post('/api/master', {'volume': 30, 'gain': 2})
        self.assertEqual(self.backend.master['volume'], 30)
        self.assertEqual(self.backend.devices[0]['volume'], 20)

    async def test_pairing_requires_explicit_receiver_and_closes_only_it(self):
        await self.login()
        self.assertEqual((await self.post('/api/pairing', {'enabled': True})).status, 400)
        for adapter in (INTERNAL, USB):
            response = await self.post('/api/pairing', {'enabled': True, 'adapter': adapter})
            self.assertEqual(response.status, 200)
        await self.post('/api/pairing', {'enabled': False, 'adapter': INTERNAL})
        state = self.backend.snapshot()
        self.assertEqual(state['adapters'][0]['pairing_seconds'], 0)
        self.assertGreater(state['adapters'][1]['pairing_seconds'], 175)

    async def test_unknown_or_invalid_receiver_rejected(self):
        await self.login()
        for adapter in ('hci1', '00:00:00:00:00:00'):
            self.assertEqual((await self.post('/api/pairing', {'enabled': True, 'adapter': adapter})).status, 400)
            self.assertEqual((await self.post('/api/radio', {'adapter': adapter})).status, 400)

    async def test_assets_available_before_login(self):
        for path in ('/', '/static/app.js', '/static/style.css', '/static/icon.svg'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    async def test_health_and_configuration_authentication(self):
        health = await self.client.get('/health')
        self.assertEqual((await health.json())['version'], VERSION)
        self.assertEqual((await self.post('/api/configuration', {'output': {}})).status, 401)
        await self.login()
        for output in ({'card': 'missing', 'control': 'PCM', 'pcm': 'default'},
                       {'card': 'DemoAudio', 'control': 'missing', 'pcm': 'plughw:CARD=DemoAudio,DEV=0'},
                       {'card': 'DemoAudio', 'control': 'PCM', 'pcm': 'plughw:CARD=DemoAudio,DEV=0', 'mode': 'existing'},
                       {'card': 'DemoAudio', 'control': 'PCM', 'pcm': 'plughw:CARD=DemoAudio,DEV=0', 'command': 'anything'}):
            self.assertEqual((await self.post('/api/configuration', {'output': output})).status, 400)

    async def test_output_choice_and_gain_limit_survive_restart(self):
        await self.login()
        response = await self.post('/api/configuration', {'output': {'card': 'DemoUSB', 'control': 'Speaker', 'pcm': 'plughw:CARD=DemoUSB,DEV=0', 'label': 'Bureau'}})
        self.assertEqual(response.status, 200)
        state = (await response.json())['state']
        self.assertEqual(state['gain_max'], 0)
        self.assertEqual(state['master']['gain'], 0)
        self.assertEqual((await self.post('/api/master', {'gain': 0.1})).status, 400)
        restored = DemoBackend(StateFile(self.tmp.name))
        self.assertEqual(restored.gain_max, 0)
        self.assertEqual(restored.output['label'], 'Bureau')

    async def test_source_gain_is_independent_persistent_and_strict(self):
        await self.login()
        for invalid in [True, -1, 4.1, '2']:
            self.assertEqual((await self.post('/api/device', {'address': PHONE, 'gain': invalid})).status, 400)
        response = await self.post('/api/device', {'address': PHONE, 'gain': 2.3})
        self.assertEqual(response.status, 200)
        devices = (await response.json())['state']['devices']
        self.assertEqual(devices[0]['gain'], 2.3)
        self.assertEqual(devices[0]['volume'], 65)
        self.assertEqual(devices[1]['gain'], 0)
        self.assertEqual(DemoBackend(StateFile(self.tmp.name)).find(PHONE)['gain'], 2.3)
        await self.post('/api/device', {'address': PHONE, 'action': 'disconnect'})
        self.assertEqual((await self.post('/api/device', {'address': PHONE, 'gain': 1})).status, 200)
        self.assertEqual(self.backend.find(PHONE)['volume'], None)
        self.assertEqual((await self.post('/api/device', {'address': PHONE, 'gain': 2, 'typo': 3})).status, 400)

    async def test_level_endpoint_authentication_and_master_mute(self):
        self.assertEqual((await self.client.get('/api/levels')).status, 401)
        await self.login()
        response = await self.client.get('/api/levels')
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data['demo'])
        self.assertTrue(data['available'])
        self.assertGreater(data['left'], 0)
        self.assertGreater(data['right'], 0)
        await self.post('/api/master', {'muted': True})
        data = await (await self.client.get('/api/levels')).json()
        self.assertEqual([data['left'], data['right']], [0, 0])
        self.assertFalse(data['clip_left'])
        self.assertFalse(data['clip_right'])

    async def test_receiver_name_update_preserves_device_name(self):
        await self.login()
        response = await self.post('/api/configuration', {'adapter': {'address': USB, 'alias': 'Radio USB'}})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.backend.snapshot()['adapters'][1]['alias'], 'Radio USB')
        self.assertEqual(self.backend.devices[2]['name'], 'Ordinateur portable avec un nom très long')

    async def test_source_management_requires_auth_and_valid_schema(self):
        self.assertEqual((await self.post('/api/source', {'action': 'retire', 'address': PHONE})).status, 401)
        await self.login()
        for body in ({'action': 'assign', 'address': PHONE},
                     {'action': 'assign', 'address': PHONE, 'adapter': '00:00:00:00:00:00'},
                     {'action': 'retire', 'address': PHONE, 'name': 'Alias'},
                     {'action': 'retire', 'address': 'AA:BB:CC:DD:EE:FF'},
                     {'action': 'restore', 'address': PHONE, 'adapter': USB}):
            self.assertEqual((await self.post('/api/source', body)).status, 400)
        self.assertEqual(len(self.backend.devices), 3)

    async def test_web_assign_retire_restore_and_persistence(self):
        await self.login()
        response = await self.post('/api/source', {'action': 'assign', 'address': PHONE, 'adapter': USB})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['state']['devices'][0]['adapter'], USB)
        response = await self.post('/api/source', {'action': 'retire', 'address': PHONE})
        state = (await response.json())['state']
        self.assertEqual(len(state['devices']), 2)
        self.assertEqual(state['retired_devices'][0]['name'], 'Smartphone')
        restarted = DemoBackend(StateFile(self.tmp.name))
        self.assertIsNone(restarted.find(PHONE))
        response = await self.post('/api/source', {'action': 'restore', 'address': PHONE, 'adapter': INTERNAL})
        self.assertEqual(response.status, 200)
        restarted = DemoBackend(StateFile(self.tmp.name))
        self.assertEqual(restarted.find(PHONE)['adapter'], INTERNAL)
        self.assertEqual(restarted.snapshot()['retired_devices'], [])


if __name__ == '__main__': unittest.main()

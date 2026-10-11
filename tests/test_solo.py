"""Solo must affect real PCM mute bits without losing the user's mix."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from app import DemoBackend, StateFile, create_app, decode_volume
from test_controls import FakePi, PHONE, WORK, PERSONAL, PCM, DEVICE, A2DP_SOURCE, A, P, Q, S, device_path


class SoloAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pi = FakePi(StateFile(self.tmp.name))
        await self.pi.refresh()
        self.pi.calls.clear()
        self.pi.mixer_calls.clear()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def source(self, address):
        return next(v for v in self.pi.snapshot()['devices'] if v['address'] == address)

    async def test_live_raw_capture_clears_bluealsa_mute_but_preserves_dsp_controls(self):
        from studio import StudioPlan
        await self.pi.set_device(WORK, {'muted': True})
        await self.pi.set_device(PHONE, {'solo': True})
        settings = self.pi.modules.source_settings(); settings['enabled'] = True
        self.pi.modules.studio.install(StudioPlan(settings))
        await self.pi.prepare_live_volumes(True)
        self.assertFalse(any(decode_volume(self.pi.pcms[path]['Volume'])[1] for path in (P, Q, S)))
        self.assertFalse(any(self.pi.pcms[path]['SoftVolume'] for path in (P, Q, S)))
        self.assertTrue(self.source(WORK)['muted'])
        self.assertTrue(self.source(PHONE)['solo'])
        self.assertTrue(self.source(WORK)['solo_suppressed'])
        settings['enabled'] = False; self.pi.modules.studio.install(StudioPlan(settings))
        await self.pi.prepare_live_volumes(False)
        self.assertTrue(self.pi.pcms[Q]['SoftVolume'])
        self.assertTrue(decode_volume(self.pi.pcms[Q]['Volume'])[1])
        self.assertTrue(self.source(WORK)['muted'])
        await self.pi.set_device(PHONE, {'solo': False})
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)
        self.assertTrue(decode_volume(self.pi.pcms[Q]['Volume'])[1])

    async def test_multiple_solos_silence_only_other_sources_and_restore_levels(self):
        await self.pi.set_device(PHONE, {'solo': True})
        self.assertEqual(self.pi.pcms[P]['Volume'], 0x7F7F)
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0xC0C0)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0xB0B0)
        self.assertTrue(self.source(PHONE)['solo'])
        self.assertTrue(self.source(WORK)['solo_suppressed'])
        self.assertFalse(self.source(WORK)['muted'])
        self.assertNotIn('solo_mutes', self.source(WORK))
        self.assertTrue(self.pi.pcms[Q]['SoftVolume'])
        await self.pi.set_device(PERSONAL, {'solo': True})
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)
        await self.pi.set_device(PHONE, {'solo': False})
        self.assertEqual(self.pi.pcms[P]['Volume'], 0xFFFF)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)
        await self.pi.set_device(PERSONAL, {'solo': False})
        self.assertEqual([self.pi.pcms[p]['Volume'] for p in (P, Q, S)], [0x7F7F, 0x4040, 0x3030])
        self.assertFalse(any(v['solo_suppressed'] for v in self.pi.snapshot()['devices']))
        self.assertFalse(self.pi.mixer_calls, 'SOLO must not reconfigure or mute MASTER')

    async def test_byte_array_partial_channel_mutes_survive_volume_changes(self):
        self.pi.audio_objects[Q][PCM]['Volume'] = bytes([0xC0, 0x20])
        await self.pi.refresh()
        await self.pi.set_device(PHONE, {'solo': True})
        await self.pi.set_device(WORK, {'volume': 50})
        self.assertEqual(self.pi.pcms[Q]['Volume'], bytes([0xC0, 0xC0]))
        await self.pi.set_device(PHONE, {'solo': False})
        self.assertEqual(self.pi.pcms[Q]['Volume'], bytes([0xC0, 0x40]))
        writes = [c for c in self.pi.calls if c[1] == Q and c[3] == 'Set' and c[5][1] == 'Volume']
        self.assertEqual(writes[-1][5][2].signature, 'ay')

    async def test_user_mute_remains_prioritary_and_editable_during_solo(self):
        await self.pi.set_device(WORK, {'muted': True})
        await self.pi.set_device(PHONE, {'solo': True})
        self.assertTrue(self.source(WORK)['muted'])
        await self.pi.set_device(WORK, {'muted': False})
        self.assertFalse(self.source(WORK)['muted'])
        self.assertTrue(decode_volume(self.pi.pcms[Q]['Volume'])[1])
        await self.pi.set_device(WORK, {'muted': True, 'solo': True})
        self.assertTrue(self.source(WORK)['solo'])
        self.assertFalse(self.source(WORK)['solo_suppressed'])
        self.assertTrue(decode_volume(self.pi.pcms[Q]['Volume'])[1])
        await self.pi.set_device(PHONE, {'solo': False})
        await self.pi.set_device(WORK, {'solo': False})
        self.assertTrue(decode_volume(self.pi.pcms[Q]['Volume'])[1])

    async def test_saved_solo_restores_original_mutes_after_backend_restart(self):
        await self.pi.set_device(WORK, {'muted': True})
        await self.pi.set_device(PHONE, {'solo': True})
        await self.pi.set_device(WORK, {'muted': False})
        replacement = FakePi(StateFile(self.tmp.name))
        replacement.audio_objects = copy.deepcopy(self.pi.audio_objects)
        await replacement.refresh()
        self.pi = replacement
        self.assertTrue(self.source(PHONE)['solo'])
        self.assertFalse(self.source(WORK)['muted'])
        await self.pi.set_device(PHONE, {'solo': False})
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0x4040)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)

    async def test_reappearing_streams_are_excluded_and_refresh_is_idempotent(self):
        await self.pi.set_device(PHONE, {'solo': True})
        stream = self.pi.audio_objects.pop(Q)
        await self.pi.refresh()
        stream[PCM]['Volume'] = 0x5050
        self.pi.audio_objects[Q] = stream
        await self.pi.refresh()
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0xD0D0)
        self.pi.calls.clear()
        await self.pi.refresh()
        self.assertFalse(any(c[3] == 'Set' and c[5][1] == 'Volume' for c in self.pi.calls))
        await self.pi.set_device(PHONE, {'solo': False})
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0x5050)

    async def test_solo_can_be_cleared_without_a_stream_and_retiring_last_solo_restores_mix(self):
        self.pi.audio_objects.pop(P)
        self.pi.blue_objects[A][DEVICE]['Connected'] = False
        await self.pi.refresh()
        await self.pi.set_device(PHONE, {'solo': True})
        self.assertIsNone(self.source(PHONE)['volume'])
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0xC0C0)
        await self.pi.source_action(PHONE, 'retire')
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0x4040)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)
        self.assertFalse(self.pi.retired[PHONE]['solo'])

    async def test_newly_discovered_source_inherits_solo_exclusion(self):
        await self.pi.set_device(PHONE, {'solo': True})
        address = '02:00:00:00:00:42'
        path = device_path('hci0', address)
        pcm = '/org/bluealsa/new-source'
        self.pi.blue_objects[path] = {DEVICE: {'Address': address, 'Name': 'Nouvelle source',
                                              'Paired': True, 'Connected': True, 'UUIDs': [A2DP_SOURCE]}}
        self.pi.audio_objects[pcm] = {PCM: {'Device': path, 'Transport': 'A2DP-sink', 'Mode': 'source',
                                           'Volume': 0x3F7F, 'Running': True}}
        await self.pi.refresh()
        self.assertTrue(self.source(address)['solo_suppressed'])
        self.assertFalse(self.source(address)['muted'])
        self.assertEqual(self.pi.pcms[pcm]['Volume'], 0xBFFF)
        await self.pi.set_device(PHONE, {'solo': False})
        self.assertEqual(self.pi.pcms[pcm]['Volume'], 0x3F7F)

    async def test_failed_solo_write_rolls_back_selection_and_other_sources(self):
        setter = self.pi.set_property
        failed = False

        async def fail_once(destination, path, interface, name, signature, value):
            nonlocal failed
            if path == S and name == 'Volume' and not failed:
                failed = True
                raise RuntimeError('Échec D-Bus simulé')
            await setter(destination, path, interface, name, signature, value)

        with patch.object(self.pi, 'set_property', fail_once):
            with self.assertRaisesRegex(RuntimeError, 'D-Bus'):
                await self.pi.set_device(PHONE, {'solo': True})
        self.assertFalse(self.source(PHONE)['solo'])
        self.assertEqual(self.pi.pcms[Q]['Volume'], 0x4040)
        self.assertEqual(self.pi.pcms[S]['Volume'], 0x3030)
        self.assertFalse(any('solo_mutes' in v for v in self.pi.sources.values()))


class SoloWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = DemoBackend(StateFile(self.tmp.name))
        self.client = TestClient(TestServer(create_app(self.backend, 'test-key')), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def post(self, path, data):
        return await self.client.post(path, json=data, headers={'Origin': self.origin})

    async def test_solo_authentication_validation_and_state(self):
        self.assertEqual((await self.post('/api/device', {'address': PHONE, 'solo': True})).status, 401)
        await self.post('/api/login', {'key': 'test-key'})
        for value in (1, 'true', None, []):
            self.assertEqual((await self.post('/api/device', {'address': PHONE, 'solo': value})).status, 400)
        response = await self.post('/api/device', {'address': PHONE, 'solo': True})
        self.assertEqual(response.status, 200)
        devices = (await response.json())['state']['devices']
        self.assertTrue(next(v for v in devices if v['address'] == PHONE)['solo'])
        self.assertTrue(next(v for v in devices if v['address'] == WORK)['solo_suppressed'])
        self.assertFalse(next(v for v in devices if v['address'] == WORK)['muted'])
        restored = DemoBackend(StateFile(self.tmp.name))
        self.assertTrue(next(v for v in restored.snapshot()['devices'] if v['address'] == PHONE)['solo'])

    async def test_demo_meter_respects_solo_and_master_mute(self):
        await self.backend.set_device(WORK, {'solo': True})
        # WORK is the demo source with no running audio; the others must be silent.
        self.assertEqual(self.backend.levels()['left'], 0)
        self.assertEqual(self.backend.levels()['right'], 0)
        await self.backend.set_device(PHONE, {'solo': True})
        self.assertGreater(self.backend.levels()['left'], 0)
        await self.backend.set_device(PHONE, {'muted': True})
        self.assertEqual(self.backend.levels()['left'], 0)
        await self.backend.set_device(PHONE, {'muted': False})
        await self.backend.set_master({'muted': True})
        self.assertEqual(self.backend.levels()['left'], 0)

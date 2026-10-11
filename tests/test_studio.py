import asyncio
import copy
import math
import queue
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from app import DemoBackend, StateFile, create_app
from audio import AudioEngine
from equalizer import EqualizerPlan, default_equalizer
from live_audio import PCMBuffer, LiveAudio, alsa_live_config, bluetooth_pcm
from studio import (Dynamics, Feedback, MixerDSP, StudioPlan, WavRecorder,
                    checked_studio, dynamics_defaults, feedback_defaults, input_defaults, studio_defaults)

ADDRESS = '02:BA:10:00:00:01'


def tone(frequency=1000, amplitude=.2, frames=48000):
    mono = amplitude * np.sin(np.arange(frames) * 2 * math.pi * frequency / 48000)
    return np.column_stack((mono, mono))


class DSPTests(unittest.TestCase):
    def test_settings_reject_unknown_nan_bool_and_duplicate_outputs(self):
        for value in ({'enabled': 1}, {'period': True}, {'period': 7}, {'output_channels': 2.5},
                      {'inputs': {'bad/id': {}}}, {'unknown': 1},
                      {'buses': {'main': {'volume': float('nan')}}},
                      {'buses': {'monitor': {'channels': [0, 1]}}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_studio(value)
        with self.assertRaises(ValueError):
            checked_studio({'captures': [{'card': 'USB;touch', 'sources': []}]})
        with self.assertRaises(ValueError):
            checked_studio({'captures': [{'card': 'USB', 'channels': 2, 'sources': [{'id': 'usb_mic', 'channel': 2}]}],
                            'inputs': {'usb_mic': input_defaults()}})

    def test_float_summing_retains_headroom_until_limiter(self):
        settings = studio_defaults()
        settings['inputs'] = {'usb_one': input_defaults(), 'usb_two': input_defaults()}
        settings['buses']['main']['dynamics'].update(limiter=True, ceiling=-1)
        output = MixerDSP().process({'usb_one': np.full((480, 2), .8), 'usb_two': np.full((480, 2), .8)},
            StudioPlan(settings), EqualizerPlan.create({}), {'volume': 100, 'gain': 0, 'muted': False})
        self.assertAlmostEqual(float(output['main'].max()), 10 ** (-1 / 20), places=10)
        self.assertAlmostEqual(float(output['stream'].max()), 1.6, places=10)

    def test_pfl_is_independent_of_source_mute_and_main_master(self):
        settings = studio_defaults(); item = input_defaults('Micro', 'mic')
        item.update(pfl=True, muted=True, volume=0)
        settings['inputs'] = {'usb_mic': item}
        signal = tone(frames=480)
        output = MixerDSP().process({'usb_mic': signal}, StudioPlan(settings), EqualizerPlan.create({}),
                                   {'volume': 0, 'gain': 0, 'muted': True})
        np.testing.assert_array_equal(output['main'], np.zeros_like(signal))
        np.testing.assert_allclose(output['cue'], signal)

    def test_muting_an_active_master_cuts_main_and_preserves_other_buses(self):
        settings = studio_defaults(); item = input_defaults(); item['pfl'] = True
        settings['inputs'] = {'usb_mic': item}
        settings['buses']['main']['feedback'].update(mode='assist', filters=[{'frequency':1000,'gain':-6,'q':60}])
        dsp, plan, eq = MixerDSP(), StudioPlan(settings), EqualizerPlan.create({})
        signal = tone(frames=480)
        master = {'volume': 100, 'gain': 0, 'muted': False}
        dsp.process({'usb_mic': signal}, plan, eq, master)
        master['muted'] = True
        output = dsp.process({'usb_mic': signal}, plan, eq, master)
        np.testing.assert_array_equal(output['main'], 0)
        np.testing.assert_allclose(output['cue'], signal)
        np.testing.assert_allclose(output['stream'], signal)

    def test_legacy_master_mute_suppresses_filter_tails(self):
        from studio import Studio
        from equalizer import EqualizerProcessor
        with tempfile.TemporaryDirectory() as directory:
            studio = Studio(StateFile(directory)); settings = studio_defaults()
            settings['buses']['main']['feedback'].update(mode='assist', filters=[{'frequency':1000,'gain':-6,'q':60}])
            studio.install(StudioPlan(settings))
            plan = EqualizerPlan.create({}); processor = EqualizerProcessor(plan)
            data = np.rint(tone(frames=1920)*32768).astype('<i2').tobytes()
            studio.process_legacy(data, processor, plan, {'muted':False})
            output = studio.process_legacy(data, processor, plan, {'muted':True})
            np.testing.assert_array_equal(np.frombuffer(output,dtype='<i2'),0)

    def test_pre_fader_sends_mix_minus_and_solo_only_change_main(self):
        settings = studio_defaults(); first, second = input_defaults(), input_defaults()
        first.update(volume=0, solo=True)
        first['sends']['monitor'].update(level=0, pre=True)
        second['sends']['stream']['excluded'] = True
        settings['inputs'] = {'usb_one': first, 'usb_two': second}
        signal = np.ones((480, 2)) * .1
        output = MixerDSP().process({'usb_one': signal, 'usb_two': signal}, StudioPlan(settings),
                                   EqualizerPlan.create({}), {'volume': 100, 'gain': 0, 'muted': False})
        np.testing.assert_allclose(output['main'], 0)
        np.testing.assert_allclose(output['monitor'], signal)
        np.testing.assert_allclose(output['stream'], 0)

    def test_input_eq_operates_before_independent_bus_eq(self):
        settings = studio_defaults(); item = input_defaults()
        item['eq']['enabled'] = True
        item['eq']['bands'][3].update(frequency=1000, gain=-6)
        settings['inputs'] = {'usb_mic': item}
        settings['buses']['main']['eq']['enabled'] = True
        settings['buses']['main']['eq']['bands'][3].update(frequency=1000, gain=-6)
        output = MixerDSP().process({'usb_mic': tone()}, StudioPlan(settings), EqualizerPlan.create({}),
                                   {'volume': 100, 'gain': 0, 'muted': False})
        self.assertAlmostEqual(float(np.max(output['main'][10000:])), .2 * 10 ** (-12 / 20), places=5)
        self.assertAlmostEqual(float(np.max(output['stream'][10000:])), .2 * 10 ** (-6 / 20), places=5)

    def test_compressor_and_limiter_stereo_linked_with_exact_ceiling(self):
        processor = Dynamics(); settings = dynamics_defaults()
        settings.update(enabled=True, threshold=-20, ratio=4, attack=.1, limiter=True, ceiling=-3)
        signal = np.column_stack((np.ones(48000), np.ones(48000) * .1))
        output = processor.process(signal, settings)
        self.assertLessEqual(float(abs(output).max()), 10 ** (-3 / 20) + 1e-10)
        np.testing.assert_allclose(output[:, 1], output[:, 0] * .1)
        self.assertGreater(processor.reduction, 10)

    def test_gate_holds_then_closes_and_reopens(self):
        settings = studio_defaults(); item = input_defaults('Micro', 'mic')
        item['gate'].update(enabled=True, threshold=-30, hold=100)
        settings['inputs'] = {'usb_mic': item}
        dsp, plan, eq = MixerDSP(), StudioPlan(settings), EqualizerPlan.create({})
        master = {'volume': 100, 'gain': 0, 'muted': False}
        dsp.process({'usb_mic': np.ones((480, 2)) * .1}, plan, eq, master)
        for _ in range(100):
            output = dsp.process({'usb_mic': np.ones((480, 2)) * .001}, plan, eq, master)
        self.assertLess(float(abs(output['main']).max()), 1e-6)
        for _ in range(10):
            output = dsp.process({'usb_mic': np.ones((480, 2)) * .1}, plan, eq, master)
        self.assertGreater(float(output['main'].max()), .09)

    def test_ducking_reduces_music_without_reducing_mic(self):
        settings = studio_defaults(); settings['ducking']['enabled'] = True
        settings['inputs'] = {'usb_mic': input_defaults('Mic', 'mic'), ADDRESS: input_defaults('Music')}
        settings['inputs']['usb_mic']['sends']['main']['excluded'] = True
        dsp, plan, eq = MixerDSP(), StudioPlan(settings), EqualizerPlan.create({})
        for _ in range(40):
            output = dsp.process({'usb_mic': np.ones((480, 2)) * .2, ADDRESS: np.ones((480, 2)) * .2},
                                plan, eq, {'volume': 100, 'gain': 0, 'muted': False})
        self.assertAlmostEqual(float(output['main'].max()), .2 * 10 ** (-15 / 20), places=5)
        self.assertGreater(float(output['stream'].max()), .2)

    def test_automix_attenuates_unused_mic(self):
        settings = studio_defaults(); settings['automix']['enabled'] = True
        settings['inputs'] = {'usb_one': input_defaults('One', 'mic'), 'usb_two': input_defaults('Two', 'mic')}
        dsp = MixerDSP()
        dsp.process({'usb_one': np.ones((480, 2)) * .2, 'usb_two': np.ones((480, 2)) * .001},
                    StudioPlan(settings), EqualizerPlan.create({}), {'volume': 100, 'gain': 0, 'muted': False})
        self.assertLess(dsp.telemetry['inputs']['usb_two']['automix_db'], -19)
        self.assertGreater(dsp.telemetry['inputs']['usb_one']['automix_db'], -.1)

    def test_feedback_assist_suggests_steady_tone_but_live_does_not_cut_it(self):
        signal = tone(1002, .2, frames=96000)
        for mode in ('assist', 'live'):
            dsp, settings = Feedback(), feedback_defaults(); settings['mode'] = mode
            for index in range(0, len(signal), 480):
                dsp.process(signal[index:index+480], settings)
            self.assertAlmostEqual(dsp.suggestion['frequency'], 1002, delta=6)
            self.assertEqual(dsp.live, [])

    def test_feedback_manual_notch_and_balance_bank_are_separate_from_eq(self):
        settings = feedback_defaults(); settings.update(mode='assist', filters=[{'frequency': 1000, 'gain': -12, 'q': 60}])
        dsp = Feedback(); output = dsp.process(tone(frames=24000), settings)
        self.assertAlmostEqual(float(output[10000:].max()), .2 * 10 ** (-12 / 20), places=5)
        balance = Feedback(); settings = feedback_defaults(); settings['mode'] = 'balance'
        signal = tone(1002, .2, frames=96000)
        for index in range(0, len(signal), 480):
            balance.process(signal[index:index+480], settings)
        self.assertEqual(len(balance.balance), 1)
        self.assertEqual(settings['filters'], [])

    def test_buffers_bound_backlog_recover_underflow_and_preserve_channels(self):
        buffer = PCMBuffer(2, 480)
        signal = np.column_stack((np.arange(6000) / 6000, np.zeros(6000)))
        buffer.append(signal)
        self.assertEqual(buffer.overruns, 1)
        block = buffer.take()
        self.assertEqual(block.shape, (480, 2))
        np.testing.assert_array_equal(block[:, 1], 0)
        for _ in range(4): buffer.take()
        self.assertGreater(buffer.underruns, 0)
        buffer.append(signal[:960]); self.assertTrue(buffer.primed)

    def test_wav_records_real_samples_and_finalizes_header(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = WavRecorder(directory); recorder.start('stream', 'post')
            signal = tone(frames=480); recorder.feed(signal); recorder.stop()
            with wave.open(str(recorder.directory / recorder.name)) as stream:
                self.assertEqual((stream.getnchannels(), stream.getframerate(), stream.getnframes()), (2, 48000, 480))
                samples = np.frombuffer(stream.readframes(480), dtype='<i2').reshape(-1, 2)
                np.testing.assert_array_equal(samples, np.rint(signal * 32768).astype('<i2'))
            self.assertTrue(recorder.snapshot()['files'][0]['ready'])

    def test_fractional_clock_correction_carries_phase_between_blocks(self):
        buffer = PCMBuffer(1, 480)
        buffer.append(np.arange(1400)[:, None].astype(float))
        first = buffer.take()
        buffer.append(np.arange(1400, 1880)[:, None].astype(float))
        second = buffer.take()
        self.assertGreater(buffer.phase, 0)
        self.assertGreater(second[0, 0], 480)
        self.assertLess(second[0, 0] - first[-1, 0], 1.01)
        buffer.append(np.zeros((10000, 1)))
        self.assertEqual(buffer.phase, 0)

    def test_zero_hold_gate_passes_a_loud_block_and_closed_mic_does_not_duck(self):
        settings = studio_defaults(); mic = input_defaults('Micro', 'mic')
        mic['gate'].update(enabled=True, hold=0, threshold=-20)
        settings['inputs'] = {'usb_mic': mic, 'usb_music': input_defaults()}
        settings['ducking']['enabled'] = True
        dsp, plan, eq, master = MixerDSP(), StudioPlan(settings), EqualizerPlan.create({}), {'volume': 100, 'gain': 0, 'muted': False}
        output = dsp.process({'usb_mic': np.full((480, 1), .3)}, plan, eq, master)
        self.assertGreater(output['main'].max(), .25)
        for _ in range(400):
            output = dsp.process({'usb_mic': np.full((480, 1), .04), 'usb_music': np.full((480, 2), .2)}, plan, eq, master)
        self.assertAlmostEqual(float(output['main'].max()), .2, places=3)

    def test_growing_tone_creates_live_filter_and_it_expires(self):
        settings = feedback_defaults(); settings.update(mode='live', release=5, max_live=1)
        fbq = Feedback()
        for index in range(180):
            phase = np.arange(index*480, (index+1)*480)*2*math.pi*1002/48000
            block = (.002 * 10 ** (index*.22/20) * np.sin(phase))[:, None]
            fbq.process(np.repeat(block, 2, axis=1), settings)
        self.assertEqual(len(fbq.live), 1)
        self.assertEqual(settings['filters'], [])
        for _ in range(600): fbq.process(np.zeros((480, 2)), settings)
        self.assertEqual(fbq.live, [])

    def test_recorder_refuses_low_space_and_stops_on_full_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = WavRecorder(directory)
            with patch('studio.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(RuntimeError, 'Espace disque'): recorder.start('main', 'post')
            recorder.queue, recorder.active = queue.Queue(maxsize=1), True
            recorder.feed(np.zeros((480, 2))); recorder.feed(np.zeros((480, 2)))
            self.assertFalse(recorder.active)
            self.assertIn('disque trop lent', recorder.error)

    def test_live_configuration_loads_isolated_bluetooth_plugin(self):
        with patch('live_audio.Path.is_file', return_value=True):
            config = alsa_live_config([ADDRESS])
        self.assertIn('pcm_type.bluealsa', config)
        self.assertIn('libasound_module_pcm_bluealsa.so', config)
        self.assertIn('pcm.' + bluetooth_pcm(ADDRESS), config)
        self.assertIn('type plug', config)


class StudioApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = StateFile(self.tmp.name)
        self.backend = DemoBackend(self.state)
        self.client = TestClient(TestServer(create_app(self.backend, 'test')))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')
        response = await self.client.post('/api/login', json={'key': 'test'}, headers={'Origin': self.origin})
        self.assertEqual(response.status, 200)

    async def asyncTearDown(self):
        await self.client.close(); self.tmp.cleanup()

    async def post(self, body):
        return await self.client.post('/api/studio', json=body, headers={'Origin': self.origin})

    async def test_configuration_persists_and_stale_revision_rejected(self):
        settings = self.backend.modules.source_settings(); settings['enabled'] = True
        settings['buses']['main']['dynamics']['limiter'] = True
        response = await self.post({'action': 'configure', 'settings': settings, 'revision': 0})
        self.assertEqual(response.status, 200, await response.text())
        restored = DemoBackend(StateFile(self.tmp.name))
        self.assertTrue(restored.modules.studio.plan.settings['enabled'])
        response = await self.post({'action': 'configure', 'settings': settings, 'revision': 0})
        self.assertEqual(response.status, 400)

    async def test_input_update_and_matrix_are_real_stored_settings(self):
        response = await self.post({'action': 'input', 'id': ADDRESS, 'changes': {'pfl': True, 'kind': 'remote'}})
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue(self.state.data['studio']['inputs'][ADDRESS]['pfl'])
        response = await self.post({'action': 'input', 'id': 'usb_unknown', 'changes': {'volume': 20}})
        self.assertEqual(response.status, 400)

    async def test_scene_save_recall_export_import_and_delete(self):
        await self.backend.set_master({'volume': 42})
        await self.backend.set_device(ADDRESS, {'volume': 23, 'muted': True})
        response = await self.post({'action': 'scene_save', 'name': 'Conférence'})
        self.assertEqual(response.status, 200, await response.text())
        exported = await self.client.get('/api/scenes/Conf%C3%A9rence')
        scene = await exported.json(); self.assertEqual(scene['version'], 1)
        await self.backend.set_master({'volume': 90})
        await self.backend.set_device(ADDRESS, {'volume': 80, 'muted': False})
        response = await self.post({'action': 'scene_recall', 'name': 'Conférence'})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.backend.master['volume'], 42)
        self.assertEqual(self.backend.find(ADDRESS)['volume'], 23)
        self.assertTrue(self.backend.find(ADDRESS)['muted'])
        response = await self.post({'action': 'scene_import', 'name': 'Copie', 'scene': scene})
        self.assertEqual(response.status, 200)
        response = await self.post({'action': 'scene_delete', 'name': 'Copie'})
        self.assertEqual(response.status, 200)
        self.assertNotIn('Copie', self.state.data['scenes'])

    async def test_invalid_scene_and_unknown_fields_have_no_side_effects(self):
        before = copy.deepcopy(self.state.data)
        response = await self.post({'action': 'scene_import', 'name': 'Bad', 'scene': {'version': 2}})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.state.data, before)
        response = await self.post({'action': 'input', 'id': ADDRESS, 'changes': {'command': 'rm'}})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.state.data, before)

    async def test_state_write_failure_does_not_change_active_plan(self):
        before = self.backend.modules.studio.plan
        with patch.object(self.state, 'save', side_effect=OSError('disk full')):
            response = await self.post({'action': 'bus', 'bus': 'main', 'changes': {'volume': 30}})
        self.assertEqual(response.status, 500)
        self.assertIs(self.backend.modules.studio.plan, before)
        self.assertNotIn('studio', self.state.data)

    async def test_demo_does_not_fabricate_recordings_and_download_is_authenticated(self):
        response = await self.post({'action': 'record', 'enabled': True, 'bus': 'main', 'tap': 'post'})
        self.assertEqual(response.status, 503)
        self.assertFalse(self.backend.modules.studio.recorder.active)
        self.client.session.cookie_jar.clear()
        response = await self.client.get('/api/scenes/test')
        self.assertEqual(response.status, 401)
        response = await self.client.get('/api/recordings/20260101-120000-12345678.wav')
        self.assertEqual(response.status, 401)

    async def test_same_origin_and_schema_checks_apply_to_new_api(self):
        response = await self.client.post('/api/studio', json={'action': 'refresh'})
        self.assertEqual(response.status, 403)
        response = await self.post({'action': 'record', 'enabled': 1, 'bus': 'main', 'tap': 'post'})
        self.assertEqual(response.status, 400)

    async def test_live_scene_keeps_extended_gain_and_return_to_classic_keeps_controls(self):
        settings = self.backend.modules.source_settings(); settings['enabled'] = True
        settings['inputs'][ADDRESS].update(gain=-12, volume=31, solo=True, muted=True)
        await self.backend.modules.commit(settings)
        await self.backend.modules.action({'action': 'scene_save', 'name': 'Live'})
        await self.backend.modules.action({'action': 'scene_recall', 'name': 'Live'})
        device = next(v for v in self.backend.snapshot()['devices'] if v['address'] == ADDRESS)
        self.assertEqual((device['gain'], device['volume'], device['solo']), (-12, 31, True))
        settings['enabled'] = False
        await self.backend.modules.commit(settings)
        device = next(v for v in self.backend.snapshot()['devices'] if v['address'] == ADDRESS)
        self.assertEqual((device['gain'], device['volume'], device['solo'], device['muted']), (0, 31, True, True))

    async def test_revision_and_filter_count_require_integers(self):
        settings = self.backend.modules.source_settings()
        response = await self.post({'action': 'configure', 'revision': 0.0, 'settings': settings})
        self.assertEqual(response.status, 400)
        settings['buses']['main']['feedback']['max_live'] = 2.5
        response = await self.post({'action': 'configure', 'revision': 0, 'settings': settings})
        self.assertEqual(response.status, 400)


class EngineStudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_audio import FakeRunner, FakeSpawn, controls
        self.tmp = tempfile.TemporaryDirectory(); self.state = StateFile(self.tmp.name)
        self.engine = AudioEngine(self.state, runner=FakeRunner(), spawn=FakeSpawn(), controls_provider=controls)
        self.output = {'card': 'USB', 'control': 'Bureau Audio Master', 'pcm': 'plughw:CARD=USB,DEV=0', 'mode': 'managed'}
        await self.engine.configure(self.output)

    async def asyncTearDown(self):
        await self.engine.close(); self.tmp.cleanup()

    async def test_studio_dynamics_changes_do_not_restart_legacy_players(self):
        previous = self.engine.process
        settings = studio_defaults(); settings['buses']['main']['dynamics']['limiter'] = True
        await self.engine.set_studio(settings)
        self.assertIs(previous, self.engine.process)
        self.assertTrue(self.state.data['studio']['buses']['main']['dynamics']['limiter'])

    async def test_failed_live_start_restores_original_route_and_plan(self):
        previous = self.engine.process
        settings = studio_defaults(); settings['enabled'] = True
        class BrokenLive:
            def __init__(self, engine): pass
            async def start(self, output): raise RuntimeError('USB missing')
            async def stop(self): pass
        with patch('audio.LiveAudio', BrokenLive):
            with self.assertRaisesRegex(RuntimeError, 'USB missing'):
                await self.engine.set_studio(settings)
        self.assertTrue(previous.terminated)
        self.assertFalse(self.engine.studio.plan.settings['enabled'])
        self.assertTrue(self.engine.snapshot()['applied'])
        self.assertNotIn('studio', self.state.data)

    async def test_raw_capture_preparation_happens_after_legacy_stop_and_restores_on_error(self):
        previous = self.engine.process
        settings = studio_defaults(); settings['enabled'] = True
        transitions = []
        async def prepare(enabled):
            self.assertTrue(previous.terminated)
            transitions.append(enabled)
        self.engine.prepare_inputs = prepare
        class BrokenLive:
            def __init__(self, engine): pass
            async def start(self, output): raise RuntimeError('USB missing')
            async def stop(self): pass
        with patch('audio.LiveAudio', BrokenLive):
            with self.assertRaisesRegex(RuntimeError, 'USB missing'): await self.engine.set_studio(settings)
        self.assertEqual(transitions, [True, False])
        self.assertFalse(self.engine.live_prepared)

    async def test_cancellation_waits_for_inflight_dsp_work(self):
        import threading
        from live_audio import audio_work
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        def work():
            started.set(); release.wait(1); done.set()
        task = asyncio.create_task(audio_work(work))
        while not started.is_set(): await asyncio.sleep(.001)
        task.cancel()
        asyncio.get_running_loop().call_later(.02, release.set)
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(done.is_set())

    async def test_live_route_uses_multichannel_pcm_and_software_master(self):
        settings = studio_defaults(); settings.update(enabled=True, output_channels=4)
        settings['buses']['cue']['channels'] = [2, 3]
        class FakeLive:
            def __init__(self, engine): self.engine, self.active = engine, False
            async def start(self, output): self.active, self.output = True, output
            async def stop(self): self.active = False
            def running(self): return self.active
            def snapshot(self): return {'available': self.active}
        with patch('audio.LiveAudio', FakeLive):
            await self.engine.set_studio(settings)
            self.assertTrue(self.engine.equalizer_snapshot()['available'])
            self.assertTrue(self.engine.snapshot()['applied'])
            probe = [call[0] for call in self.engine.runner.calls if '-f' in call[0]][-1]
            self.assertEqual(probe[probe.index('-c')+1], '4')
            self.assertEqual(probe[probe.index('-f')+1], 'S32_LE')
            count = len(self.engine.runner.calls)
            await self.engine.set_master({'volume': 23})
            self.assertEqual(len(self.engine.runner.calls), count)
            settings['enabled'] = False
            await self.engine.set_studio(settings)
            self.assertTrue(self.engine.snapshot()['applied'])

    async def test_live_float_output_maps_buses_to_distinct_hardware_pairs(self):
        settings = studio_defaults(); settings.update(enabled=True, output_channels=4)
        settings['buses']['cue']['channels'] = [2, 3]
        item = input_defaults(); item.update(pfl=True, muted=True)
        settings['inputs'] = {'usb_mic': item}
        bridge = LiveAudio(self.engine); bridge.block, bridge.dsp = 480, MixerDSP()
        from equalizer import EqualizerProcessor
        bridge.spectral = EqualizerProcessor(EqualizerPlan.create({}))
        output = bridge.render({'usb_mic': np.full((480, 1), .25)}, StudioPlan(settings),
                                EqualizerPlan.create({}), {'volume': 100, 'gain': 0, 'muted': False})
        samples = np.frombuffer(output, dtype='<i4').reshape(-1, 4)
        np.testing.assert_array_equal(samples[:, :2], 0)
        np.testing.assert_array_equal(samples[:, 2:], 536870912)


class LiveIOTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from studio import Studio
        self.studio = Studio(StateFile(self.tmp.name))
        settings = studio_defaults(); settings['enabled'] = True
        settings['captures'] = [{'card': 'USB', 'device': 0, 'channels': 2, 'sources': [{'id': 'usb_mic', 'channel': 0}]}]
        settings['inputs'] = {'usb_mic': input_defaults('Micro', 'mic')}
        self.studio.install(StudioPlan(settings))
        self.processes, self.calls = [], []
        self.block_capture, self.block_output = False, False
        owner = self
        class Process:
            def __init__(self):
                self.returncode, self.stdout, self.data = None, asyncio.StreamReader(), []
                self.stdin, self.waiter = self, asyncio.Event()
            def write(self, data): self.data.append(data)
            async def drain(self):
                if owner.block_output: await asyncio.Event().wait()
            def terminate(self): self.returncode = 0; self.waiter.set()
            def kill(self): self.returncode = -9; self.waiter.set()
            async def wait(self): await self.waiter.wait(); return self.returncode
        async def spawn(*args, **kwargs):
            self.calls.append((args, kwargs)); process = Process(); self.processes.append(process)
            if 'arecord' in args[0]:
                process.stdout.feed_data(np.full((480*12, 2), 536870912, dtype='<i4').tobytes())
            return process
        async def stderr(process): await process.waiter.wait()
        self.engine = SimpleNamespace(studio=self.studio, equalizer=EqualizerPlan.create({}), master={'volume': 100, 'gain': 0, 'muted': False},
                                      inputs={}, stderr='', spawn=spawn, _read_stderr=stderr, environment=lambda: {'ALSA_CONFIG_PATH': 'test.conf'})
        self.live = LiveAudio(self.engine)

    async def asyncTearDown(self):
        await self.live.stop(); self.studio.recorder.stop(); self.tmp.cleanup()

    async def test_capture_playback_real_loops_forward_samples_and_reap_children(self):
        await self.live.start({'pcm': 'plughw:CARD=USB,DEV=0'})
        self.assertTrue(self.live.running())
        self.assertTrue(self.processes[0].data)
        samples = np.frombuffer(self.processes[0].data[0], dtype='<i4')
        np.testing.assert_array_equal(samples, 536870912)
        self.assertTrue(all('--disable-channels' in args for args, _ in self.calls))
        await self.live.stop()
        self.assertTrue(all(p.returncode is not None for p in self.processes))
        self.assertTrue(all(task.done() for task in self.live.tasks))

    async def test_usb_eof_stops_output_and_reports_capture(self):
        await self.live.start({'pcm': 'plughw:CARD=USB,DEV=0'})
        self.processes[1].stdout.feed_eof()
        for _ in range(30):
            if self.processes[0].returncode is not None: break
            await asyncio.sleep(.01)
        self.assertFalse(self.live.running())
        self.assertIn('Capture USB interrompue', self.live.error)
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_output_backpressure_stops_engine_instead_of_hanging(self):
        await self.live.start({'pcm': 'plughw:CARD=USB,DEV=0'})
        self.block_output = True
        await asyncio.wait_for(self.live.task, 1)
        self.assertFalse(self.live.running())
        self.assertIn('Sortie live interrompue', self.live.error)
        self.assertIsNotNone(self.processes[0].returncode)


if __name__ == '__main__':
    unittest.main()

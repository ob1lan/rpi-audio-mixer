"""Measured EQ behavior, streaming continuity, persistence and protected API."""
import asyncio
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from app import DemoBackend, StateFile, create_app
from audio import AudioEngine, OutputMeter, pcm_peaks
from equalizer import (EqualizerPlan, EqualizerProcessor, biquad, checked_equalizer,
                       default_equalizer, response_db)


def tone(frequency=1000, amplitude=.1, frames=24000):
    left = amplitude * np.sin(2 * np.pi * frequency * np.arange(frames) / 48000)
    return np.rint(np.column_stack((left, np.zeros(frames))) * 32768).astype('<i2').tobytes()


def samples(data):
    return np.frombuffer(data, dtype='<i2').reshape(-1, 2).astype(float) / 32768


class FilterTests(unittest.TestCase):
    def test_flat_and_bypass_preserve_every_input_byte(self):
        data = np.random.default_rng(1).integers(-32768, 32768, (1920, 2), dtype=np.int16).astype('<i2').tobytes()
        for enabled in (False, True):
            plan = EqualizerPlan.create({'enabled': enabled})
            processor = EqualizerProcessor(plan)
            self.assertEqual(processor.process(data, plan), data)

    def test_real_tone_cut_and_boost_match_the_requested_gain(self):
        data = tone()
        before = np.sqrt(np.mean(samples(data)[4000:, 0] ** 2))
        for gain in (-6, 6):
            settings = default_equalizer()
            settings.update(enabled=True, auto_headroom=False)
            settings['bands'][2].update(frequency=1000, gain=gain, q=2)
            plan = EqualizerPlan.create(settings)
            after = samples(EqualizerProcessor(plan).process(data, plan))
            measured = 20 * np.log10(np.sqrt(np.mean(after[4000:, 0] ** 2)) / before)
            self.assertAlmostEqual(measured, gain, delta=.02)
            self.assertTrue(np.all(after[:, 1] == 0), 'Left/right filter memories must be independent.')

    def test_cutoff_filters_reject_their_stopbands(self):
        for kind, pass_frequency, stop_frequency in (('highpass', 4000, 100), ('lowpass', 100, 4000)):
            settings = default_equalizer()
            settings.update(enabled=True, auto_headroom=False)
            settings[kind] = {'enabled': True, 'frequency': 1000}
            plan = EqualizerPlan.create(settings)
            for frequency, maximum, minimum in ((stop_frequency, -23, -100), (pass_frequency, .01, -.1)):
                data = tone(frequency)
                output = EqualizerProcessor(plan).process(data, plan)
                ratio = np.sqrt(np.mean(samples(output)[4000:, 0] ** 2)) / np.sqrt(np.mean(samples(data)[4000:, 0] ** 2))
                measured = 20 * np.log10(ratio)
                self.assertLess(measured, maximum)
                self.assertGreater(measured, minimum)

    def test_shelves_have_correct_passband_gain(self):
        for kind, frequency in (('low_shelf', 20), ('high_shelf', 20000)):
            rows = [biquad(kind, 1000, gain=6)]
            self.assertAlmostEqual(response_db(rows, frequency), 6, delta=.01)

    def test_streaming_chunks_match_one_continuous_render(self):
        settings = default_equalizer()
        settings.update(enabled=True, auto_headroom=False)
        settings['bands'][0].update(frequency=80, gain=-9, q=8)
        plan = EqualizerPlan.create(settings)
        data = tone(80, frames=48000)
        whole = EqualizerProcessor(plan).process(data, plan)
        processor = EqualizerProcessor(plan)
        chunked = b''.join(processor.process(data[i:i+7680], plan) for i in range(0, len(data), 7680))
        self.assertEqual(chunked, whole)

    def test_automatic_headroom_accounts_for_overlapping_bands(self):
        settings = default_equalizer()
        settings['bands'][0].update(frequency=1000, gain=6)
        settings['bands'][1].update(frequency=1000, gain=6)
        plan = EqualizerPlan.create(settings)
        self.assertAlmostEqual(plan.compensation, -12, delta=.001)
        self.assertAlmostEqual(response_db(plan.coefficients, 1000)+plan.effective_preamp, 0, delta=.001)

    def test_positive_gain_saturates_without_integer_wraparound(self):
        settings = default_equalizer()
        settings.update(enabled=True, auto_headroom=False)
        settings['bands'][0].update(frequency=1000, gain=12)
        plan = EqualizerPlan.create(settings)
        output = samples(EqualizerProcessor(plan).process(tone(amplitude=.9), plan))
        self.assertEqual(output[:, 0].max(), 32767/32768)
        self.assertEqual(output[:, 0].min(), -1)
        self.assertGreater(np.mean(output[:, 0] * samples(tone(amplitude=.9))[:, 0]), .4)

    def test_bypass_transition_then_exact_original_bytes(self):
        settings = default_equalizer()
        settings.update(enabled=True, preamp=-12)
        active = EqualizerPlan.create(settings)
        bypass = EqualizerPlan.create({**settings, 'enabled': False})
        data = tone(frames=1920)
        processor = EqualizerProcessor(active)
        processor.process(data, active)
        transition = processor.process(data, bypass)
        self.assertNotEqual(transition, data)
        self.assertEqual(processor.process(data, bypass), data)

    def test_all_filter_extremes_are_stable(self):
        for kind in ('peak', 'low_shelf', 'high_shelf', 'lowpass', 'highpass'):
            for frequency in (20, 20000):
                for gain in (-12, 12):
                    row = biquad(kind, frequency, gain, .2 if kind == 'peak' else math.sqrt(.5))
                    self.assertTrue(np.all(np.abs(np.roots(row[3:])) < 1))

    def test_spectrum_measures_the_processed_output(self):
        settings = default_equalizer()
        settings.update(enabled=True, auto_headroom=False)
        settings['bands'][2].update(frequency=1000, gain=-6)
        plan = EqualizerPlan.create(settings)
        processor = EqualizerProcessor(plan)
        processor.process(tone(), plan)
        index = max(range(96), key=lambda i: processor.spectrum['db'][i])
        self.assertLess(abs(processor.spectrum['frequencies'][index]-1000), 150)
        self.assertAlmostEqual(processor.spectrum['db'][index], -26, delta=2)

    def test_validation_rejects_bad_numbers_types_shapes_and_cutoffs(self):
        invalid = [{'enabled': 1}, {'typo': 1}, {'preamp': True}, {'preamp': float('nan')},
                   {'preamp': 1}, {'auto_headroom': 'yes'}, {'bands': []}, {'bands': {}},
                   {'highpass': {'frequency': 0}}, {'lowpass': {'enabled': 1}},
                   {'highpass': {'enabled': True, 'frequency': 2000}, 'lowpass': {'enabled': True, 'frequency': 1000}}]
        for key, value in (('frequency', 20001), ('gain', -13), ('q', 0), ('frequency', float('inf')),
                           ('type', 'invalid'), ('enabled', 'true'), ('unknown', 1)):
            item = default_equalizer()
            item['bands'][0][key] = value
            invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                checked_equalizer(item)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = StateFile(self.tmp.name)
        self.engine = AudioEngine(self.state)

    async def test_unavailable_route_can_save_bypass_but_cannot_enable(self):
        await self.engine.set_equalizer({'preamp': -6})
        self.assertEqual(self.state.data['equalizer']['preamp'], -6)
        with self.assertRaises(RuntimeError): await self.engine.set_equalizer({'enabled': True})
        self.assertFalse(self.engine.equalizer.settings['enabled'])
        self.assertEqual(self.engine.equalizer_revision, 1)

    async def test_saving_settings_does_not_restart_players_and_survives_reload(self):
        self.engine.applied = True
        self.engine.meter = SimpleNamespace(running=lambda: True)
        self.engine.process = SimpleNamespace(returncode=None)
        original = self.engine.process
        await self.engine.set_equalizer({'enabled': True, 'preamp': -5})
        self.assertIs(self.engine.process, original)
        reloaded = AudioEngine(StateFile(self.tmp.name))
        self.assertEqual(reloaded.equalizer.settings, self.engine.equalizer.settings)
        self.assertEqual(reloaded.equalizer_revision, 1)

    async def test_failed_persistence_keeps_previous_runtime_and_state(self):
        before = copy.deepcopy(self.state.data)
        with patch.object(self.state, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): await self.engine.set_equalizer({'preamp': -12})
        self.assertEqual(self.state.data, before)
        self.assertEqual(self.engine.equalizer.settings['preamp'], 0)

    async def test_loopback_forwards_filtered_frames_and_meters_them(self):
        plan = EqualizerPlan.create({'enabled': True, 'preamp': -6})
        self.engine.equalizer = plan
        meter = OutputMeter(self.engine)
        meter.processes = [SimpleNamespace(returncode=None), SimpleNamespace(returncode=None)]
        reader, written = asyncio.StreamReader(), bytearray()
        forwarded = asyncio.Event()
        class Writer:
            def write(self, value):
                written.extend(value)
                forwarded.set()
            async def drain(self): pass
        task = asyncio.create_task(meter.forward(SimpleNamespace(stdout=reader), SimpleNamespace(stdin=Writer())))
        data = tone(frames=1920)
        reader.feed_data(data)
        await asyncio.wait_for(forwarded.wait(), 10)
        self.assertEqual(len(written), len(data))
        self.assertNotEqual(bytes(written), data)
        self.assertEqual(meter.peaks, pcm_peaks(bytes(written)))
        reader.feed_eof()
        await task
        self.assertFalse(meter.snapshot()['available'])
        self.assertIsNone(meter.snapshot()['spectrum'])


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = DemoBackend(StateFile(self.tmp.name))
        self.client = TestClient(TestServer(create_app(self.backend, 'eq-key')), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def post(self, body, path='/api/equalizer'):
        return await self.client.post(path, json=body, headers={'Origin': self.origin})

    async def login(self):
        self.assertEqual((await self.post({'key': 'eq-key'}, '/api/login')).status, 200)

    async def test_auth_origin_and_validation_protect_settings(self):
        self.assertEqual((await self.post({'enabled': True})).status, 401)
        await self.login()
        self.assertEqual((await self.client.post('/api/equalizer', json={'enabled': True}, headers={'Origin': 'https://example.com'})).status, 403)
        before = copy.deepcopy(self.backend.state.data)
        for body in ({'enabled': 'yes'}, {'unknown': True}, {'bands': []}, {}, []):
            self.assertEqual((await self.post(body)).status, 400)
        self.assertEqual(self.backend.state.data, before)

    async def test_valid_settings_are_returned_saved_and_marked_simulated(self):
        await self.login()
        response = await self.post({'enabled': True, 'preamp': -3})
        self.assertEqual(response.status, 200)
        eq = (await response.json())['state']['equalizer']
        self.assertTrue(eq['demo'])
        self.assertTrue(eq['active'])
        self.assertEqual(eq['revision'], 1)
        self.assertEqual(json.loads((Path(self.tmp.name)/'settings.json').read_text())['equalizer']['preamp'], -3)
        reloaded = DemoBackend(StateFile(self.tmp.name))
        self.assertEqual(reloaded.equalizer.settings, eq['settings'])

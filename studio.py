"""Live mixer plans, float DSP, feedback assistance and bounded WAV recording.

ALSA streams are owned by live_audio.py. No hardware is opened by this module.
Feedback detection is a spectral heuristic, not an acoustic stability guarantee.
"""
import copy
import datetime
import math
import queue
import re
import shutil
import threading
import time
import uuid
import wave
from pathlib import Path

from equalizer import EqualizerPlan, EqualizerProcessor, biquad, default_equalizer, checked_equalizer

BUSES = ('main', 'monitor', 'stream', 'cue')
BUS_NAMES = {'main': 'Salle', 'monitor': 'Retour', 'stream': 'Streaming', 'cue': 'Casque PFL'}
SOURCE_ID = re.compile(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}|usb_[a-z0-9_-]{1,32}')
CARD_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
RATE = 48000


def numeric(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Valeur attendue entre {low:g} et {high:g}.')
    return float(value)


def flag(value):
    if not isinstance(value, bool):
        raise ValueError('État vrai/faux attendu.')
    return value


def fields(value, defaults):
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('Réglage audio inconnu.')
    return {**copy.deepcopy(defaults), **value}


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise ValueError('Nom attendu : 1 à 64 caractères.')
    return value.strip()


def dynamics_defaults():
    return {'enabled': False, 'threshold': -18.0, 'ratio': 3.0, 'attack': 10.0,
            'release': 150.0, 'makeup': 0.0, 'limiter': False, 'ceiling': -1.0}


def checked_dynamics(value):
    result = fields(value, dynamics_defaults())
    for key in ('enabled', 'limiter'):
        result[key] = flag(result[key])
    for key, low, high in (('threshold', -60, 0), ('ratio', 1, 20), ('attack', .1, 100),
                          ('release', 10, 2000), ('makeup', 0, 12), ('ceiling', -12, 0)):
        result[key] = numeric(result[key], low, high)
    return result


def feedback_defaults():
    return {'mode': 'off', 'filters': [], 'sensitivity': 18.0, 'max_live': 4, 'release': 30.0}


def checked_feedback(value):
    result = fields(value, feedback_defaults())
    if result['mode'] not in ('off', 'assist', 'balance', 'live'):
        raise ValueError('Mode FBQ inconnu.')
    result['sensitivity'] = numeric(result['sensitivity'], 10, 40)
    result['release'] = numeric(result['release'], 5, 120)
    numeric(result['max_live'], 1, 8)
    if not isinstance(result['max_live'], int):
        raise ValueError('Un nombre entier de filtres FBQ est attendu.')
    if not isinstance(result['filters'], list) or len(result['filters']) > 8:
        raise ValueError('Huit filtres fixes FBQ maximum.')
    filters = []
    for item in result['filters']:
        item = fields(item, {'frequency': 1000, 'gain': -6, 'q': 40})
        filters.append({'frequency': numeric(item['frequency'], 40, 16000),
                        'gain': numeric(item['gain'], -24, -1), 'q': numeric(item['q'], 10, 100)})
    result['filters'] = filters
    return result


def input_defaults(name='Source', kind='music'):
    return {'name': name, 'kind': kind, 'volume': 100.0, 'gain': 0.0, 'muted': False,
            'solo': False, 'pfl': False, 'pan': 0.0, 'eq': default_equalizer(),
            'dynamics': dynamics_defaults(), 'gate': {'enabled': False, 'threshold': -45.0, 'hold': 150.0},
            'sends': {bus: {'level': 0.0 if bus in ('main', 'stream') else -60.0,
                            'pre': bus == 'monitor', 'excluded': False} for bus in BUSES if bus != 'cue'}}


def checked_input(value):
    result = fields(value, input_defaults())
    result['name'] = label(result['name'])
    if result['kind'] not in ('mic', 'music', 'remote'):
        raise ValueError('Type de source inconnu.')
    for key in ('muted', 'solo', 'pfl'):
        result[key] = flag(result[key])
    for key, low, high in (('volume', 0, 100), ('gain', -24, 24), ('pan', -1, 1)):
        result[key] = numeric(result[key], low, high)
    result['eq'] = checked_equalizer(result['eq'])
    result['dynamics'] = checked_dynamics(result['dynamics'])
    gate = fields(result['gate'], {'enabled': False, 'threshold': -45.0, 'hold': 150.0})
    result['gate'] = {'enabled': flag(gate['enabled']), 'threshold': numeric(gate['threshold'], -80, -10),
                      'hold': numeric(gate['hold'], 0, 2000)}
    sends = fields(result['sends'], input_defaults()['sends'])
    result['sends'] = {}
    for bus, send in sends.items():
        send = fields(send, input_defaults()['sends'][bus])
        result['sends'][bus] = {'level': numeric(send['level'], -60, 12),
                                'pre': flag(send['pre']), 'excluded': flag(send['excluded'])}
    return result


def studio_defaults():
    return {'enabled': False, 'period': 10, 'output_channels': 2, 'captures': [], 'inputs': {},
            'buses': {bus: {'name': BUS_NAMES[bus], 'volume': 100.0, 'muted': False,
                            'channels': [0, 1] if bus == 'main' else [], 'eq': default_equalizer(),
                            'dynamics': dynamics_defaults(), 'feedback': feedback_defaults()}
                      for bus in BUSES},
            'automix': {'enabled': False, 'floor': -20.0},
            'ducking': {'enabled': False, 'threshold': -35.0, 'depth': -15.0, 'release': 500.0}}


def checked_studio(value):
    result = fields(value, studio_defaults())
    result['enabled'] = flag(result['enabled'])
    if isinstance(result['period'], bool) or result['period'] not in (5, 10, 20, 40):
        raise ValueError('Période audio attendue : 5, 10, 20 ou 40 ms.')
    count = result['output_channels']
    if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 16:
        raise ValueError('Entre 2 et 16 canaux de sortie sont attendus.')
    if not isinstance(result['captures'], list) or len(result['captures']) > 4:
        raise ValueError('Quatre périphériques de capture maximum.')
    captures, capture_ids, source_ids = [], set(), set()
    for item in result['captures']:
        item = fields(item, {'card': '', 'device': 0, 'channels': 2, 'sources': []})
        if not isinstance(item['card'], str) or not CARD_ID.fullmatch(item['card']):
            raise ValueError('Carte de capture ALSA invalide.')
        for key, high in (('device', 255), ('channels', 32)):
            v = item[key]
            if isinstance(v, bool) or not isinstance(v, int) or not (1 if key == 'channels' else 0) <= v <= high:
                raise ValueError('Configuration de capture invalide.')
        identity = (item['card'], item['device'])
        if identity in capture_ids:
            raise ValueError('Une seule ouverture par périphérique de capture.')
        capture_ids.add(identity)
        if not isinstance(item['sources'], list) or len(item['sources']) > 32:
            raise ValueError('Canaux de capture invalides.')
        channels = []
        for source in item['sources']:
            if not isinstance(source, dict) or set(source) != {'id', 'channel'}:
                raise ValueError('Identifiant et canal de micro attendus.')
            sid, channel = source['id'], source['channel']
            if not isinstance(sid, str) or not sid.startswith('usb_') or not SOURCE_ID.fullmatch(sid) or sid in source_ids:
                raise ValueError('Identifiant de micro invalide ou en double.')
            if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel < item['channels']:
                raise ValueError('Canal de micro absent du périphérique.')
            source_ids.add(sid)
            channels.append(dict(source))
        captures.append({**item, 'sources': channels})
    result['captures'] = captures
    if not isinstance(result['inputs'], dict) or len(result['inputs']) > 48:
        raise ValueError('Quarante-huit sources maximum.')
    result['inputs'] = {sid: checked_input(item) for sid, item in result['inputs'].items()
                        if isinstance(sid, str) and SOURCE_ID.fullmatch(sid)}
    if len(result['inputs']) != len(value.get('inputs', {})):
        raise ValueError('Identifiant de source invalide.')
    if not source_ids <= set(result['inputs']):
        raise ValueError('Chaque micro capturé doit avoir ses réglages de source.')
    bus_values = fields(result['buses'], studio_defaults()['buses'])
    buses, used_channels = {}, set()
    for bus, item in bus_values.items():
        item = fields(item, studio_defaults()['buses'][bus])
        item['name'], item['volume'], item['muted'] = label(item['name']), numeric(item['volume'], 0, 100), flag(item['muted'])
        channels = item['channels']
        if not isinstance(channels, list) or len(channels) not in (0, 2) or any(
                isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < count for v in channels):
            raise ValueError('Chaque bus utilise une paire de canaux ou reste sans sortie physique.')
        if len(set(channels)) != len(channels) or used_channels.intersection(channels):
            raise ValueError('Les sorties physiques des bus doivent être distinctes.')
        used_channels.update(channels)
        item['eq'] = checked_equalizer(item['eq'])
        item['dynamics'], item['feedback'] = checked_dynamics(item['dynamics']), checked_feedback(item['feedback'])
        buses[bus] = item
    result['buses'] = buses
    automix = fields(result['automix'], studio_defaults()['automix'])
    result['automix'] = {'enabled': flag(automix['enabled']), 'floor': numeric(automix['floor'], -40, -6)}
    duck = fields(result['ducking'], studio_defaults()['ducking'])
    result['ducking'] = {'enabled': flag(duck['enabled']), 'threshold': numeric(duck['threshold'], -60, -10),
                          'depth': numeric(duck['depth'], -30, 0), 'release': numeric(duck['release'], 50, 2000)}
    return result


def topology(settings):
    return (settings['enabled'], settings['period'], settings['output_channels'],
            repr(settings['captures']))


class StudioPlan:
    def __init__(self, value):
        self.settings = checked_studio(value)
        self.input_eq = {sid: EqualizerPlan.create(item['eq']) for sid, item in self.settings['inputs'].items()}
        self.bus_eq = {bus: EqualizerPlan.create(item['eq']) for bus, item in self.settings['buses'].items()}


class Dynamics:
    """Stereo linked block detector, smoothed gain and a sample peak ceiling.

    This is a peak limiter (instant gain reduction, exponential release), not a
    true-peak oversampling limiter. Float sums are never clipped before it.
    """
    def __init__(self):
        self.gain, self.limiter_gain, self.reduction = 1.0, 1.0, 0.0

    def process(self, samples, settings):
        import numpy as np
        if not len(samples):
            return samples
        output = samples
        n = len(samples)
        if settings['enabled']:
            rms = float(np.sqrt(np.mean(samples ** 2)))
            level = 20 * math.log10(max(1e-9, rms))
            reduction = max(0, level - settings['threshold']) * (1 - 1 / settings['ratio'])
            target = 10 ** ((settings['makeup'] - reduction) / 20)
        else:
            target = 1.0
        tau = settings['attack'] if target < self.gain else settings['release']
        curve = target + (self.gain - target) * np.exp(-np.arange(1, n + 1) / (RATE * tau / 1000))
        self.gain = float(curve[-1])
        output = samples * curve[:, None]
        self.reduction = max(0, -20 * math.log10(max(1e-9, self.gain)) + (settings['makeup'] if settings['enabled'] else 0))
        if settings['limiter']:
            ceiling = 10 ** (settings['ceiling'] / 20)
            required = np.minimum(1, ceiling / np.maximum(1e-12, np.max(np.abs(output), axis=1)))
            # Minimum recurrence g[n] = min(required[n], g[n-1]/release).
            # A transformed cumulative minimum evaluates it natively in NumPy.
            decay = np.exp(-np.arange(1, n + 1) / (RATE * .080))
            gains = np.minimum.accumulate(np.minimum(required * decay, self.limiter_gain)) / decay
            gains = np.minimum(gains, 1)
            self.limiter_gain = float(gains[-1])
            output = output * gains[:, None]
            self.reduction += max(0, -20 * math.log10(max(1e-9, self.limiter_gain)))
        else:
            self.limiter_gain = 1
        return output


class Feedback:
    """8192-frame overlapping analysis, narrow notches and bounded live filters."""
    def __init__(self):
        import numpy as np
        self.history = np.empty((0, 2))
        self.window = np.hanning(8192)
        self.since_analysis = 0
        self.candidate, self.confidence, self.age, self.previous_db = None, 0, 0, -100
        self.suggestion, self.live, self.balance = None, [], []
        self.last_set, self.clock, self.signature, self.sos, self.zi = -10, 0, None, None, None
        self.mode = 'off'

    def process(self, samples, settings):
        import numpy as np
        from scipy.signal import sosfilt
        elapsed = len(samples) / RATE
        self.clock += elapsed
        if settings['mode'] != self.mode:
            self.live, self.balance = [], []
            self.candidate, self.suggestion, self.age, self.confidence = None, None, 0, 0
            self.mode = settings['mode']
        if settings['mode'] == 'off':
            self.suggestion = None
            return samples
        self.history = np.concatenate((self.history, samples))[-8192:]
        self.since_analysis += len(samples)
        if len(self.history) == 8192 and self.since_analysis >= 2048:
            analysis_time = self.since_analysis / RATE
            self.since_analysis = 0
            magnitude = np.max(abs(np.fft.rfft(self.history * self.window[:, None], axis=0)), axis=1) * 2 / self.window.sum()
            db = 20 * np.log10(np.maximum(magnitude, 1e-9))
            lo, hi = math.ceil(80 * 8192 / RATE), math.floor(16000 * 8192 / RATE)
            index = lo + int(np.argmax(db[lo:hi]))
            near = np.concatenate((db[max(lo, index - 20):max(lo, index - 3)], db[min(hi, index + 4):min(hi, index + 21)]))
            prominence = float(db[index] - np.median(near)) if len(near) else 0
            frequency = index * RATE / 8192
            if db[index] > -45 and prominence >= settings['sensitivity']:
                same = self.candidate is not None and abs(frequency - self.candidate) < max(12, frequency * .015)
                self.age = self.age + analysis_time if same else 0
                if not same:
                    self.confidence = 0
                growing = same and db[index] > self.previous_db + .3
                self.confidence = min(1, self.confidence + .15) if growing else max(0, self.confidence - .015)
                self.candidate, self.previous_db = frequency, float(db[index])
                if self.age >= .3:
                    self.suggestion = {'frequency': round(frequency, 1), 'db': round(float(db[index]), 1),
                                       'prominence': round(prominence, 1), 'confidence': round(self.confidence, 2)}
                    bank = self.balance if settings['mode'] == 'balance' else self.live
                    # Balance deliberately accepts steady ringing. Live requires
                    # repeated growth; sustained notes remain suggestions only.
                    automatic = settings['mode'] == 'balance' or (settings['mode'] == 'live' and self.confidence >= .6)
                    existing = settings['filters'] + self.balance + self.live
                    if automatic and self.age >= .6 and self.clock - self.last_set >= 2 and len(bank) < settings['max_live'] and not any(
                            abs(v['frequency'] - frequency) < max(20, frequency * .025) for v in existing):
                        bank.append({'frequency': round(frequency, 1), 'gain': -6.0, 'q': 60.0,
                                     'expires': self.clock + settings['release']})
                        self.last_set = self.clock
            else:
                self.candidate, self.suggestion, self.age, self.confidence = None, None, 0, 0
            self.live = [v for v in self.live if v['expires'] > self.clock]
        filters = settings['filters'] + self.balance + self.live
        signature = tuple((v['frequency'], v['gain'], v['q']) for v in filters)
        if signature != self.signature:
            self.signature = signature
            self.sos = np.array([biquad('peak', *v) for v in signature]) if signature else None
            self.zi = np.zeros((len(signature), 2, 2)) if signature else None
            if signature and len(self.history):
                _, self.zi = sosfilt(self.sos, self.history, axis=0, zi=self.zi)
        if self.sos is not None:
            output, self.zi = sosfilt(self.sos, samples, axis=0, zi=self.zi)
            return output
        return samples

    def snapshot(self):
        return {'suggestion': copy.deepcopy(self.suggestion),
                'live': [{k: v[k] for k in ('frequency', 'gain', 'q')} for v in self.live],
                'balance': [{k: v[k] for k in ('frequency', 'gain', 'q')} for v in self.balance]}


class WavRecorder:
    def __init__(self, directory):
        self.directory = Path(directory) / 'recordings'
        self.active, self.error, self.name, self.frames = False, '', None, 0
        self.bus, self.tap, self.thread, self.queue = 'main', 'post', None, None

    def start(self, bus, tap):
        if bus not in BUSES or tap not in ('pre', 'post'):
            raise ValueError('Bus et prise pre/post attendus.')
        if self.active or (self.thread and self.thread.is_alive()):
            raise ValueError('Un enregistrement est déjà en cours.')
        self.directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.directory).free < 128 * 1024 ** 2:
            raise RuntimeError('Espace disque insuffisant pour enregistrer.')
        self.name = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.wav'
        self.bus, self.tap, self.frames, self.error = bus, tap, 0, ''
        self.queue, self.active = queue.Queue(maxsize=256), True
        self.thread = threading.Thread(target=self._write, daemon=True, name='bureau-wav')
        self.thread.start()

    def _write(self):
        try:
            with wave.open(str(self.directory / self.name), 'wb') as stream:
                stream.setnchannels(2)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                next_disk_check = RATE * 5
                while self.active or not self.queue.empty():
                    try:
                        data = self.queue.get(timeout=.1)
                    except queue.Empty:
                        continue
                    stream.writeframesraw(data)
                    self.frames += len(data) // 4
                    low_space = False
                    if self.frames >= next_disk_check:
                        low_space = shutil.disk_usage(self.directory).free < 64 * 1024 ** 2
                        next_disk_check = self.frames + RATE * 5
                    if self.frames * 4 >= 2 * 1024 ** 3 or low_space:
                        self.error, self.active = 'Enregistrement arrêté : limite de fichier ou espace disque.', False
                        break
        except Exception as exc:
            self.error = 'Enregistrement interrompu : ' + str(exc)
        finally:
            self.active = False

    def feed(self, samples):
        import numpy as np
        if self.active:
            data = np.clip(np.rint(samples * 32768), -32768, 32767).astype('<i2').tobytes()
            try:
                self.queue.put_nowait(data)
            except queue.Full:
                self.error, self.active = 'Enregistrement arrêté : disque trop lent.', False

    def stop(self):
        self.active = False
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('Le disque ne répond pas ; finalisation WAV en attente.')

    def snapshot(self):
        files = []
        if self.directory.exists():
            for path in sorted(self.directory.glob('*.wav'), reverse=True)[:40]:
                if path.is_file() and not path.is_symlink():
                    files.append({'name': path.name, 'bytes': path.stat().st_size,
                                  'ready': path.name != self.name or not (self.thread and self.thread.is_alive())})
        return {'active': self.active, 'name': self.name, 'seconds': round(self.frames / RATE, 1),
                'bus': self.bus, 'tap': self.tap, 'error': self.error, 'files': files}


class MixerDSP:
    def __init__(self):
        self.eq, self.dynamics, self.feedback, self.gates, self.gains = {}, {}, {}, {}, {}
        self.duck_gain, self.telemetry = 1.0, {'inputs': {}, 'buses': {}}

    def filter(self, key, samples, plan):
        if key not in self.eq:
            self.eq[key] = EqualizerProcessor(plan)
        return self.eq[key].process_float(samples, plan)

    def dynamic(self, key, samples, settings):
        processor = self.dynamics.setdefault(key, Dynamics())
        return processor.process(samples, settings)

    def smooth(self, key, target, n, milliseconds=20):
        import numpy as np
        previous = self.gains.get(key, target)
        values = target + (previous - target) * np.exp(-np.arange(1, n + 1) / (RATE * milliseconds / 1000))
        self.gains[key] = float(values[-1])
        return values[:, None]

    def process(self, frames, plan, master_eq, master, recorder=None):
        import numpy as np
        settings = plan.settings
        n = len(next(iter(frames.values()))) if frames else settings['period'] * 48
        silence = np.zeros((n, 2))
        processed, powers, levels = {}, {}, {}
        for sid, item in settings['inputs'].items():
            raw = frames.get(sid, silence)
            samples = np.repeat(raw, 2, axis=1) if raw.shape[1] == 1 else raw.copy()
            samples *= self.smooth((sid, 'trim'), 10 ** (item['gain'] / 20), n)
            samples = self.filter(('input', sid), samples, plan.input_eq[sid])
            peak = float(np.max(abs(samples)))
            if item['gate']['enabled']:
                timer = self.gates.get(sid, 0)
                timer = max(n / RATE, item['gate']['hold'] / 1000) if peak >= 10 ** (item['gate']['threshold'] / 20) else max(0, timer - n / RATE)
                self.gates[sid] = timer
                samples *= self.smooth((sid, 'gate'), 1 if timer > 0 else 0, n, 10 if timer else 80)
            samples = self.dynamic(('input', sid), samples, item['dynamics'])
            powers[sid] = float(np.mean(samples ** 2))
            processed[sid] = samples
            levels[sid] = {'peak': peak, 'reduction': round(self.dynamics[('input', sid)].reduction, 1)}
        mic_power = {sid: powers[sid] for sid, item in settings['inputs'].items()
                     if item['kind'] == 'mic' and not item['muted']}
        total = sum(mic_power.values())
        speaking = max(mic_power.values(), default=0) >= 10 ** (settings['ducking']['threshold'] / 10)
        duck_target = 10 ** (settings['ducking']['depth'] / 20) if speaking and settings['ducking']['enabled'] else 1
        duck = self.smooth('duck', duck_target, n, 30 if speaking else settings['ducking']['release'])
        buses = {bus: silence.copy() for bus in BUSES}
        solo = any(item['solo'] for item in settings['inputs'].values())
        for sid, samples in processed.items():
            item = settings['inputs'][sid]
            if item['pfl']:
                buses['cue'] += samples  # Before fader, mute, pan, duck and automix.
            if item['muted']:
                continue
            auto = 1
            if settings['automix']['enabled'] and sid in mic_power:
                floor = 10 ** (settings['automix']['floor'] / 20)
                auto = max(floor, math.sqrt(mic_power[sid] / max(1e-12, total)))
            samples = samples * self.smooth((sid, 'automix'), auto, n, 100)
            levels[sid]['automix_db'] = round(20 * math.log10(max(1e-9, auto)), 1)
            if item['kind'] == 'music':
                samples = samples * duck
            # Stereo balance: do not crossfeed channels from stereo music.
            samples = samples * np.array([min(1, 1 - item['pan']), min(1, 1 + item['pan'])])
            post = samples * self.smooth((sid, 'fader'), item['volume'] / 100, n)
            for bus, send in item['sends'].items():
                if send['excluded'] or send['level'] <= -60 or (bus == 'main' and solo and not item['solo']):
                    continue
                selected = samples if send['pre'] else post
                buses[bus] += selected * self.smooth((sid, bus), 10 ** (send['level'] / 20), n)
        output, bus_levels = {}, {}
        for bus, samples in buses.items():
            item = settings['buses'][bus]
            if recorder and recorder.active and recorder.bus == bus and recorder.tap == 'pre':
                recorder.feed(samples)
            samples = self.filter(('bus', bus), samples, plan.bus_eq[bus])
            if bus == 'main':
                samples = self.filter('master', samples, master_eq)
                samples *= self.smooth('master_fader', master['volume'] / 100 * 10 ** (master['gain'] / 20), n)
                if master['muted']:
                    samples *= 0
            samples *= self.smooth(('bus_level', bus), item['volume'] / 100, n)
            if item['muted']:
                samples *= 0
            fbq = self.feedback.setdefault(bus, Feedback())
            samples = fbq.process(samples, item['feedback'])
            samples = self.dynamic(('bus', bus), samples, item['dynamics'])
            # Suppress IIR tails too; a muted bus must remain silent.
            if item['muted'] or (bus == 'main' and master['muted']):
                samples *= 0
            output[bus] = samples
            peak = np.max(abs(samples), axis=0).tolist()
            bus_levels[bus] = {'peaks': peak, 'clip': max(peak) >= 1,
                               'reduction': round(self.dynamics[('bus', bus)].reduction, 1), 'feedback': fbq.snapshot()}
            if recorder and recorder.active and recorder.bus == bus and recorder.tap == 'post':
                recorder.feed(samples)
        self.telemetry = {'inputs': levels, 'buses': bus_levels, 'duck_db': round(20 * math.log10(max(1e-9, float(duck[-1, 0]))), 1)}
        return output

    def legacy(self, samples, plan, recorder, master_muted=False):
        """Postmix modules; EQ has already been applied by OutputMeter."""
        import numpy as np
        if recorder.active and recorder.tap == 'pre':
            recorder.feed(samples)
        bus = plan.settings['buses']['main']
        samples = self.filter(('bus', 'main'), samples, plan.bus_eq['main'])
        samples *= self.smooth(('bus_level', 'main'), bus['volume'] / 100, len(samples))
        fbq = self.feedback.setdefault('main', Feedback())
        samples = fbq.process(samples, bus['feedback'])
        samples = self.dynamic(('bus', 'main'), samples, bus['dynamics'])
        if bus['muted'] or master_muted:
            samples *= 0
        if recorder.active and recorder.tap == 'post':
            recorder.feed(samples)
        self.telemetry = {'inputs': {}, 'buses': {'main': {'peaks': np.max(abs(samples), axis=0).tolist(),
                         'reduction': round(self.dynamics[('bus', 'main')].reduction, 1), 'feedback': fbq.snapshot()}}}
        return np.clip(np.rint(samples * 32768), -32768, 32767).astype('<i2').tobytes()


class Studio:
    def __init__(self, state):
        self.state = state
        self.plan = StudioPlan(state.data.get('studio', {}))
        self.revision = state.data.get('studio_revision', 0)
        self.recorder = WavRecorder(Path(state.path).parent)
        self.dsp = None
        self.capture_choices, self.last_telemetry, self.updated = [], {}, 0

    def install(self, plan):
        self.plan, self.revision = plan, self.revision + 1

    def process_legacy(self, data, equalizer, plan, master=None):
        import numpy as np
        if self.dsp is None:
            self.dsp = MixerDSP()
        samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2).astype(float) / 32768
        samples = equalizer.process_float(samples, plan)
        output = self.dsp.legacy(samples, self.plan, self.recorder, bool(master and master['muted']))
        equalizer._spectrum(np.frombuffer(output, dtype='<i2').reshape(-1, 2).astype(float) / 32768)
        self.last_telemetry, self.updated = self.dsp.telemetry, time.monotonic()
        return output

    def snapshot(self, available=False, demo=False):
        return {'settings': copy.deepcopy(self.plan.settings), 'revision': self.revision,
                'available': available, 'demo': demo, 'capture_choices': self.capture_choices,
                'recording': self.recorder.snapshot(),
                'scenes': [{'name': name} for name in self.state.data.get('scenes', {})],
                'meters': self.last_telemetry if available and time.monotonic() - self.updated < .5 else {}}

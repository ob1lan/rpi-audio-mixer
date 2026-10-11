"""Stereo master EQ: validated settings, RBJ biquads and a stateful PCM processor.

Coefficient formulae: https://www.w3.org/TR/audio-eq-cookbook/ . Audio stays
on the Pi; the browser only changes settings and displays measurements.
"""
import copy
import math
from dataclasses import dataclass


RATE = 48000
BAND_COUNT = 6
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def default_equalizer():
    return {'enabled': False, 'preamp': 0.0, 'auto_headroom': True,
            'bands': [{'enabled': True, 'type': 'peak', 'frequency': frequency,
                       'gain': 0.0, 'q': 1.0}
                      for frequency in (80, 250, 630, 1600, 4000, 10000)],
            'highpass': {'enabled': False, 'frequency': 40.0},
            'lowpass': {'enabled': False, 'frequency': 18000.0}}


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Valeur EQ attendue entre {low:g} et {high:g}.')
    return float(value)


def _boolean(value):
    if not isinstance(value, bool):
        raise ValueError('État EQ vrai/faux attendu.')
    return value


def checked_equalizer(value):
    defaults = default_equalizer()
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('Réglage EQ inconnu.')
    result = {**defaults, **value}
    result['enabled'] = _boolean(result['enabled'])
    result['auto_headroom'] = _boolean(result['auto_headroom'])
    result['preamp'] = _number(result['preamp'], -24, 0)
    if not isinstance(result['bands'], list) or len(result['bands']) != BAND_COUNT:
        raise ValueError('Six bandes EQ sont attendues.')
    bands = []
    for index, band in enumerate(result['bands']):
        if not isinstance(band, dict) or set(band) - set(defaults['bands'][index]):
            raise ValueError('Bande EQ invalide.')
        band = {**defaults['bands'][index], **band}
        if band['type'] not in ('peak', 'low_shelf', 'high_shelf'):
            raise ValueError('Type de filtre EQ inconnu.')
        bands.append({'enabled': _boolean(band['enabled']), 'type': band['type'],
                      'frequency': _number(band['frequency'], 20, 20000),
                      'gain': _number(band['gain'], -12, 12),
                      'q': _number(band['q'], .2, 12)})
    result['bands'] = bands
    for name in ('highpass', 'lowpass'):
        item = result[name]
        if not isinstance(item, dict) or set(item) - {'enabled', 'frequency'}:
            raise ValueError('Filtre de coupure EQ invalide.')
        item = {**defaults[name], **item}
        result[name] = {'enabled': _boolean(item['enabled']),
                        'frequency': _number(item['frequency'], 20, 20000)}
    if result['highpass']['enabled'] and result['lowpass']['enabled'] \
            and result['highpass']['frequency'] >= result['lowpass']['frequency']:
        raise ValueError('Le passe-haut doit être inférieur au passe-bas.')
    return result


def biquad(kind, frequency, gain=0.0, q=1.0):
    """Normalized [b0,b1,b2,a0,a1,a2]; shelves use monotonic slope S=1."""
    omega = 2 * math.pi * frequency / RATE
    cosine, sine = math.cos(omega), math.sin(omega)
    alpha = sine / (2 * q)
    a = 10 ** (gain / 40)
    if kind == 'peak':
        b = (1 + alpha * a, -2 * cosine, 1 - alpha * a)
        denominator = (1 + alpha / a, -2 * cosine, 1 - alpha / a)
    elif kind == 'highpass':
        b = ((1 + cosine) / 2, -(1 + cosine), (1 + cosine) / 2)
        denominator = (1 + alpha, -2 * cosine, 1 - alpha)
    elif kind == 'lowpass':
        b = ((1 - cosine) / 2, 1 - cosine, (1 - cosine) / 2)
        denominator = (1 + alpha, -2 * cosine, 1 - alpha)
    elif kind in ('low_shelf', 'high_shelf'):
        beta = sine * math.sqrt(2 * a)
        if kind == 'low_shelf':
            b = (a * ((a + 1) - (a - 1) * cosine + beta),
                 2 * a * ((a - 1) - (a + 1) * cosine),
                 a * ((a + 1) - (a - 1) * cosine - beta))
            denominator = ((a + 1) + (a - 1) * cosine + beta,
                           -2 * ((a - 1) + (a + 1) * cosine),
                           (a + 1) + (a - 1) * cosine - beta)
        else:
            b = (a * ((a + 1) + (a - 1) * cosine + beta),
                 -2 * a * ((a - 1) + (a + 1) * cosine),
                 a * ((a + 1) + (a - 1) * cosine - beta))
            denominator = ((a + 1) - (a - 1) * cosine + beta,
                           2 * ((a - 1) - (a + 1) * cosine),
                           (a + 1) - (a - 1) * cosine - beta)
    else:
        raise ValueError('Type de filtre EQ inconnu.')
    return tuple(v / denominator[0] for v in (*b, *denominator))


def response_db(coefficients, frequency):
    omega = 2 * math.pi * frequency / RATE
    z = complex(math.cos(omega), -math.sin(omega))
    amplitude = 1.0
    for b0, b1, b2, a0, a1, a2 in coefficients:
        amplitude *= abs((b0 + b1 * z + b2 * z * z) / (a0 + a1 * z + a2 * z * z))
    return 20 * math.log10(max(1e-12, amplitude))


@dataclass
class EqualizerPlan:
    settings: dict
    coefficients: tuple
    compensation: float

    @classmethod
    def create(cls, value):
        settings = checked_equalizer(value)
        coefficients = []
        for name in ('highpass', 'lowpass'):
            item = settings[name]
            coefficients.append(biquad(name, item['frequency'], q=math.sqrt(.5))
                                if item['enabled'] else IDENTITY)
        coefficients.extend(biquad(b['type'], b['frequency'], b['gain'], b['q'])
                            if b['enabled'] and b['gain'] else IDENTITY for b in settings['bands'])
        # Sample the combined response, including each exact band center. This
        # is gain compensation, not a limiter or a guarantee against clipping.
        frequencies = [20 * 1000 ** (i / 1023) for i in range(1024)]
        frequencies.extend(b['frequency'] for b in settings['bands'])
        peak = max(0.0, max(response_db(coefficients, f) for f in frequencies))
        return cls(settings, tuple(coefficients), -peak if settings['auto_headroom'] else 0.0)

    @property
    def effective_preamp(self):
        return self.settings['preamp'] + self.compensation

    @property
    def flat(self):
        return not self.settings['enabled'] or (self.effective_preamp == 0 and
                                                all(row == IDENTITY for row in self.coefficients))

    def snapshot(self, available, demo=False, message=''):
        return {'settings': copy.deepcopy(self.settings), 'available': available, 'demo': demo,
                'active': available and self.settings['enabled'],
                'compensation': round(self.compensation, 3),
                'effective_preamp': round(self.effective_preamp, 3), 'message': message}


class EqualizerProcessor:
    """Native SOS filtering off the web event loop, with stereo states and fades."""
    def __init__(self, plan):
        import numpy as np
        from scipy.signal import sosfilt
        self.np, self.sosfilt = np, sosfilt
        self.current = self._chain(plan)
        self.previous, self.fade_position = None, 0
        self.history = np.empty((0, 2), dtype=np.float64)
        self.frequencies = np.geomspace(20, 20000, 96)
        self.window = np.hanning(2048)
        bins = np.fft.rfftfreq(2048, 1 / RATE)
        self.spectrum_indices = []
        for frequency in self.frequencies:
            indices = np.flatnonzero((bins >= frequency / 1.075) & (bins < frequency * 1.075))
            self.spectrum_indices.append(indices if len(indices) else np.array([int(np.argmin(abs(bins-frequency)))]))
        self.spectrum_history = np.empty((0, 2), dtype=np.float64)
        self.spectrum = None

    def _chain(self, plan):
        np = self.np
        sos = np.array(plan.coefficients, dtype=np.float64)
        return {'plan': plan, 'sos': sos, 'state': np.zeros((len(sos), 2, 2)),
                'gain': 10 ** (plan.effective_preamp / 20)}

    def _filter(self, chain, samples):
        if chain['plan'].flat:
            return samples
        output, chain['state'] = self.sosfilt(chain['sos'], samples, axis=0, zi=chain['state'])
        return output * chain['gain']

    def process(self, data, plan):
        np = self.np
        if len(data) % 4:
            raise ValueError('Un bloc PCM doit contenir des trames stéréo complètes.')
        if not data:
            return data
        samples = np.frombuffer(data, dtype='<i2').reshape(-1, 2).astype(np.float64) / 32768
        output = self.process_float(samples, plan)
        quantized = np.clip(np.rint(output * 32768), -32768, 32767).astype('<i2')
        self._spectrum(quantized.astype(np.float64) / 32768)
        if np.array_equal(output, samples):
            return data
        return quantized.tobytes()

    def process_float(self, samples, plan):
        """Keep headroom until the final DAC conversion in the multibus mixer."""
        np = self.np
        if not len(samples):
            return samples
        if plan.settings != self.current['plan'].settings and self.previous is None:
            self.previous, self.current = self.current, self._chain(plan)
            if len(self.history):
                self._filter(self.current, self.history)
            self.fade_position = 0
        output = self._filter(self.current, samples)
        if self.previous is not None:
            old = self._filter(self.previous, samples)
            blend = np.minimum(1, (np.arange(len(samples)) + self.fade_position + 1) / 1920)[:, None]
            output = old * (1 - blend) + output * blend
            self.fade_position += len(samples)
            if self.fade_position >= 1920:
                self.previous = None
        self.history = np.concatenate((self.history, samples))[-8192:]
        return output

    def _spectrum(self, output):
        np = self.np
        self.spectrum_history = np.concatenate((self.spectrum_history, output))[-2048:]
        if len(self.spectrum_history) < 2048:
            return
        magnitude = np.max(np.abs(np.fft.rfft(self.spectrum_history * self.window[:, None], axis=0)), axis=1)
        magnitude *= 2 / self.window.sum()
        db = []
        for indices in self.spectrum_indices:
            amplitude = float(np.max(magnitude[indices])) if len(indices) else 0.0
            db.append(round(max(-100, min(0, 20 * math.log10(max(1e-5, amplitude)))), 1))
        self.spectrum = {'frequencies': self.frequencies.round(2).tolist(), 'db': db}

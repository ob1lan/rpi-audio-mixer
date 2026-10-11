"""Owned ALSA capture/playback processes for the float multibus mixer.

USB channels from one interface share one capture stream. Asynchronous Bluetooth
and additional USB clocks use bounded elastic buffers with linear drift correction.
Python/ALSA scheduling is not a hard real-time guarantee; measure on the target Pi.
"""
import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path

from equalizer import EqualizerProcessor
from studio import BUSES, MixerDSP, RATE


async def audio_work(function, *args):
    """Finish an in-flight DSP block before a route can reuse its state."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


def bluetooth_pcm(address):
    return 'bureau_live_' + address.replace(':', '').lower()


def alsa_live_config(inputs):
    """Also support the BlueALSA plugin in the installer's isolated prefix."""
    config = '</usr/share/alsa/alsa.conf>\n'
    library = Path('/opt/bureau-audio-tools/bluealsa-4.3.1/lib/alsa-lib/libasound_module_pcm_bluealsa.so')
    if library.is_file():
        config += f'pcm_type.bluealsa {{ lib "{library}"; }}\n'
    for address in inputs:
        config += f'''pcm.{bluetooth_pcm(address)} {{
    type plug
    slave.pcm {{ type bluealsa; device "{address}"; profile "a2dp"; service "org.bluealsa"; }}
}}
'''
    return config


class PCMBuffer:
    def __init__(self, channels, block, target_blocks=2):
        import numpy as np
        self.channels, self.block, self.target = channels, block, block * target_blocks
        self.samples = np.empty((0, channels))
        self.primed, self.overruns, self.underruns = False, 0, 0
        self.phase = 0.0

    def append(self, samples):
        import numpy as np
        self.samples = np.concatenate((self.samples, samples))
        limit = max(self.target * 4, self.block * 12)
        if len(self.samples) > limit:
            self.samples = self.samples[-self.target:]
            self.phase = 0.0
            self.overruns += 1
        self.primed = self.primed or len(self.samples) >= self.target

    def take(self):
        import numpy as np
        n = self.block
        if not self.primed:
            return np.zeros((n, self.channels))
        if len(self.samples) < n + 1:
            output = np.zeros((n, self.channels))
            output[:min(n, len(self.samples))] = self.samples[:n]
            self.samples = self.samples[min(n, len(self.samples)):]
            self.underruns += 1
            self.primed = False
            self.phase = 0.0
            return output
        # Slowly correct independent clock drift; bound pitch change to 0.3%.
        correction = max(-.003, min(.003, (len(self.samples) - self.target) / (RATE * 10)))
        ratio = min(1 + correction, (len(self.samples) - 1 - self.phase) / n)
        positions = self.phase + np.arange(n) * ratio
        consumed = int(self.phase + n * ratio)
        self.phase = self.phase + n * ratio - consumed
        lower = positions.astype(int)
        fraction = (positions - lower)[:, None]
        output = self.samples[lower] * (1 - fraction) + self.samples[lower + 1] * fraction
        self.samples = self.samples[consumed:]
        return output


class LiveAudio:
    def __init__(self, engine):
        self.engine = engine
        self.processes, self.tasks, self.buffers, self.errors = [], [], {}, {}
        self.playback, self.task, self.error, self.updated = None, None, '', 0
        self.peaks, self.spectrum = [0, 0], None
        self.dsp, self.spectral = None, None
        self.closed = False

    def environment(self):
        return self.engine.environment()

    def command(self, name):
        return shutil.which(name) or '/usr/bin/' + name

    def common(self, channels):
        period = self.engine.studio.plan.settings['period'] * 1000
        return ['-q', '-t', 'raw', '-f', 'S32_LE', '-c', str(channels), '-r', str(RATE),
                '--period-time=' + str(period), '--buffer-time=' + str(period * 4)]

    async def start(self, output):
        import numpy as np
        settings = self.engine.studio.plan.settings
        self.dsp = await asyncio.to_thread(MixerDSP)
        self.spectral = await asyncio.to_thread(EqualizerProcessor, self.engine.equalizer)
        self.block = settings['period'] * 48
        self.output = dict(output)
        self.playback = await self.engine.spawn(self.command('aplay'), '-D', output['pcm'],
            *self.common(settings['output_channels']), '--disable-channels', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=self.environment())
        self.processes.append(self.playback)
        self.tasks.append(asyncio.create_task(self.engine._read_stderr(self.playback)))
        required = []
        for index, capture in enumerate(settings['captures']):
            key = 'capture:' + str(index)
            buffer = PCMBuffer(capture['channels'], self.block)
            self.buffers[key] = buffer
            pcm = f"plughw:CARD={capture['card']},DEV={capture['device']}"
            task = asyncio.create_task(self.capture(key, pcm, buffer, required=True))
            self.tasks.append(task)
            required.append((key, buffer, task))
        for address in self.engine.inputs:
            buffer = PCMBuffer(2, self.block, target_blocks=6)
            self.buffers[address] = buffer
            self.tasks.append(asyncio.create_task(self.capture(address, bluetooth_pcm(address), buffer)))
        # Before accepting a route, require real frames from physical captures.
        deadline = time.monotonic() + 3
        while required and not all(buffer.primed for _, buffer, _ in required):
            if self.error or self.playback.returncode is not None:
                raise RuntimeError(self.error or self.engine.stderr or 'Sortie live indisponible.')
            if time.monotonic() >= deadline:
                raise RuntimeError('La capture USB ne fournit pas de trames. Vérifie les canaux et la fréquence 48 kHz.')
            await asyncio.sleep(.02)
        self.task = asyncio.create_task(self.forward())
        await asyncio.sleep(.12)
        if not self.running():
            raise RuntimeError(self.error or self.engine.stderr or 'Le moteur live ne démarre pas.')

    async def capture(self, key, pcm, buffer, required=False):
        import numpy as np
        while not self.closed:
            process, stderr_task = None, None
            try:
                process = await self.engine.spawn(self.command('arecord'), '-D', pcm, *self.common(buffer.channels),
                    *(['--disable-channels'] if required else []),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.environment())
                self.processes.append(process)
                stderr_task = asyncio.create_task(self.engine._read_stderr(process))
                while True:
                    data = await process.stdout.readexactly(self.block * buffer.channels * 4)
                    samples = np.frombuffer(data, dtype='<i4').reshape(-1, buffer.channels).astype(float) / 2147483648
                    buffer.append(samples)
                    self.errors.pop(key, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[key] = str(exc) or 'Flux audio interrompu.'
                buffer.primed = False
                buffer.phase = 0.0
                buffer.samples = np.empty((0, buffer.channels))
                if required:
                    self.error = f'Capture USB interrompue ({pcm}) : {exc}'
                    return
            finally:
                if stderr_task:
                    stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task
                if process:
                    await self.stop_process(process)
                    if process in self.processes:
                        self.processes.remove(process)
            if not self.closed:
                await asyncio.sleep(2)

    def render(self, frames, plan, master_eq, master):
        import numpy as np
        buses = self.dsp.process(frames, plan, master_eq, master, self.engine.studio.recorder)
        samples = np.zeros((self.block, plan.settings['output_channels']))
        for bus in BUSES:
            channels = plan.settings['buses'][bus]['channels']
            if channels:
                samples[:, channels] = buses[bus]
        main = np.clip(np.rint(buses['main'] * 32768), -32768, 32767).astype('<i2')
        self.peaks = np.max(abs(main.astype(float)), axis=0).tolist()
        self.peaks = [v / 32768 for v in self.peaks]
        self.spectral._spectrum(main.astype(float) / 32768)
        self.spectrum = self.spectral.spectrum
        self.engine.studio.last_telemetry = self.dsp.telemetry
        self.engine.studio.updated = time.monotonic()
        return np.clip(np.rint(samples * 2147483648), -2147483648, 2147483647).astype('<i4').tobytes()

    async def forward(self):
        clock = time.monotonic()
        try:
            while not self.closed and not self.error:
                plan = self.engine.studio.plan
                frames = {address: buffer.take() for address, buffer in self.buffers.items() if not address.startswith('capture:')}
                for index, capture in enumerate(plan.settings['captures']):
                    data = self.buffers['capture:' + str(index)].take()
                    for item in capture['sources']:
                        frames[item['id']] = data[:, item['channel']:item['channel'] + 1]
                output = await audio_work(self.render, frames, plan, self.engine.equalizer, dict(self.engine.master))
                self.playback.stdin.write(output)
                await asyncio.wait_for(self.playback.stdin.drain(), .5)
                self.updated = time.monotonic()
                clock += self.block / RATE
                # Recover scheduler stalls without emitting a backlog of blocks.
                if clock < self.updated - self.block / RATE:
                    clock = self.updated
                await asyncio.sleep(max(0, clock - time.monotonic()))
            if self.error:
                await self.stop_process(self.playback)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = 'Sortie live interrompue : ' + str(exc)
            await self.stop_process(self.playback)

    def running(self):
        return bool(not self.closed and not self.error and self.playback and self.playback.returncode is None and self.task and not self.task.done())

    def snapshot(self):
        fresh = self.running() and time.monotonic() - self.updated < .3
        return {'available': self.running(), 'demo': False, 'left': self.peaks[0] if fresh else 0,
                'right': self.peaks[1] if fresh else 0, 'clip_left': fresh and self.peaks[0] >= 32767 / 32768,
                'clip_right': fresh and self.peaks[1] >= 32767 / 32768, 'spectrum': self.spectrum if fresh else None,
                'message': self.error, 'streams': dict(self.errors),
                'buffers': {key: {'underruns': v.underruns, 'overruns': v.overruns} for key, v in self.buffers.items()}}

    async def stop_process(self, process):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def stop(self):
        self.closed = True
        tasks = [*self.tasks, *([self.task] if self.task else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for process in list(self.processes):
            await self.stop_process(process)
        self.processes = []

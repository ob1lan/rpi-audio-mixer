"""ALSA output discovery and an unprivileged, owned BlueALSA playback process.

Configuration lives in the application's state directory. No shell, system-wide
ALSA rewrite or privileged service operation is used by this module.
"""
import asyncio
import contextlib
import ctypes
import ctypes.util
import hashlib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


SOFTWARE_CONTROL = 'Bureau Audio Master'
SOFTWARE_GAIN_MAX = 4.0
CARD_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
PCM_RE = re.compile(r'[A-Za-z0-9_:,=.+-]{1,160}')


def executable(name):
    return shutil.which(name) or f'/usr/bin/{name}'


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ''
    stderr: str = ''


async def execute(args, input=None, env=None):
    """Run an argument list directly, with bounded duration and captured output."""
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input), 12)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    return CommandResult(process.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace'))


def parse_outputs(text):
    """Parse locale-independent aplay -l output, retaining stable ALSA card IDs."""
    choices = []
    expression = re.compile(r'^card \d+: ([^\s\[]+) \[([^\]]+)\], device (\d+): (.+)$')
    for line in text.splitlines():
        match = expression.match(line.strip())
        if not match or not CARD_RE.fullmatch(match[1]):
            continue
        card, name, device = match[1], match[2], int(match[3])
        choices.append({'card': card, 'name': name, 'device': device,
                        'pcm': f'plughw:CARD={card},DEV={device}'})
    return choices


def mixer_controls(card):
    """Read actual playback mixer capabilities and dB limits through libasound."""
    library = ctypes.util.find_library('asound')
    if not library:
        return None
    try:
        lib = ctypes.CDLL(library)
        ptr, long_ptr = ctypes.c_void_p, ctypes.POINTER(ctypes.c_long)
        signatures = {
            'snd_mixer_open': ([ctypes.POINTER(ptr), ctypes.c_int], ctypes.c_int),
            'snd_mixer_attach': ([ptr, ctypes.c_char_p], ctypes.c_int),
            'snd_mixer_selem_register': ([ptr, ptr, ptr], ctypes.c_int),
            'snd_mixer_load': ([ptr], ctypes.c_int),
            'snd_mixer_close': ([ptr], ctypes.c_int),
            'snd_mixer_first_elem': ([ptr], ptr),
            'snd_mixer_elem_next': ([ptr], ptr),
            'snd_mixer_selem_is_active': ([ptr], ctypes.c_int),
            'snd_mixer_selem_has_playback_volume': ([ptr], ctypes.c_int),
            'snd_mixer_selem_has_playback_switch': ([ptr], ctypes.c_int),
            'snd_mixer_selem_get_name': ([ptr], ctypes.c_char_p),
            'snd_mixer_selem_get_index': ([ptr], ctypes.c_uint),
            'snd_mixer_selem_get_playback_dB_range': ([ptr, long_ptr, long_ptr], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(lib, name)
            function.argtypes, function.restype = arguments, result
        handle = ptr()
        if lib.snd_mixer_open(ctypes.byref(handle), 0) < 0:
            return None
        try:
            if lib.snd_mixer_attach(handle, card.encode()) < 0:
                return None
            if lib.snd_mixer_selem_register(handle, None, None) < 0 or lib.snd_mixer_load(handle) < 0:
                return None
            result, element = [], lib.snd_mixer_first_elem(handle)
            while element:
                if lib.snd_mixer_selem_is_active(element) and lib.snd_mixer_selem_has_playback_volume(element):
                    name = lib.snd_mixer_selem_get_name(element).decode(errors='replace')
                    index = lib.snd_mixer_selem_get_index(element)
                    minimum, maximum = ctypes.c_long(), ctypes.c_long()
                    has_db = lib.snd_mixer_selem_get_playback_dB_range(
                        element, ctypes.byref(minimum), ctypes.byref(maximum)) >= 0
                    result.append({'name': name if not index else f'{name},{index}', 'index': index,
                                   'has_switch': bool(lib.snd_mixer_selem_has_playback_switch(element)),
                                   'has_db': has_db, 'gain_max': max(0, maximum.value / 100) if has_db else 0,
                                   'db_min': minimum.value / 100 if has_db else None, 'software': False})
                element = lib.snd_mixer_elem_next(element)
            return result
        finally:
            lib.snd_mixer_close(handle)
    except (AttributeError, OSError, ValueError):
        return None


def fallback_controls(text):
    """Conservative fallback: never advertise unknown positive hardware gain."""
    result = []
    for block in re.split(r'(?=^Simple mixer control )', text, flags=re.MULTILINE):
        match = re.match(r"Simple mixer control '(.+)',(\d+)", block)
        capabilities = re.search(r'^\s*Capabilities:\s*(.+)$', block, re.MULTILINE)
        if not match or not capabilities or 'pvolume' not in capabilities[1].split():
            continue
        index = int(match[2])
        result.append({'name': match[1] if not index else f'{match[1]},{index}', 'index': index,
                       'gain_max': 0, 'has_switch': 'pswitch' in capabilities[1].split(),
                       'has_db': bool(re.search(r'\[-?\d+(?:\.\d+)?dB\]', block)),
                       'db_min': None, 'software': False})
    return result


def hardware_diagnostics(text):
    """Report observed playback mute/zero levels without guessing device routing."""
    inactive = []
    for block in re.split(r'(?=^Simple mixer control )', text, flags=re.MULTILINE):
        name = re.match(r"Simple mixer control '(.+)',(\d+)", block)
        capabilities = re.search(r'^\s*Capabilities:\s*(.+)$', block, re.MULTILINE)
        if not name or name[1] == SOFTWARE_CONTROL or not capabilities or 'pvolume' not in capabilities[1].split():
            continue
        playback = '\n'.join(re.findall(r'^\s*[^\n]*:\s*Playback\b[^\n]*$', block, re.MULTILINE))
        percentages = re.findall(r'\[(\d+)%\]', playback)
        switches = re.findall(r'\[(on|off)\]', playback)
        if (percentages and all(int(v) == 0 for v in percentages)) or (switches and all(v == 'off' for v in switches)):
            inactive.append(name[1] if name[2] == '0' else f'{name[1]},{name[2]}')
    if not inactive:
        return []
    return ['Contrôles matériels muets ou à zéro sur cette carte : ' + ', '.join(inactive) +
            '. Ils peuvent bloquer l’écoute ; vérifie leurs réglages ou choisis un contrôle matériel dans la configuration.']


def checked_settings(value, gain_max):
    if not isinstance(value, dict) or set(value) - {'volume', 'gain', 'muted'}:
        raise ValueError('Réglages du volume général invalides.')
    result = {'volume': value.get('volume', 100), 'gain': value.get('gain', 0), 'muted': value.get('muted', False)}
    for field, low, high in (('volume', 0, 100), ('gain', 0, gain_max)):
        number = result[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'{field} doit être compris entre {low:g} et {high:g}.')
        result[field] = float(number)
    if not isinstance(result['muted'], bool):
        raise ValueError('État mute invalide.')
    return result


def alsa_config(output):
    """Render only validated values; leave the global ALSA configuration intact."""
    card, device = output['card'], output.get('device', 0)
    if not CARD_RE.fullmatch(card) or isinstance(device, bool) or not isinstance(device, int) or not 0 <= device <= 255:
        raise ValueError('Carte ou sortie ALSA invalide.')
    header = '</usr/share/alsa/alsa.conf>\n'
    if output['mode'] == 'existing':
        return header
    key = 0x420000 + int(hashlib.sha256(f'{card}:{device}'.encode()).hexdigest()[:5], 16)
    result = header + f'''pcm.bureau_mix {{
    type dmix
    ipc_key {key}
    ipc_key_add_uid true
    ipc_perm 0600
    slave {{
        pcm "hw:CARD={card},DEV={device}"
        format S16_LE
        rate 48000
        channels 2
        period_time 40000
        periods 5
    }}
    bindings {{ 0 0; 1 1; }}
}}
'''
    slave = 'bureau_mix'
    if output['control'] == SOFTWARE_CONTROL:
        result += f'''pcm.bureau_soft {{
    type softvol
    slave.pcm "bureau_mix"
    control {{ name "{SOFTWARE_CONTROL}"; card "{card}"; count 1; }}
    min_dB -60.0
    max_dB {SOFTWARE_GAIN_MAX:.1f}
    resolution 641
}}
'''
        slave = 'bureau_soft'
    return result + f'pcm.bureau_audio {{ type plug; slave.pcm "{slave}"; }}\n'


class AudioEngine:
    """Own one playback process, its real output route, and that route's master."""
    def __init__(self, state, runner=None, spawn=None, controls_provider=None):
        self.state = state
        self.directory = Path(state.dir) if hasattr(state, 'dir') else Path(state.path).parent
        self.config_path = self.directory / 'alsa-playback.conf'
        self.runner = runner or execute
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.controls_provider = controls_provider or mixer_controls
        self.output_choices, self.output, self.control = [], None, None
        self.master = {'volume': 100, 'gain': 0, 'muted': False}
        self.process, self.reader_task = None, None
        self.error, self.applied, self.stderr = '', False, ''
        self.master_known = False
        self.lock = asyncio.Lock()

    def environment(self):
        return {**os.environ, 'LC_ALL': 'C', 'ALSA_CONFIG_PATH': str(self.config_path)}

    async def command(self, args, input=None, env=None):
        result = await self.runner(args, input=input, env=env or {**os.environ, 'LC_ALL': 'C'})
        if result.returncode:
            message = (result.stderr or result.stdout).strip()[-1200:]
            raise RuntimeError(message or f'{Path(args[0]).name} a échoué ({result.returncode}).')
        return result.stdout

    async def discover(self):
        output = await self.command([executable('aplay'), '-l'])
        choices, controls, diagnostics = parse_outputs(output), {}, {}
        for choice in choices:
            card = choice['card']
            if card not in controls:
                items = await asyncio.to_thread(self.controls_provider, card)
                try:
                    mixer_text = await self.command([executable('amixer'), '-c', card, 'scontents'])
                except (RuntimeError, OSError):
                    mixer_text = ''
                if items is None:
                    items = fallback_controls(mixer_text)
                diagnostics[card] = hardware_diagnostics(mixer_text)
                items = [dict(item) for item in items if item.get('name') != SOFTWARE_CONTROL
                         and (item.get('has_switch') or (item.get('db_min') is not None and item['db_min'] < -90000))]
                controls[card] = [{'name': SOFTWARE_CONTROL, 'index': 0, 'gain_max': SOFTWARE_GAIN_MAX,
                                   'has_switch': False, 'has_db': True, 'db_min': -60, 'software': True}, *items]
            choice['controls'] = [dict(item) for item in controls[card]]
            choice['diagnostics'] = list(diagnostics[card])
        self.output_choices = choices
        if self.output and not any(item['card'] == self.output['card'] and item['device'] == self.output['device']
                                   for item in choices):
            self.applied = False
            self.error = 'La sortie audio configurée est déconnectée ou indisponible.'
        return choices

    def diagnostics(self, output):
        if not output:
            return []
        choice = next((item for item in self.output_choices if item['card'] == output.get('card')
                       and item['device'] == output.get('device', item['device'])), None)
        selected = next((item for item in choice['controls'] if item['name'] == output.get('control')), None) if choice else None
        return list(choice.get('diagnostics', [])) if selected and selected.get('software') else []

    async def normalize(self, output):
        if not isinstance(output, dict):
            raise ValueError('Sortie audio invalide.')
        card, control = output.get('card'), output.get('control')
        mode, pcm = output.get('mode', 'managed'), output.get('pcm', '')
        if not isinstance(card, str) or not CARD_RE.fullmatch(card):
            raise ValueError('Carte ALSA invalide.')
        if not isinstance(control, str) or not control or len(control) > 160 or any(ord(c) < 32 for c in control):
            raise ValueError('Contrôle de volume invalide.')
        if mode not in ('managed', 'existing'):
            raise ValueError('Mode de lecture invalide.')
        if not isinstance(pcm, str) or not PCM_RE.fullmatch(pcm):
            raise ValueError('Nom du PCM ALSA invalide.')
        choices = await self.discover()
        matching = [choice for choice in choices if choice['card'] == card]
        if not matching:
            raise ValueError('La carte audio choisie est absente.')
        device = output.get('device')
        if device is None:
            found = re.search(r'(?:^|,)DEV=(\d+)(?:,|$)', pcm)
            device = int(found[1]) if found else matching[0]['device']
        if isinstance(device, bool) or not isinstance(device, int) or not 0 <= device <= 255:
            raise ValueError('Numéro de sortie ALSA invalide.')
        choice = next((item for item in matching if item['device'] == device), None)
        if not choice:
            raise ValueError('La sortie choisie est absente de cette carte.')
        if mode == 'managed' and pcm != choice['pcm']:
            raise ValueError('Le PCM choisi ne correspond pas à cette carte et à cette sortie.')
        selected = next((item for item in choice['controls'] if item['name'] == control), None)
        if not selected:
            raise ValueError('Le contrôle de volume choisi est absent de cette carte.')
        if mode == 'existing' and selected['software']:
            raise ValueError('Un PCM existant doit utiliser son propre contrôle matériel.')
        label = output.get('label') or f"{choice['name']} · sortie {device}"
        if not isinstance(label, str) or len(label) > 200 or any(ord(c) < 32 for c in label):
            raise ValueError('Nom de sortie invalide.')
        return {'card': card, 'control': control, 'pcm': pcm, 'label': label,
                'device': device, 'mode': mode, 'gain_max': selected['gain_max']}, dict(selected)

    async def external_player_active(self):
        try:
            result = await self.runner([executable('systemctl'), 'is-active', 'bluealsa-aplay.service'],
                                       input=None, env={**os.environ, 'LC_ALL': 'C'})
            return result.returncode == 0 and result.stdout.strip() == 'active'
        except OSError:
            return False

    async def _apply_master(self, settings, output, control):
        settings = checked_settings(settings, control['gain_max'])
        arguments = [executable('amixer'), '-c', output['card'], '--', 'sset', output['control']]
        silent = settings['muted'] or settings['volume'] == 0
        if silent and not control['has_switch']:
            arguments.append('0')
        else:
            if control['has_db']:
                decibels = 20 * math.log10(max(settings['volume'], 0.00001) / 100) + settings['gain']
                if control.get('db_min') is not None:
                    decibels = max(control['db_min'], decibels)
                arguments.append(f'{decibels:.2f}dB')
            else:
                arguments.append(f"{settings['volume']:.2f}%")
            if control['has_switch']:
                arguments.append('mute' if silent else 'unmute')
        await self.command(arguments, env=self.environment())
        return settings

    async def _read_stderr(self, process):
        if process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(1024)
            if not chunk:
                return
            self.stderr = (self.stderr + chunk.decode(errors='replace'))[-4000:]

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
            self.reader_task = None

    async def _start(self, output):
        pcm = output['pcm'] if output['mode'] == 'existing' else 'bureau_audio'
        arguments = [executable('bluealsa-aplay'), '-D', pcm, '--volume=software',
                     '--pcm-period-time=40000', '--pcm-buffer-time=200000']
        self.stderr = ''
        process = await self.spawn(*arguments, stdin=asyncio.subprocess.DEVNULL,
                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                                   env=self.environment())
        self.process = process
        self.reader_task = asyncio.create_task(self._read_stderr(process))
        await asyncio.sleep(0.12)
        if process.returncode is not None:
            raise RuntimeError(f"Le lecteur audio s'est arrêté ({process.returncode}). {self.stderr.strip()[-800:]}")

    async def configure(self, output, current_master=None):
        """Validate before interrupting audio; restore the old route if the probe fails."""
        async with self.lock:
            selected, control = await self.normalize(output)
            route_keys = ('card', 'device', 'mode', 'pcm', 'control')
            if self.applied and self.process is not None and self.process.returncode is None and self.output is not None \
                    and all(self.output.get(key) == selected.get(key) for key in route_keys):
                if current_master is not None:
                    settings = checked_settings(current_master, control['gain_max'])
                    if settings != self.master:
                        self.master = await self._apply_master(settings, selected, control)
                self.output, self.control, self.error = selected, control, ''
                return dict(selected)
            if current_master is None and selected['mode'] == 'existing':
                settings = await self._read_master(selected, control)
            else:
                settings = current_master if current_master is not None else self.master
            settings = checked_settings(settings, control['gain_max'])
            if await self.external_player_active():
                raise RuntimeError('bluealsa-aplay.service possède déjà la sortie. Termine la migration du lecteur avec le script d’installation avant de choisir une sortie dans l’interface.')
            previous_output, previous_control, previous_master = self.output, self.control, dict(self.master)
            previous_config = self.config_path.read_text() if self.config_path.exists() else None
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_suffix('.tmp')
            temporary.write_text(alsa_config(selected))
            temporary.chmod(0o600)
            temporary.replace(self.config_path)
            stopped = False
            try:
                # A hardware PCM may allow only one opener. Release our own
                # player before the short test, then restore it on any failure.
                self.applied = False
                await self._stop()
                stopped = True
                pcm = selected['pcm'] if selected['mode'] == 'existing' else 'bureau_audio'
                await self.command([executable('aplay'), '-q', '-D', pcm, '-t', 'raw', '-f', 'S16_LE',
                                    '-c', '2', '-r', '48000'], input=b'\0' * 19200, env=self.environment())
                settings = await self._apply_master(settings, selected, control)
                await self._start(selected)
                self.output, self.control, self.master = selected, control, settings
                self.master_known = True
                self.applied, self.error = True, ''
                return dict(selected)
            except BaseException as error:
                if previous_config is None:
                    self.config_path.unlink(missing_ok=True)
                else:
                    self.config_path.write_text(previous_config)
                    self.config_path.chmod(0o600)
                if stopped:
                    await self._stop()
                    if previous_output:
                        try:
                            await self._apply_master(previous_master, previous_output, previous_control)
                            await self._start(previous_output)
                            self.applied = True
                        except Exception as rollback_error:
                            self.applied = False
                            self.error = f'Échec de la nouvelle sortie et de sa restauration : {rollback_error}'
                self.error = self.error or str(error)
                raise

    async def set_master(self, settings):
        async with self.lock:
            if self.output is None or self.control is None or not self.applied:
                raise RuntimeError('Configure une sortie audio disponible avant de régler le volume général.')
            combined = {**self.master, **settings}
            self.master = await self._apply_master(combined, self.output, self.control)
            return dict(self.master)

    async def get_master(self):
        if self.output is None or self.control is None:
            raise RuntimeError('Aucune sortie audio configurée.')
        # ALSA exposes the combined level, not separate volume and gain. After
        # applying settings, preserve these two requested components and retain
        # the level needed to restore a mute without a hardware switch.
        if self.master_known:
            return dict(self.master)
        self.master = await self._read_master(self.output, self.control)
        self.master_known = True
        return dict(self.master)

    async def _read_master(self, output, control):
        text = await self.command([executable('amixer'), '-c', output['card'], '--', 'sget', output['control']])
        levels = re.findall(r'\[(-?\d+(?:\.\d+)?)dB\]', text)
        percentages = re.findall(r'\[(\d+)%\]', text)
        switches = re.findall(r'\[(on|off)\]', text)
        if levels and control['has_db']:
            decibels = float(levels[0])
            return {'volume': round(min(100, 100 * 10 ** (decibels / 20))),
                    'gain': round(max(0, min(control['gain_max'], decibels)), 2),
                    'muted': bool(switches and all(value == 'off' for value in switches))}
        elif percentages:
            return {'volume': float(percentages[0]), 'gain': 0,
                    'muted': bool(switches and all(value == 'off' for value in switches))}
        else:
            raise RuntimeError('Le contrôle audio ne fournit pas de niveau lisible.')

    async def poll(self):
        if self.process is not None and self.process.returncode is not None:
            self.applied = False
            self.error = f"Le lecteur audio s'est arrêté ({self.process.returncode}). {self.stderr.strip()[-800:]}".strip()
        return self.snapshot()

    def snapshot(self):
        running = bool(self.process is not None and self.process.returncode is None)
        return {'output': dict(self.output) if self.output else None, 'output_choices': self.output_choices,
                'applied': self.applied and running, 'running': running,
                'message': self.error or ('Lecteur audio prêt' if self.applied and running else 'Choisis une sortie audio'),
                'gain_max': self.control['gain_max'] if self.control else 0,
                'master': dict(self.master)}

    async def close(self):
        async with self.lock:
            await self._stop()
            self.applied = False

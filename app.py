#!/usr/bin/env python3
"""Console locale pour BlueZ, BlueALSA et les sorties audio ALSA."""
import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from dbus_next import BusType, DBusError, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method
from audio import AudioEngine, SOURCE_GAIN_MAX

ROOT = Path(__file__).resolve().parent
PROPERTIES = 'org.freedesktop.DBus.Properties'
OBJECTS = 'org.freedesktop.DBus.ObjectManager'
DEVICE = 'org.bluez.Device1'
ADAPTER = 'org.bluez.Adapter1'
PCM = 'org.bluealsa.PCM1'
AGENT_PATH = '/fr/bureauaudio/agent'
LOG = logging.getLogger('bureau-audio')
VERSION = '3.0'


def plain(value):
    if isinstance(value, Variant):
        return plain(value.value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Valeur numérique attendue.')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'La valeur doit être comprise entre {low} et {high}.')
    return float(value)


def boolean(value):
    if not isinstance(value, bool):
        raise ValueError('Valeur vrai/faux attendue.')
    return value


def decode_volume(value):
    """Ancienne API uint16 ou API par octet/canal, détectées sur le flux réel."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65535:
        levels = [value >> 8, value & 255]
    elif isinstance(value, (bytes, bytearray, list, tuple)) and value:
        levels = list(value)
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in levels):
            raise ValueError('Volume BlueALSA invalide.')
    else:
        raise ValueError('API BlueALSA incompatible : volume uint16 ou tableau d’octets attendu.')
    return round(sum(v & 127 for v in levels) * 100 / (127 * len(levels))), all(v & 128 for v in levels)


def encode_volume(value, volume=None, muted=None):
    decode_volume(value)
    packed = isinstance(value, int)
    levels = [value >> 8, value & 255] if packed else list(value)
    if volume is not None:
        level = round(number(volume, 0, 100) * 127 / 100)
        levels = [(v & 128) | level for v in levels]
    if muted is not None:
        bit = 128 if boolean(muted) else 0
        levels = [(v & 127) | bit for v in levels]
    return (levels[0] << 8) | levels[1] if packed else bytes(levels)


def master_db(volume, gain):
    number(volume, 0, 100)
    number(gain, 0, 4)
    return max(-102.39, 20 * math.log10(max(volume, 0.00001) / 100) + gain)


def parse_mixer(output):
    dbs = re.findall(r'\[(-?\d+(?:\.\d+)?)dB\]', output)
    switches = re.findall(r'\[(on|off)\]', output)
    if not dbs:
        raise RuntimeError('Le contrôle PCM du jack ne fournit pas de niveau en dB.')
    db = float(dbs[0])
    return {'volume': round(min(100, 100 * 10 ** (db / 20))),
            'gain': round(max(0, min(4, db)), 1),
            'muted': bool(switches and switches[0] == 'off')}


class StateFile:
    def __init__(self, directory):
        self.path = Path(directory) / 'settings.json'
        self.data = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                raise ValueError('Fichier de préférences invalide : objet JSON attendu.')

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        tmp.chmod(0o600)
        if os.geteuid() == 0:
            owner = self.path.stat() if self.path.exists() else self.path.parent.stat()
            os.chown(tmp, owner.st_uid, owner.st_gid)
        tmp.replace(self.path)


A2DP_SOURCE = '0000110a-0000-1000-8000-00805f9b34fb'


def bt_address(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', value):
        raise ValueError('Adresse Bluetooth invalide.')
    return value.upper()


def routing_config(value):
    if not isinstance(value, dict) or not isinstance(value.get('adapters', []), list):
        raise ValueError('Configuration des récepteurs invalide.')
    if not isinstance(value.get('sources', []), list):
        raise ValueError('Liste des sources invalide.')
    adapters, sources = [], []
    for item in value.get('adapters', []):
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip()
                                             for k in ('label', 'short_label', 'alias')):
            raise ValueError('Nom ou alias du récepteur absent.')
        address = bt_address(item.get('address'))
        if address in {v['address'] for v in adapters}:
            raise ValueError('Récepteur en double.')
        adapters.append({**item, 'address': address})
    for item in value.get('sources', []):
        if not isinstance(item, dict):
            raise ValueError('Source invalide.')
        address, adapter = bt_address(item.get('address')), bt_address(item.get('adapter'))
        if adapter not in {v['address'] for v in adapters}:
            raise ValueError('Récepteur de la source inconnu.')
        if address in {v['address'] for v in sources}:
            raise ValueError('Source en double.')
        sources.append({**item, 'address': address, 'adapter': adapter})
    return {'adapters': adapters, 'sources': sources}


class SourceRegistry:
    """Les routes du fichier servent à la migration ; le registre devient la référence."""
    def init_registry(self, state, config):
        self.state, self.config = state, config
        self.specs = {v['address']: v for v in config['adapters']}
        for address, spec in state.data.get('adapter_registry', {}).items():
            address = bt_address(address)
            self.specs[address] = {**spec, 'address': address}
        migrated = 'source_registry' not in state.data
        seeds = {v['address']: v for v in config['sources']}
        if migrated:
            seeds = {**state.data.get('added_sources', {}), **seeds}
        else:
            seeds = state.data['source_registry']
        def load(items):
            result = {}
            for address, item in items.items():
                address = bt_address(address)
                adapter = bt_address(item.get('adapter'))
                if adapter not in self.specs:
                    self.specs[adapter] = {'address': adapter, 'label': 'Récepteur ' + adapter[-5:],
                                           'short_label': 'BT' + str(len(self.specs) + 1), 'alias': 'Bureau Audio ' + adapter[-5:]}
                result[address] = {'address': address, 'adapter': adapter, 'kind': item.get('kind', 'computer'),
                                   'gain': number(item.get('gain', 0), 0, SOURCE_GAIN_MAX)}
                # Les anciens libellés personnalisés ne sont jamais réutilisés comme nom Bluetooth.
                if isinstance(item.get('bluetooth_name'), str) and item['bluetooth_name'].strip():
                    result[address]['bluetooth_name'] = item['bluetooth_name']
            return result
        self.retired = load(state.data.get('retired_sources', {}))
        self.sources = {a: v for a, v in load(seeds).items() if a not in self.retired}
        self.routes = {a: v['adapter'] for a, v in self.sources.items()}
        if migrated:
            self.save_registry()

    def save_registry(self):
        self.state.data['source_registry'] = self.sources
        self.state.data['retired_sources'] = self.retired
        self.state.data['adapter_registry'] = self.specs
        self.state.save()

    def discover_adapters(self, discovered):
        changed = False
        for address, props in discovered.items():
            if address not in self.specs:
                alias = props.get('Alias') or props.get('Name') or 'Bureau Audio ' + str(len(self.specs) + 1)
                self.specs[address] = {'address': address, 'label': alias,
                                      'short_label': 'BT' + str(len(self.specs) + 1), 'alias': alias}
                changed = True
        if changed:
            self.save_registry()

    def source_name(self, address):
        spec = self.sources.get(address) or self.retired.get(address, {})
        return spec.get('bluetooth_name') or address

    def change_source(self, action, address, adapter=None):
        address = bt_address(address)
        if action not in ('assign', 'retire', 'restore'):
            raise ValueError('Action inconnue.')
        if action in ('assign', 'restore'):
            adapter = bt_address(adapter)
            if adapter not in self.specs:
                raise ValueError('Récepteur inconnu.')
        if action == 'restore':
            if address not in self.retired:
                raise ValueError('Appareil retiré inconnu.')
            self.sources[address] = {**self.retired.pop(address), 'adapter': adapter}
        elif address not in self.sources:
            raise ValueError('Appareil inconnu.')
        elif action == 'retire':
            self.retired[address] = self.sources.pop(address)
        else:
            self.sources[address]['adapter'] = adapter
        self.routes = {a: v['adapter'] for a, v in self.sources.items()}
        self.save_registry()


class PairingAgent(ServiceInterface):
    def __init__(self, backend):
        super().__init__('org.bluez.Agent1')
        self.backend = backend

    def authorize(self, device):
        adapter = self.backend.adapter_for_path(device)
        address = device.rsplit('/dev_', 1)[-1].replace('_', ':').upper()
        if address in self.backend.retired:
            raise DBusError('org.bluez.Error.Rejected', 'Rétablis cet appareil dans la console avant de l’utiliser.')
        required = self.backend.routes.get(address)
        if not adapter or (required and required != adapter):
            raise DBusError('org.bluez.Error.Rejected', 'Utilise le récepteur affecté à cet appareil.')
        known = self.backend.devices.get(device, {})
        opened = self.backend.pairing_active_for(adapter)
        if known.get('Blocked') or (not opened and not known.get('Trusted')):
            raise DBusError('org.bluez.Error.Rejected', 'Appairage fermé.')
        if opened:
            self.backend.authorized.add(device)

    @method()
    def Release(self):
        self.backend.agent_registered = False

    @method()
    def RequestPinCode(self, device: 'o') -> 's':
        raise DBusError('org.bluez.Error.Rejected', 'Appairage sans code uniquement.')

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':
        raise DBusError('org.bluez.Error.Rejected', 'Appairage sans code uniquement.')

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's'):
        self.authorize(device)

    @method()
    def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'):
        self.authorize(device)

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):
        self.authorize(device)

    @method()
    def RequestAuthorization(self, device: 'o'):
        self.authorize(device)

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        if uuid.lower() not in {A2DP_SOURCE, '0000110b-0000-1000-8000-00805f9b34fb',
                                '0000110d-0000-1000-8000-00805f9b34fb', '0000110c-0000-1000-8000-00805f9b34fb',
                                '0000110e-0000-1000-8000-00805f9b34fb', '0000110f-0000-1000-8000-00805f9b34fb'}:
            raise DBusError('org.bluez.Error.Rejected', 'Service audio uniquement.')
        self.authorize(device)

    @method()
    def Cancel(self):
        pass


class PiBackend(SourceRegistry):
    def __init__(self, state, routing=None):
        self.state, self.bus = state, None
        config_path = Path(os.environ.get('BUREAU_ROUTES', str(ROOT / 'routing.json')))
        self.init_registry(state, routing_config(routing if routing is not None else json.loads(config_path.read_text())))
        self.adapters, self.devices, self.pcms = {}, {}, {}
        self.audio_engine = AudioEngine(state)
        self.output_choices, self.output_diagnostics = [], []
        self.output = state.data.get('output')
        self.card = os.environ.get('BUREAU_CARD')
        if not self.output and self.card:
            self.output = {'card': self.card, 'control': os.environ.get('BUREAU_CONTROL', 'PCM'),
                           'pcm': os.environ.get('BUREAU_PCM', 'hub_mix'), 'mode': 'existing', 'label': 'Sortie audio'}
        self.master = {'volume': 75, 'gain': 0, 'muted': False}
        self.master_ready = False
        self.gain_max, self.last_audio_discovery = 0, 0
        self.deadlines = {}
        self.agent_registered, self.authorized = False, set()
        self.errors, self.retry_after, self.connecting = [], {}, set()
        self.lock = asyncio.Lock()

    def pairing_active_for(self, address):
        return time.monotonic() < self.deadlines.get(address, 0)

    @property
    def pairing_active(self):
        return any(self.pairing_active_for(a) for a in self.specs)

    def adapter_for_path(self, path):
        parent = path.split('/dev_', 1)[0]
        return next((a for a, props in self.adapters.items() if props['path'] == parent), None)

    async def call(self, destination, path, interface, member, signature='', body=None, timeout=8):
        if not self.bus:
            raise RuntimeError('Connexion Bluetooth indisponible.')
        try:
            reply = await asyncio.wait_for(self.bus.call(Message(
                destination=destination, path=path, interface=interface,
                member=member, signature=signature, body=body or [])), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError('La demande Bluetooth a expiré. Vérifie que l’appareil est éveillé et à portée.') from None
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f'{reply.error_name}: {reply.body[0] if reply.body else member}')
        return plain(reply.body)

    async def set_property(self, destination, path, interface, name, signature, value):
        await self.call(destination, path, PROPERTIES, 'Set', 'ssv', [interface, name, Variant(signature, value)])

    async def amixer(self, *arguments):
        proc = await asyncio.create_subprocess_exec('/usr/bin/amixer', '-c', self.card, '--', *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, 'LC_ALL': 'C'})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError('Le réglage du jack ne répond pas.') from None
        if proc.returncode:
            raise RuntimeError(err.decode(errors='replace').strip() or 'Réglage du jack impossible.')
        return out.decode(errors='replace')

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.export(AGENT_PATH, PairingAgent(self))
        with contextlib.suppress(Exception):
            await self.register_agent()
        await self.refresh()

    async def register_agent(self):
        manager = 'org.bluez.AgentManager1'
        try:
            await self.call('org.bluez', '/org/bluez', manager, 'RegisterAgent', 'os', [AGENT_PATH, 'NoInputNoOutput'])
        except RuntimeError as exc:
            if 'AlreadyExists' not in str(exc):
                raise
        await self.call('org.bluez', '/org/bluez', manager, 'RequestDefaultAgent', 'o', [AGENT_PATH])
        self.agent_registered = True

    async def close(self):
        for address in list(self.deadlines):
            with contextlib.suppress(Exception):
                await self.pair(False, address)
        if self.bus:
            self.bus.disconnect()
        await self.audio_engine.close()

    async def route_property(self, path, props, name, value):
        if bool(props.get(name)) == value:
            return
        adapter, address = self.adapter_for_path(path), bt_address(props['Address'])
        key = adapter + '/' + address
        records = self.state.data.setdefault('routing_originals', {})
        record = records.setdefault(key, {'adapter': adapter, 'device': address, 'original': {}, 'applied': {}})
        record['original'].setdefault(name, bool(props.get(name)))
        record['applied'][name] = value
        self.state.save()
        await self.set_property('org.bluez', path, DEVICE, name, 'b', value)
        props[name] = value

    async def prepare_adapter(self, address, props):
        if props.get('PowerState') == 'off-blocked':
            self.errors.append(self.specs[address]['label'] + ' : radio bloquée. Utilise « Activer » sur ce récepteur.')
            return
        key = 'adapter:' + address
        if time.monotonic() < self.retry_after.get(key, 0):
            self.errors.append(self.specs[address]['label'] + ' : activation en attente.')
            return
        try:
            if not props.get('Powered'):
                await self.set_property('org.bluez', props['path'], ADAPTER, 'Powered', 'b', True)
                props['Powered'] = True
            alias = self.specs[address]['alias']
            if props.get('Alias') != alias:
                await self.set_property('org.bluez', props['path'], ADAPTER, 'Alias', 's', alias)
                props['Alias'] = alias
            if not self.pairing_active_for(address):
                for name in ('Discoverable', 'Pairable'):
                    if props.get(name):
                        await self.set_property('org.bluez', props['path'], ADAPTER, name, 'b', False)
                        props[name] = False
            self.retry_after.pop(key, None)
        except Exception as exc:
            self.retry_after[key] = time.monotonic() + 15
            self.errors.append(self.specs[address]['label'] + ' : ' + str(exc))

    async def unblock_radio(self):
        manager, unit = 'org.freedesktop.systemd1.Manager', 'bureau-audio-radio.service'
        job = (await self.call('org.freedesktop.systemd1', '/org/freedesktop/systemd1', manager,
                               'RestartUnit', 'ss', [unit, 'replace']))[0]
        for attempt in range(30):
            await asyncio.sleep(0.1)
            try:
                await self.call('org.freedesktop.systemd1', job, PROPERTIES, 'Get', 'ss',
                                ['org.freedesktop.systemd1.Job', 'State'])
            except RuntimeError as exc:
                if 'UnknownObject' not in str(exc) and 'UnknownMethod' not in str(exc) and 'NoSuchJob' not in str(exc):
                    raise
                break
        else:
            raise RuntimeError('Le déblocage Bluetooth n’a pas terminé. Réessaie dans quelques secondes.')
        path = (await self.call('org.freedesktop.systemd1', '/org/freedesktop/systemd1', manager,
                                'GetUnit', 's', [unit]))[0]
        props = (await self.call('org.freedesktop.systemd1', path, PROPERTIES, 'GetAll', 's',
                                 ['org.freedesktop.systemd1.Service']))[0]
        if props.get('Result') != 'success' or props.get('ExecMainStatus') != 0:
            raise RuntimeError('Le service de déblocage Bluetooth a échoué.')

    async def activate_radio(self, address):
        address = bt_address(address)
        if address not in self.specs or address not in self.adapters:
            raise ValueError('Récepteur absent ou inconnu.')
        await self.unblock_radio()
        async with self.lock:
            adapter = self.adapters[address]
            await self.set_property('org.bluez', adapter['path'], ADAPTER, 'Powered', 'b', True)
            self.retry_after.pop('adapter:' + address, None)
        await self.refresh()

    async def enforce_routes(self):
        # Les nouveaux appareils héritent du récepteur sur lequel ils sont appairés.
        changed = False
        for path, props in sorted(self.devices.items(), key=lambda item: not item[1].get('Connected', False)):
            if not (props.get('Paired') or props.get('Bonded')):
                continue
            address, adapter = bt_address(props['Address']), self.adapter_for_path(path)
            supports_source = A2DP_SOURCE in [v.lower() for v in props.get('UUIDs', [])]
            supports_source = supports_source or any(v.get('Device') == path for v in self.pcms.values())
            if address not in self.routes and not supports_source:
                continue
            if address not in self.routes and address not in self.retired:
                item = {'address': address, 'adapter': adapter,
                        'kind': 'phone' if props.get('Icon') == 'phone' else 'computer'}
                self.sources[address], self.routes[address] = item, adapter
                changed = True
        for address, spec in {**self.sources, **self.retired}.items():
            candidates = [(p, v) for p, v in self.devices.items() if v.get('Address', '').upper() == address and v.get('Name')]
            candidates.sort(key=lambda pv: (self.adapter_for_path(pv[0]) != spec['adapter'], not pv[1].get('Connected')))
            if candidates and spec.get('bluetooth_name') != candidates[0][1]['Name']:
                spec['bluetooth_name'] = candidates[0][1]['Name']
                changed = True
        if changed:
            self.save_registry()
        for path, props in self.devices.items():
            address = bt_address(props['Address'])
            required = self.routes.get(address)
            if not required and address not in self.retired:
                continue
            try:
                if address in self.retired or self.adapter_for_path(path) != required:
                    await self.route_property(path, props, 'Blocked', True)
                    if props.get('Connected') and time.monotonic() >= self.retry_after.get(path, 0):
                        self.retry_after[path] = time.monotonic() + 15
                        try:
                            await self.call('org.bluez', path, DEVICE, 'Disconnect')
                        except RuntimeError as exc:
                            if 'NotConnected' not in str(exc):
                                raise
                        props['Connected'] = False
                else:
                    await self.route_property(path, props, 'Blocked', False)
                    if props.get('Paired'):
                        await self.route_property(path, props, 'Trusted', True)
                        self.authorized.discard(path)
            except Exception as exc:
                self.errors.append(self.source_name(address) + ' : affectation non appliquée : ' + str(exc))

    async def source_action(self, address, action, adapter=None):
        address = bt_address(address)
        async with self.lock:
            if address in self.connecting:
                raise RuntimeError('Attends la fin de la demande de connexion avant de modifier cet appareil.')
            self.change_source(action, address, adapter)
            self.authorized = {p for p in self.authorized if not p.endswith('/dev_' + address.replace(':', '_'))}
            # Réessayer immédiatement après un choix explicite de récepteur.
            for p in self.devices:
                if p.endswith('/dev_' + address.replace(':', '_')):
                    self.retry_after.pop(p, None)
            await self.enforce_routes()
        await self.refresh()

    async def refresh(self):
        async with self.lock:
            self.errors = []
            try:
                objects = (await self.call('org.bluez', '/', OBJECTS, 'GetManagedObjects'))[0]
                self.adapters = {bt_address(v[ADAPTER]['Address']): {**v[ADAPTER], 'path': p}
                                 for p, v in objects.items() if ADAPTER in v}
                self.discover_adapters(self.adapters)
                parents = {v['path'] for v in self.adapters.values()}
                self.devices = {p: v[DEVICE] for p, v in objects.items()
                                if DEVICE in v and p.split('/dev_', 1)[0] in parents}
                for address, spec in self.specs.items():
                    props = self.adapters.get(address)
                    if props:
                        await self.prepare_adapter(address, props)
                    else:
                        self.errors.append(spec['label'] + ' : récepteur absent.')
                await self.enforce_routes()
                for address, deadline in list(self.deadlines.items()):
                    if deadline and not self.pairing_active_for(address):
                        await self.pair(False, address, locked=True)
            except Exception as exc:
                self.adapters, self.devices = {}, {}
                self.agent_registered = False
                self.errors.append('Bluetooth : ' + str(exc))
            try:
                objects = (await self.call('org.bluealsa', '/org/bluealsa', OBJECTS, 'GetManagedObjects'))[0]
                self.pcms = {p: v[PCM] for p, v in objects.items() if PCM in v
                             and v[PCM].get('Transport') == 'A2DP-sink' and v[PCM].get('Mode') == 'source'}
                for props in self.pcms.values():
                    decode_volume(props.get('Volume'))
            except Exception as exc:
                self.pcms = {}
                self.errors.append('Audio Bluetooth : ' + str(exc))
            await self.refresh_audio()

    async def refresh_audio(self):
        self.output_diagnostics = []
        try:
            await self.audio_engine.configure_inputs({a: v.get('gain', 0) for a, v in self.sources.items()})
            if not self.last_audio_discovery or time.monotonic() - self.last_audio_discovery > 15:
                self.output_choices = await self.audio_engine.discover()
                self.last_audio_discovery = time.monotonic()
            # L’ancienne route personnalisée reste disponible pour la migration.
            if self.output and self.output.get('mode') == 'existing':
                found = next((v for v in self.output_choices if v['card'] == self.output['card']), None)
                if found and not any(v['card'] == self.output['card'] and v['pcm'] == self.output['pcm'] for v in self.output_choices):
                    self.output_choices.append({**found, 'pcm': self.output['pcm'], 'mode': 'existing',
                                                'label': 'Route audio existante · ' + self.output['pcm'],
                                                'controls': [v for v in found['controls'] if not v.get('software')]})
            if self.output and not self.master_ready and time.monotonic() >= self.retry_after.get('audio', 0):
                self.validate_output(self.output)
                settings = self.state.data.get('master')
                if settings is None and self.output.get('mode') != 'existing':
                    settings = self.master
                self.output = await self.audio_engine.configure(self.output, settings)
                self.master = await self.audio_engine.get_master()
                self.gain_max = self.output.get('gain_max', 0)
                self.master_ready = True
                self.retry_after.pop('audio', None)
                self.state.data['output'] = self.output
                self.state.save()
            await self.audio_engine.poll()
            if self.output and not self.audio_engine.snapshot().get('applied'):
                if self.master_ready:
                    self.retry_after['audio'] = time.monotonic() + 15
                self.master_ready = False
                self.output_diagnostics.append(self.audio_engine.snapshot().get('message') or 'Lecteur audio indisponible.')
        except Exception as exc:
            self.master_ready = False
            self.retry_after['audio'] = time.monotonic() + 15
            self.output_diagnostics.append(str(exc))
        if not self.output_choices:
            self.output_diagnostics.append('Aucune sortie ALSA détectée. Branche une carte son ou active une sortie audio.')
        if not self.adapters:
            self.output_diagnostics.append('Aucun récepteur Bluetooth détecté.')
        self.output_diagnostics.extend(self.audio_engine.diagnostics(self.output))
        self.errors.extend('Sortie audio : ' + v for v in self.output_diagnostics if self.output)

    def validate_output(self, output):
        if not isinstance(output, dict):
            raise ValueError('Configuration de sortie attendue.')
        choice = next((v for v in self.output_choices if str(v['card']) == str(output.get('card')) and v['pcm'] == output.get('pcm')), None)
        if not choice:
            raise ValueError('Cette sortie audio est absente. Sélectionne une sortie détectée.')
        mode = output.get('mode', 'managed')
        if mode != choice.get('mode', 'managed'):
            raise ValueError('Mode de sortie invalide.')
        control = next((v for v in choice['controls'] if v['name'] == output.get('control')), None)
        if not control:
            raise ValueError('Contrôle de volume inconnu pour cette sortie.')
        label = output.get('label') or choice['name']
        if not isinstance(label, str) or len(label) > 100 or any(ord(c) < 32 for c in label):
            raise ValueError('Nom de sortie invalide (100 caractères maximum).')
        return {**output, 'label': label, 'card': str(choice['card']), 'device': choice.get('device', 0), 'mode': mode}

    async def configuration_action(self, data):
        async with self.lock:
            if set(data) == {'adapter'}:
                item = data['adapter']
                if not isinstance(item, dict) or set(item) != {'address', 'alias'}:
                    raise ValueError('Adresse et nom de récepteur attendus.')
                address, alias = bt_address(item['address']), item['alias']
                if address not in self.specs or not isinstance(alias, str) or not alias.strip() or len(alias.encode()) > 248 or len(alias) > 64 or any(ord(c) < 32 for c in alias):
                    raise ValueError('Nom de récepteur invalide (64 caractères maximum).')
                if address in self.adapters:
                    await self.set_property('org.bluez', self.adapters[address]['path'], ADAPTER, 'Alias', 's', alias.strip())
                self.specs[address]['alias'] = alias.strip()
                self.save_registry()
                return
            if set(data) != {'output'} or not isinstance(data['output'], dict) or set(data['output']) - {'card', 'control', 'pcm', 'label', 'mode'}:
                raise ValueError('Configuration de sortie invalide.')
            self.output_choices = await self.audio_engine.discover()
            self.last_audio_discovery = 0
            if self.output and self.output.get('mode') == 'existing':
                found = next((v for v in self.output_choices if v['card'] == self.output['card']), None)
                if found and not any(v['card'] == self.output['card'] and v['pcm'] == self.output['pcm'] for v in self.output_choices):
                    self.output_choices.append({**found, 'pcm': self.output['pcm'], 'mode': 'existing',
                                                'label': 'Route existante', 'controls': [v for v in found['controls'] if not v.get('software')]})
            requested = self.validate_output(data['output'])
            control = next(v for c in self.output_choices if c['card'] == requested['card'] and c['pcm'] == requested['pcm']
                           for v in c['controls'] if v['name'] == requested['control'])
            settings = {**self.master, 'gain': min(self.master['gain'], control.get('gain_max', 0))}
            await self.audio_engine.configure_inputs({a: v.get('gain', 0) for a, v in self.sources.items()})
            result = await self.audio_engine.configure(requested, settings)
            self.output, self.master = result, await self.audio_engine.get_master()
            self.gain_max = result.get('gain_max', 0)
            self.master_ready = True
            self.state.data['output'], self.state.data['master'] = self.output, self.master
            self.state.save()
            self.retry_after.pop('audio', None)
        await self.refresh()

    async def apply_master(self, settings):
        number(settings['volume'], 0, 100)
        number(settings['gain'], 0, self.gain_max)
        boolean(settings['muted'])
        await self.audio_engine.set_master(settings)

    async def set_master(self, changes):
        async with self.lock:
            if not self.master_ready:
                raise RuntimeError('La sortie audio est indisponible. Vérifie sa configuration.')
            current = {**self.master, **changes}
            await self.apply_master(current)
            self.master = current
            self.state.data['master'] = current
            self.state.save()

    def desired_path(self, address):
        address = bt_address(address)
        if address not in self.routes:
            raise ValueError('Appareil inconnu.')
        adapter = self.adapters.get(self.routes[address])
        if not adapter:
            raise RuntimeError('Le récepteur affecté à cet appareil est absent.')
        return adapter['path'] + '/dev_' + address.replace(':', '_')

    def device_path(self, address):
        path = self.desired_path(address)
        if path not in self.devices or not self.devices[path].get('Paired'):
            spec = self.specs[self.routes[bt_address(address)]]
            raise ValueError('Appaire cet appareil avec ' + spec['alias'] + ' depuis ses réglages Bluetooth.')
        return path

    async def set_device(self, address, changes):
        address = bt_address(address)
        async with self.lock:
            if address not in self.sources:
                raise ValueError('Appareil inconnu.')
            matching = []
            if set(changes) & {'volume', 'muted'}:
                path = self.device_path(address)
                matching = [(p, props) for p, props in self.pcms.items() if props.get('Device') == path]
                if not matching:
                    raise ValueError('Flux audio indisponible. Lance une lecture puis réessaie.')
            if 'gain' in changes:
                gain = number(changes['gain'], 0, SOURCE_GAIN_MAX)
                await self.audio_engine.set_source_gain(address, gain)
                self.sources[address]['gain'] = gain
                self.save_registry()
            for pcm_path, props in matching:
                packed = encode_volume(props['Volume'], changes.get('volume'), changes.get('muted'))
                await self.set_property('org.bluealsa', pcm_path, PCM, 'SoftVolume', 'b', True)
                await self.set_property('org.bluealsa', pcm_path, PCM, 'Volume', 'q' if isinstance(packed, int) else 'ay', packed)
                props['Volume'], props['SoftVolume'] = packed, True

    async def device_action(self, address, action):
        address = bt_address(address)
        if action not in ('connect', 'disconnect', 'trust'):
            raise ValueError('Action inconnue.')
        async with self.lock:
            if address in self.connecting:
                raise RuntimeError('Une demande de connexion est déjà en cours pour cet appareil.')
            path = self.device_path(address)
            props = self.devices[path]
            await self.route_property(path, props, 'Blocked', False)
            if action in ('trust', 'connect'):
                await self.route_property(path, props, 'Trusted', True)
            if action == 'trust':
                return
            self.connecting.add(address)
        # Une connexion longue ne verrouille pas les volumes des autres sources.
        try:
            if action == 'connect':
                await self.call('org.bluez', path, DEVICE, 'ConnectProfile', 's', [A2DP_SOURCE], timeout=30)
            else:
                await self.call('org.bluez', path, DEVICE, 'Disconnect', timeout=15)
        finally:
            self.connecting.discard(address)
        await self.refresh()

    async def pair(self, enabled, address, locked=False):
        address = bt_address(address)
        if address not in self.specs:
            raise ValueError('Récepteur inconnu.')
        if not locked:
            async with self.lock:
                return await self.pair(enabled, address, locked=True)
        adapter = self.adapters.get(address)
        if not enabled:
            self.deadlines.pop(address, None)
            self.authorized = {p for p in self.authorized if self.adapter_for_path(p) != address}
            if adapter:
                for prop in ('Discoverable', 'Pairable'):
                    await self.set_property('org.bluez', adapter['path'], ADAPTER, prop, 'b', False)
                    adapter[prop] = False
            return
        if not adapter:
            raise RuntimeError('Ce récepteur est absent.')
        if adapter.get('PowerState') == 'off-blocked':
            raise RuntimeError('Radio bloquée. Utilise « Activer » sur ce récepteur.')
        await self.register_agent()
        self.authorized = {p for p in self.authorized if self.adapter_for_path(p) != address}
        self.deadlines[address] = time.monotonic() + 180
        try:
            for name, signature, value in (('Powered', 'b', True), ('DiscoverableTimeout', 'u', 180),
                                           ('PairableTimeout', 'u', 180), ('Pairable', 'b', True), ('Discoverable', 'b', True)):
                await self.set_property('org.bluez', adapter['path'], ADAPTER, name, signature, value)
                adapter[name] = value
        except Exception:
            self.deadlines.pop(address, None)
            for prop in ('Discoverable', 'Pairable'):
                with contextlib.suppress(Exception):
                    await self.set_property('org.bluez', adapter['path'], ADAPTER, prop, 'b', False)
            raise

    async def restore_routing(self):
        objects = (await self.call('org.bluez', '/', OBJECTS, 'GetManagedObjects'))[0]
        adapters = {bt_address(v[ADAPTER]['Address']): p for p, v in objects.items() if ADAPTER in v}
        records = self.state.data.get('routing_originals', {})
        for key, record in list(records.items()):
            parent = adapters.get(record['adapter'])
            if not parent:
                continue
            path = parent + '/dev_' + record['device'].replace(':', '_')
            props = objects.get(path, {}).get(DEVICE)
            if props is not None:
                for name, original in record['original'].items():
                    if props.get(name) == record['applied'].get(name):
                        await self.set_property('org.bluez', path, DEVICE, name, 'b', original)
            del records[key]
        self.state.save()
        if records:
            raise RuntimeError('Certains récepteurs sont absents. Rebranche-les pour restaurer leurs réglages.')

    def snapshot(self):
        devices, adapters = [], []
        for address, spec in self.specs.items():
            props = self.adapters.get(address, {})
            adapters.append({**spec, 'present': bool(props), 'powered': bool(props.get('Powered')),
                             'power_state': props.get('PowerState'), 'path': props.get('path'),
                             'pairing_seconds': max(0, math.ceil(self.deadlines.get(address, 0) - time.monotonic()))})
        for address, spec in self.sources.items():
            receiver = self.adapters.get(spec['adapter'])
            path = receiver['path'] + '/dev_' + address.replace(':', '_') if receiver else None
            props = self.devices.get(path, {})
            stream = next((v for v in self.pcms.values() if path and v.get('Device') == path), None)
            volume, muted = decode_volume(stream['Volume']) if stream else (None, False)
            devices.append({**spec, 'name': self.source_name(address),
                            'receiver': self.specs[spec['adapter']]['short_label'],
                            'remote_name': self.source_name(address),
                            'receiver_present': bool(receiver), 'connected': bool(props.get('Connected')),
                            'paired': bool(props.get('Paired')), 'trusted': bool(props.get('Trusted')),
                            'connecting': address in self.connecting, 'volume': volume, 'muted': muted,
                            'gain': spec.get('gain', 0), 'gain_max': SOURCE_GAIN_MAX if self.output and self.output.get('mode') == 'managed' else 0,
                            'gain_ready': self.master_ready and self.audio_engine.source_gain_ready(address),
                            'playing': bool(stream and stream.get('Running')), 'codec': stream.get('Codec') if stream else None,
                            'channels': stream.get('Channels') if stream else None,
                            'sampling': stream.get('Rate', stream.get('Sampling')) if stream else None})
        retired = [{**spec, 'name': self.source_name(address),
                    'connected': any(p.get('Connected') for p in self.devices.values() if p.get('Address', '').upper() == address)}
                   for address, spec in self.retired.items()]
        return {'demo': False, 'version': VERSION, 'bluetooth': any(v['powered'] for v in adapters),
                'adapters': adapters, 'master': dict(self.master), 'master_ready': self.master_ready,
                'gain_max': self.gain_max, 'devices': devices, 'retired_devices': retired, 'errors': list(self.errors),
                'configuration': {'output': self.output, 'output_choices': self.output_choices,
                                  'configured': bool(self.output and self.master_ready),
                                  'audio_status': self.audio_engine.snapshot(), 'diagnostics': self.output_diagnostics}}


class DemoBackend(SourceRegistry):
    """Simulation explicite, sans accès au Bluetooth ni au son de l'ordinateur."""
    def __init__(self, state):
        self.master = state.data.get('master', {'volume': 78, 'gain': 1.2, 'muted': False})
        config = demo_config()
        self.init_registry(state, config)
        self.adapters = [{**v, 'present': True, 'powered': True, 'pairing_seconds': 0} for v in self.specs.values()]
        self.deadlines = {}
        names = ['Smartphone', 'Ordinateur du bureau', 'Ordinateur portable avec un nom très long']
        for i, item in enumerate(config['sources']):
            spec = self.sources.get(item['address']) or self.retired.get(item['address'])
            if spec:
                spec['bluetooth_name'] = names[i % len(names)]
        for i in range(int(os.environ.get('BUREAU_DEMO_EXTRA', '0'))):
            address = f'02:00:00:00:00:{i + 1:02X}'
            if address not in self.sources and address not in self.retired:
                self.sources[address] = {'address': address, 'adapter': self.adapters[i % len(self.adapters)]['address'],
                                         'bluetooth_name': f'Tablette {i + 1}', 'kind': 'computer'}
        self.routes = {a: v['adapter'] for a, v in self.sources.items()}
        self.save_registry()
        self.devices = [{**v, 'receiver': next(a['short_label'] for a in self.adapters if a['address'] == v['adapter']),
                         'name': self.source_name(v['address']), 'remote_name': self.source_name(v['address']),
                         'receiver_present': True, 'paired': True, 'trusted': True,
                         'connected': True, 'connecting': False, 'playing': i != 1, 'muted': False,
                         'volume': [65, 72, 80][i % 3], 'codec': 'SBC', 'channels': 2, 'sampling': 44100}
                        for i, v in enumerate(self.sources.values())]
        self.output_choices = [
            {'card': 'DemoAudio', 'name': 'Sortie audio simulée', 'device': 0, 'pcm': 'plughw:CARD=DemoAudio,DEV=0', 'mode': 'managed',
             'controls': [{'name': 'Bureau Audio Master', 'gain_max': 4, 'has_switch': False, 'has_db': True, 'software': True},
                          {'name': 'PCM', 'gain_max': 4, 'has_switch': True, 'has_db': True}]},
            {'card': 'DemoUSB', 'name': 'Carte USB simulée', 'device': 0, 'pcm': 'plughw:CARD=DemoUSB,DEV=0', 'mode': 'managed',
             'controls': [{'name': 'Bureau Audio Master', 'gain_max': 4, 'has_switch': False, 'has_db': True, 'software': True},
                          {'name': 'Speaker', 'gain_max': 0, 'has_switch': True, 'has_db': True}]},
            {'card': 'DemoHDMI', 'name': 'HDMI simulé', 'device': 0, 'pcm': 'plughw:CARD=DemoHDMI,DEV=0', 'mode': 'managed',
             'controls': [{'name': 'Bureau Audio Master', 'gain_max': 4, 'has_switch': False, 'has_db': True, 'software': True}]}]
        self.output = state.data.get('output') or {k: self.output_choices[0][k] for k in ('card', 'pcm', 'mode', 'device')}
        self.output = {**self.output, 'control': self.output.get('control', 'Bureau Audio Master'), 'label': self.output.get('label', 'Sortie audio simulée')}
        self.master_ready = os.environ.get('BUREAU_DEMO_SETUP') != '1' or bool(state.data.get('output'))
        choice = next(v for v in self.output_choices if v['card'] == self.output['card'] and v['pcm'] == self.output['pcm'])
        self.gain_max = next(v['gain_max'] for v in choice['controls'] if v['name'] == self.output['control'])
        self.master = {**self.master, 'gain': min(self.master['gain'], self.gain_max)}
        self.output['gain_max'] = self.gain_max

    async def start(self): pass
    async def close(self): pass
    async def refresh(self): pass

    async def set_master(self, changes):
        if not self.master_ready: raise RuntimeError('Configure d’abord la sortie audio.')
        current = {**self.master, **changes}
        number(current['gain'], 0, self.gain_max)
        self.master = current
        self.state.data['master'] = self.master
        self.state.save()

    async def configuration_action(self, data):
        if set(data) == {'adapter'}:
            item = data['adapter']
            if not isinstance(item, dict) or set(item) != {'address', 'alias'}:
                raise ValueError('Adresse et nom du récepteur attendus.')
            address, alias = bt_address(item['address']), item['alias']
            if address not in self.specs or not isinstance(alias, str) or not alias.strip() or len(alias.encode()) > 248 or len(alias) > 64 or any(ord(c) < 32 for c in alias):
                raise ValueError('Nom de récepteur invalide.')
            self.specs[address]['alias'] = alias.strip()
            next(v for v in self.adapters if v['address'] == address)['alias'] = alias.strip()
            self.save_registry()
            return
        if set(data) != {'output'} or not isinstance(data['output'], dict) or set(data['output']) - {'card', 'control', 'pcm', 'label', 'mode'}:
            raise ValueError('Configuration de sortie invalide.')
        output = PiBackend.validate_output(self, data['output'])
        choice = next(v for v in self.output_choices if v['card'] == output['card'] and v['pcm'] == output['pcm'])
        control = next(v for v in choice['controls'] if v['name'] == output['control'])
        self.gain_max = control['gain_max']
        self.master = {**self.master, 'gain': min(self.master['gain'], self.gain_max)}
        self.output = {**output, 'gain_max': self.gain_max}
        self.master_ready = True
        self.state.data.update(output=self.output, master=self.master)
        self.state.save()

    def find(self, address):
        return next((v for v in self.devices if v['address'] == bt_address(address)), None)

    async def set_device(self, address, changes):
        device = self.find(address)
        if not device or (device['volume'] is None and set(changes) & {'volume', 'muted'}):
            raise ValueError('Flux audio indisponible.')
        if 'gain' in changes:
            if not self.master_ready:
                raise RuntimeError('Configure d’abord la sortie audio.')
            gain = number(changes['gain'], 0, SOURCE_GAIN_MAX)
            self.sources[device['address']]['gain'] = gain
            self.save_registry()
        device.update(changes)

    async def device_action(self, address, action):
        device = self.find(address)
        if not device: raise ValueError('Appareil inconnu.')
        if action == 'trust':
            device['trusted'] = True
            return
        device['connected'] = action == 'connect'
        device['volume'] = 70 if device['connected'] else None
        device['codec'], device['channels'], device['sampling'] = ('SBC', 2, 44100) if device['connected'] else (None, None, None)
        device['playing'] = False

    async def pair(self, enabled, address):
        address = bt_address(address)
        if address not in {v['address'] for v in self.adapters}: raise ValueError('Récepteur inconnu.')
        self.deadlines[address] = time.monotonic() + 180 if enabled else 0

    async def activate_radio(self, address):
        adapter = next((v for v in self.adapters if v['address'] == bt_address(address)), None)
        if not adapter: raise ValueError('Récepteur inconnu.')
        adapter['powered'] = True

    async def source_action(self, address, action, adapter=None):
        address = bt_address(address)
        self.change_source(action, address, adapter)
        device = self.find(address)
        if action == 'retire':
            self.devices.remove(device)
        elif action == 'restore':
            spec = self.sources[address]
            self.devices.append({**spec, 'name': self.source_name(address), 'remote_name': self.source_name(address),
                                 'receiver': self.specs[spec['adapter']]['short_label'], 'receiver_present': True,
                                 'paired': True, 'trusted': True, 'connected': False, 'connecting': False,
                                 'playing': False, 'volume': None, 'muted': False, 'codec': None,
                                 'channels': None, 'sampling': None})
        else:
            device['adapter'], device['receiver'] = adapter, self.specs[adapter]['short_label']
            device.update(connected=False, playing=False, volume=None, codec=None, channels=None, sampling=None)

    def snapshot(self):
        return {'demo': True, 'version': VERSION, 'bluetooth': True,
                'adapters': [{**v, 'pairing_seconds': max(0, math.ceil(self.deadlines.get(v['address'], 0) - time.monotonic()))}
                             for v in self.adapters], 'master': dict(self.master), 'master_ready': self.master_ready,
                'gain_max': self.gain_max, 'devices': [{**v, 'gain': v.get('gain', 0), 'gain_max': SOURCE_GAIN_MAX,
                                                      'gain_ready': self.master_ready} for v in self.devices],
                'retired_devices': [{**v, 'name': self.source_name(a), 'connected': False} for a, v in self.retired.items()], 'errors': [],
                'configuration': {'output': self.output, 'output_choices': self.output_choices, 'configured': self.master_ready,
                                  'diagnostics': [], 'audio_status': {'applied': self.master_ready,
                                  'message': 'Sortie simulée prête' if self.master_ready else 'Choisis une sortie audio.'}}}

    def levels(self):
        # Explicit demo waveform; production reads only measured PCM samples.
        t = time.monotonic()
        levels = [0.0, 0.0]
        if self.master_ready and not self.master['muted']:
            for i, device in enumerate(self.devices):
                if device['playing'] and device['connected'] and not device['muted']:
                    amplitude = (device['volume'] or 0) / 100 * 10 ** (device.get('gain', 0) / 20)
                    for channel in (0, 1):
                        levels[channel] += amplitude * (.15 + .28 * (1 + math.sin(t * (3.1 + i * .7) + channel * .8 + i)) / 2)
            levels = [v * self.master['volume'] / 100 * 10 ** (self.master['gain'] / 20) for v in levels]
        return {'available': self.master_ready, 'demo': True, 'left': min(1, levels[0]), 'right': min(1, levels[1]),
                'clip_left': levels[0] >= 1, 'clip_right': levels[1] >= 1, 'message': 'Niveaux simulés'}


def demo_config():
    """Adresses synthétiques locales, distinctes des installations réelles."""
    first, second = '02:BA:00:00:00:01', '02:BA:00:00:00:02'
    return {'adapters': [
                {'address': first, 'label': 'Récepteur Bluetooth 1', 'short_label': 'BT1', 'alias': 'Bureau Audio 1'},
                {'address': second, 'label': 'Récepteur Bluetooth 2', 'short_label': 'BT2', 'alias': 'Bureau Audio 2'}],
            'sources': [{'address': '02:BA:10:00:00:01', 'adapter': first, 'kind': 'phone'},
                        {'address': '02:BA:10:00:00:02', 'adapter': first, 'kind': 'computer'},
                        {'address': '02:BA:10:00:00:03', 'adapter': second, 'kind': 'computer'}]}


def create_app(backend, access_key):
    sessions, login_attempts = {}, {}
    @web.middleware
    async def protection(request, handler):
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('Origin')
            if not origin or urlsplit(origin).netloc != request.host or urlsplit(origin).scheme != request.scheme:
                raise web.HTTPForbidden(text='Origine de la requête refusée.')
            if request.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise web.HTTPBadRequest(text='Requête JSON attendue.')
        if request.path.startswith('/api/') and request.path != '/api/login':
            session = sessions.get(request.cookies.get('bureau_session', ''))
            if not session or session < time.monotonic():
                raise web.HTTPUnauthorized(text='Connexion requise.')
        try:
            result = await handler(request)
        except (ValueError, json.JSONDecodeError) as exc:
            result = web.json_response({'error': str(exc)}, status=400)
        except (RuntimeError, DBusError) as exc:
            result = web.json_response({'error': str(exc)}, status=503)
        except web.HTTPException:
            raise
        except Exception:
            LOG.exception('Erreur pendant une requête')
            result = web.json_response({'error': 'Une erreur est survenue. Réessaie dans quelques secondes.'}, status=500)
        result.headers['Cache-Control'] = 'no-store'
        result.headers['X-Content-Type-Options'] = 'nosniff'
        result.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return result

    app = web.Application(middlewares=[protection], client_max_size=4096)

    async def login(request):
        address = request.remote or 'local'
        now = time.monotonic()
        attempts = [t for t in login_attempts.get(address, []) if t > now - 60]
        if len(attempts) >= 10:
            raise web.HTTPTooManyRequests(text='Réessaie dans une minute.')
        login_attempts[address] = attempts + [now]
        data = await request.json()
        provided = data.get('key', '') if isinstance(data, dict) else ''
        if not isinstance(provided, str) or not hmac.compare_digest(
                hashlib.sha256(provided.encode()).digest(), hashlib.sha256(access_key.encode()).digest()):
            raise web.HTTPUnauthorized(text='Code incorrect.')
        login_attempts.pop(address, None)
        for key, expiry in list(sessions.items()):
            if expiry < now:
                del sessions[key]
        token = secrets.token_urlsafe(32)
        sessions[token] = now + 30 * 86400
        response = web.json_response({'ok': True})
        response.set_cookie('bureau_session', token, httponly=True, samesite='Strict', max_age=30 * 86400)
        return response

    async def status(request):
        return web.json_response(backend.snapshot())

    async def levels(request):
        return web.json_response(backend.levels() if isinstance(backend, DemoBackend) else backend.audio_engine.levels())

    async def action(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Objet JSON attendu.')
        section = request.match_info['section']
        if section == 'master':
            if not data or set(data) - {'volume', 'gain', 'muted'}:
                raise ValueError('Réglage général inconnu.')
            if 'volume' in data:
                number(data['volume'], 0, 100)
            if 'gain' in data:
                number(data['gain'], 0, backend.snapshot()['gain_max'])
            if 'muted' in data:
                boolean(data['muted'])
            await backend.set_master(data)
        elif section == 'pairing':
            if set(data) != {'enabled', 'adapter'}:
                raise ValueError('Récepteur et état d’appairage attendus.')
            await backend.pair(boolean(data.get('enabled')), bt_address(data.get('adapter')))
        elif section == 'radio':
            if set(data) != {'adapter'}:
                raise ValueError('Récepteur attendu.')
            await backend.activate_radio(bt_address(data.get('adapter')))
        elif section == 'configuration':
            await backend.configuration_action(data)
        elif section == 'source':
            command = data.get('action')
            expected = {'action', 'address'} if command == 'retire' else {'action', 'address', 'adapter'}
            if command not in ('assign', 'retire', 'restore') or set(data) != expected:
                raise ValueError('Modification d’appareil invalide.')
            await backend.source_action(bt_address(data.get('address')), command,
                                        bt_address(data['adapter']) if 'adapter' in data else None)
        elif section == 'device':
            address, command = data.get('address'), data.get('action', 'volume')
            if not isinstance(address, str):
                raise ValueError('Adresse attendue.')
            if command in ('connect', 'disconnect', 'trust'):
                await backend.device_action(address, command)
            elif command == 'volume':
                if set(data) - {'address', 'action', 'volume', 'muted', 'gain'}:
                    raise ValueError('Réglage de source inconnu.')
                changes = {k: data[k] for k in ('volume', 'muted', 'gain') if k in data}
                if not changes:
                    raise ValueError('Réglage absent.')
                if 'volume' in changes:
                    number(changes['volume'], 0, 100)
                if 'gain' in changes:
                    number(changes['gain'], 0, SOURCE_GAIN_MAX)
                if 'muted' in changes:
                    boolean(changes['muted'])
                await backend.set_device(address, changes)
            else:
                raise ValueError('Action inconnue.')
        else:
            raise web.HTTPNotFound()
        return web.json_response({'ok': True, 'state': backend.snapshot()})

    async def index(request):
        return web.FileResponse(ROOT / 'static' / 'index.html')

    async def health(request):
        return web.json_response({'version': VERSION})

    app.router.add_get('/', index)
    app.router.add_get('/health', health)
    app.router.add_post('/api/login', login)
    app.router.add_get('/api/status', status)
    app.router.add_get('/api/levels', levels)
    app.router.add_post('/api/{section:master|pairing|device|radio|source|configuration}', action)
    app.router.add_static('/static/', ROOT / 'static', show_index=False)

    async def lifecycle(app):
        await backend.start()
        async def poll():
            while True:
                await asyncio.sleep(2)
                try:
                    await backend.refresh()
                except Exception:
                    LOG.exception('Actualisation impossible')
        task = asyncio.create_task(poll())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await backend.close()
    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--restore-routing', action='store_true', help='Restaurer les réglages Bluetooth antérieurs à la répartition.')
    parser.add_argument('--host', default=os.environ.get('BUREAU_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('BUREAU_PORT', '8088')))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.restore_routing:
        state = StateFile(os.environ.get('BUREAU_STATE', '/var/lib/bureau-audio'))
        engine = PiBackend(state)
        async def restore():
            engine.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                await engine.restore_routing()
            finally:
                engine.bus.disconnect()
        asyncio.run(restore())
        print('Réglages Bluetooth antérieurs restaurés pour les récepteurs présents.')
        raise SystemExit(0)
    key = os.environ.get('BUREAU_KEY', 'demo' if args.demo else '')
    if not key:
        parser.error('BUREAU_KEY doit contenir le code d’accès.')
    state = StateFile(os.environ.get('BUREAU_STATE', '/var/lib/bureau-audio' if not args.demo else str(ROOT / '.demo-state')))
    engine = DemoBackend(state) if args.demo else PiBackend(state)
    web.run_app(create_app(engine, key), host=args.host, port=args.port, access_log=None)

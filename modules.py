"""Authenticated studio actions shared by the hardware and demo backends."""
import asyncio
import copy

from audio import checked_settings
from equalizer import EqualizerPlan
from studio import BUSES, StudioPlan, input_defaults, label


class Modules:
    def __init__(self, backend, studio, demo=False):
        self.backend, self.studio, self.demo = backend, studio, demo

    def source_settings(self):
        settings = copy.deepcopy(self.studio.plan.settings)
        snapshot = self.backend.snapshot()
        for device in snapshot['devices']:
            sid = device['address']
            if sid not in settings['inputs']:
                item = input_defaults(device['name'])
                item.update(volume=device.get('volume') if device.get('volume') is not None else 100,
                            gain=device.get('gain', 0), muted=device.get('muted', False), solo=device.get('solo', False))
                settings['inputs'][sid] = item
            elif not settings['enabled']:
                settings['inputs'][sid].update({key: device[key] for key in ('volume', 'gain', 'muted', 'solo') if device.get(key) is not None})
        return settings

    async def commit(self, settings, eq=None, master=None):
        backend = self.backend
        plan = await asyncio.to_thread(StudioPlan, settings)
        if self.demo:
            was_live = self.studio.plan.settings['enabled']
            eq_plan = EqualizerPlan.create(eq) if eq is not None else backend.equalizer
            master_settings = checked_settings(master, backend.gain_max) if master is not None else backend.master
            previous = dict(backend.state.data)
            revision = self.studio.revision + 1
            backend.state.data.update(studio=plan.settings, studio_revision=revision)
            if eq is not None:
                backend.state.data.update(equalizer=eq_plan.settings, equalizer_revision=backend.equalizer_revision + 1)
            if master is not None:
                backend.state.data['master'] = master_settings
            try:
                backend.state.save()
            except BaseException:
                backend.state.data = previous
                raise
            self.studio.install(plan)
            if eq is not None:
                backend.equalizer, backend.equalizer_revision = eq_plan, backend.equalizer_revision + 1
            backend.master = master_settings
            if was_live and not plan.settings['enabled']:
                await backend.restore_scene_sources({sid: {key: item[key] if key != 'gain' else max(0, min(4, item[key]))
                    for key in ('volume', 'gain', 'muted', 'solo')} for sid, item in plan.settings['inputs'].items() if ':' in sid})
        else:
            was_live = self.studio.plan.settings['enabled']
            # The engine prepares raw PCM after stopping the legacy players.
            try:
                await backend.audio_engine.set_studio(plan.settings, eq, master)
            except BaseException:
                if not was_live:
                    await backend.prepare_live_volumes(False)
                raise
            if was_live and not plan.settings['enabled']:
                await backend.restore_scene_sources({sid: {key: item[key] for key in ('volume', 'gain', 'muted', 'solo')}
                    for sid, item in plan.settings['inputs'].items() if ':' in sid})
            if master is not None:
                backend.master = dict(backend.audio_engine.master)
                backend.master_ready = backend.audio_engine.snapshot()['applied']

    def snapshot(self):
        available = self.backend.master_ready if self.demo else bool(self.backend.audio_engine.equalizer_snapshot().get('available'))
        return self.studio.snapshot(available, self.demo)

    def device_overlay(self, address):
        settings = self.studio.plan.settings
        if not settings['enabled'] or address not in settings['inputs']:
            return {}
        item = settings['inputs'][address]
        return {key: item[key] for key in ('volume', 'gain', 'muted', 'solo')} | {
                'gain_max': 24, 'solo_suppressed': any(v['solo'] for v in settings['inputs'].values()) and not item['solo']}

    async def action(self, data):
        backend, studio = self.backend, self.studio
        if not isinstance(data, dict) or not isinstance(data.get('action'), str):
            raise ValueError('Action de module attendue.')
        action = data['action']
        schemas = {'configure': {'action', 'settings', 'revision'}, 'input': {'action', 'id', 'changes'},
                   'bus': {'action', 'bus', 'changes'}, 'record': {'action', 'enabled', 'bus', 'tap'},
                   'scene_save': {'action', 'name'}, 'scene_recall': {'action', 'name'},
                   'scene_delete': {'action', 'name'}, 'scene_import': {'action', 'name', 'scene'},
                   'feedback_lock': {'action', 'bus'}, 'refresh': {'action'}}
        if action not in schemas or set(data) != schemas[action]:
            raise ValueError('Action ou paramètres de module inconnus.')
        async with backend.lock:
            if action == 'refresh':
                if not self.demo:
                    await backend.audio_engine.discover_captures()
                return
            if action == 'record':
                if not isinstance(data['enabled'], bool):
                    raise ValueError('État d’enregistrement invalide.')
                if data['bus'] not in BUSES or data['tap'] not in ('pre', 'post'):
                    raise ValueError('Bus ou prise invalide.')
                if data['enabled']:
                    if self.demo:
                        raise RuntimeError('L’aperçu ne produit pas de son à enregistrer.')
                    if not self.snapshot()['available']:
                        raise RuntimeError('La sortie audio traitée est indisponible.')
                    if not studio.plan.settings['enabled'] and data['bus'] != 'main':
                        raise RuntimeError('Les autres bus nécessitent le mode live.')
                    await asyncio.to_thread(studio.recorder.start, data['bus'], data['tap'])
                else:
                    await asyncio.to_thread(studio.recorder.stop)
                return
            if action == 'configure':
                if type(data['revision']) is not int or data['revision'] != studio.revision:
                    raise ValueError('Les réglages ont changé. Recharge-les avant d’appliquer tes modifications.')
                await self.commit(data['settings'])
                return
            if action in ('input', 'bus', 'feedback_lock'):
                settings = self.source_settings()
                if action == 'input':
                    sid = data['id']
                    if not isinstance(sid, str) or sid not in settings['inputs'] or not isinstance(data['changes'], dict):
                        raise ValueError('Source inconnue ou réglages invalides.')
                    settings['inputs'][sid].update(data['changes'])
                else:
                    bus = data['bus']
                    if not isinstance(bus, str) or bus not in BUSES:
                        raise ValueError('Bus inconnu.')
                    if action == 'bus':
                        if not isinstance(data['changes'], dict):
                            raise ValueError('Réglages de bus attendus.')
                        settings['buses'][bus].update(data['changes'])
                    else:
                        dsp = backend.audio_engine.live.dsp if not self.demo and backend.audio_engine.live else studio.dsp
                        fbq = dsp.feedback.get(bus) if dsp else None
                        if not fbq or not fbq.balance:
                            raise RuntimeError('Aucun filtre de balance à figer.')
                        fixed = [{k: item[k] for k in ('frequency', 'gain', 'q')} for item in fbq.balance]
                        settings['buses'][bus]['feedback']['filters'] += fixed
                        settings['buses'][bus]['feedback']['mode'] = 'assist'
                await self.commit(settings)
                return
            name = label(data['name'])
            scenes = copy.deepcopy(backend.state.data.get('scenes', {}))
            if action == 'scene_save':
                source_controls = {device['address']: {key: device[key] for key in ('volume', 'muted', 'gain', 'solo')
                                   if key in device and device[key] is not None} for device in backend.snapshot()['devices']}
                scene = {'version': 1, 'studio': self.source_settings(), 'equalizer': backend.snapshot()['equalizer']['settings'],
                         'master': dict(backend.master), 'sources': source_controls}
                scenes[name] = scene
            elif action == 'scene_import':
                scene = self.checked_scene(data['scene'])
                scenes[name] = scene
            elif action == 'scene_delete':
                if name not in scenes:
                    raise ValueError('Scène inconnue.')
                del scenes[name]
            else:
                if name not in scenes:
                    raise ValueError('Scène inconnue.')
                scene = self.checked_scene(scenes[name])
                old = self.checked_scene({'version': 1, 'studio': self.source_settings(),
                        'equalizer': backend.snapshot()['equalizer']['settings'], 'master': dict(backend.master),
                        'sources': {device['address']: {key: device[key] for key in ('volume', 'muted', 'gain', 'solo')
                                   if device.get(key) is not None} for device in backend.snapshot()['devices']}})
                try:
                    await self.commit(scene['studio'], scene['equalizer'], scene['master'])
                    await backend.restore_scene_sources(scene['sources'])
                except BaseException:
                    await self.commit(old['studio'], old['equalizer'], old['master'])
                    await backend.restore_scene_sources(old['sources'])
                    raise
                return
            if len(scenes) > 20:
                raise ValueError('Vingt scènes maximum. Supprime une scène avant d’en ajouter une.')
            previous = backend.state.data.get('scenes')
            backend.state.data['scenes'] = scenes
            try:
                backend.state.save()
            except BaseException:
                if previous is None:
                    backend.state.data.pop('scenes', None)
                else:
                    backend.state.data['scenes'] = previous
                raise

    @staticmethod
    def checked_scene(value):
        from app import bt_address, number, boolean
        if not isinstance(value, dict) or set(value) != {'version', 'studio', 'equalizer', 'master', 'sources'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('Format de scène invalide ou version incompatible.')
        studio = StudioPlan(value['studio']).settings
        eq = EqualizerPlan.create(value['equalizer']).settings
        master = checked_settings(value['master'], 4)
        sources = value['sources']
        if not isinstance(sources, dict) or len(sources) > 48:
            raise ValueError('Sources de scène invalides.')
        checked = {}
        for address, item in sources.items():
            if not isinstance(item, dict) or set(item) - {'volume', 'gain', 'muted', 'solo'}:
                raise ValueError('Réglages de source invalides.')
            normalized = bt_address(address)
            if normalized in checked:
                raise ValueError('Source de scène répétée.')
            checked[normalized] = {key: boolean(v) if key in ('muted', 'solo') else number(v, -24 if key == 'gain' and studio['enabled'] else 0, 24 if key == 'gain' and studio['enabled'] else 4 if key == 'gain' else 100)
                                            for key, v in item.items()}
        return {'version': 1, 'studio': studio, 'equalizer': eq, 'master': master, 'sources': checked}

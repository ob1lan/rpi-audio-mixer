#!/usr/bin/env python3
"""First-run configuration through the running app's authenticated local API."""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def read_environment(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if separator:
            result[name] = value.strip().strip('\"\'')
    return result


class Client:
    def __init__(self, environment):
        host = environment.get('BUREAU_HOST', '0.0.0.0')
        host = {'0.0.0.0': '127.0.0.1', '::': '[::1]'}.get(host, host)
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        self.origin = 'http://' + host + ':' + environment.get('BUREAU_PORT', '8088')
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request('/api/login', {'key': environment['BUREAU_KEY']})

    def request(self, path, data=None):
        request = urllib.request.Request(self.origin + path,
                                        data=None if data is None else json.dumps(data).encode(),
                                        headers={'Origin': self.origin, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            message = error.read().decode(errors='replace')
            try:
                message = json.loads(message).get('error', message)
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(message) from error


class Terminal:
    """Separate streams work with non-seekable terminals, including SSH."""
    def __init__(self, path='/dev/tty'):
        self.reader = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOCTTY), 'r')
        try:
            self.writer = os.fdopen(os.open(path, os.O_WRONLY | os.O_NOCTTY), 'w')
        except OSError:
            self.reader.close()
            raise

    def write(self, value):
        return self.writer.write(value)

    def flush(self):
        self.writer.flush()

    def readline(self):
        return self.reader.readline()

    def close(self):
        self.reader.close()
        self.writer.close()


def choose_output(configuration, requested=None, non_interactive=False, terminal=None):
    choices = configuration.get('output_choices', [])
    if requested:
        choices = [choice for choice in choices
                   if requested == '{}:{}'.format(choice['card'], choice.get('device', 0))]
        if len(choices) != 1:
            raise RuntimeError('Sortie absente ou ambiguë : ' + requested)
    elif configuration.get('output'):
        # Do not overwrite routes, controls, gain or saved master settings.
        return None
    if not choices:
        raise RuntimeError('Aucune sortie ALSA détectée. Branche une carte son, puis relance la configuration.')
    if len(choices) == 1:
        selected = choices[0]
    else:
        if non_interactive or terminal is None:
            available = ', '.join('{}:{}'.format(c['card'], c.get('device', 0)) for c in choices)
            raise RuntimeError('Plusieurs sorties : utilise --output CARD:DEVICE parmi ' + available)
        print('\nChoisis la sortie audio :', file=terminal, flush=True)
        for index, choice in enumerate(choices, 1):
            print('  {}. {} — {}:{}'.format(index, choice.get('label') or choice['name'],
                                           choice['card'], choice.get('device', 0)), file=terminal)
        while True:
            print('Numéro : ', end='', file=terminal, flush=True)
            answer = terminal.readline()
            if not answer:
                raise RuntimeError('Sélection interrompue.')
            if answer.strip().isdigit() and 1 <= int(answer) <= len(choices):
                selected = choices[int(answer) - 1]
                break
    controls = selected.get('controls', [])
    control = next((c for c in controls if c.get('software')), controls[0] if controls else None)
    if control is None:
        raise RuntimeError('Aucun contrôle de volume utilisable sur cette sortie.')
    return {'card': selected['card'], 'pcm': selected['pcm'], 'mode': selected.get('mode', 'managed'),
            'label': selected.get('label') or selected['name'], 'control': control['name']}


def configure(client, requested=None, non_interactive=False, terminal=None):
    status = client.request('/api/status')
    output = choose_output(status['configuration'], requested, non_interactive, terminal)
    if output:
        client.request('/api/configuration', {'output': output})
        print('Sortie configurée : ' + output['label'])
    else:
        print('Sortie et réglages existants conservés.')
    problems = []
    for adapter in status.get('adapters', []):
        if not adapter.get('present'):
            continue
        if not adapter.get('powered'):
            try:
                client.request('/api/radio', {'adapter': adapter['address']})
            except RuntimeError as error:
                problems.append('{} : {}'.format(adapter.get('alias') or adapter['address'], error))
    # Read back actual state: an HTTP success alone does not prove audio/radios work.
    status = client.request('/api/status')
    if not status.get('master_ready'):
        problems.extend(status['configuration'].get('diagnostics', []))
        problems.append('La sortie audio n’est pas prête.')
    present = [a for a in status.get('adapters', []) if a.get('present')]
    if not present:
        problems.append('Aucun récepteur Bluetooth détecté. Vérifie les dongles et l’activation Bluetooth du Pi.')
    for adapter in present:
        powered = adapter.get('powered', False)
        print('Bluetooth {} ({}) : {}'.format(adapter.get('alias') or adapter['address'],
                                              adapter['address'], 'allumé' if powered else 'bloqué / éteint'))
        if not powered:
            problems.append('Récepteur éteint : ' + adapter['address'] + '. Vérifie rfkill list bluetooth et le matériel.')
    for diagnostic in status['configuration'].get('diagnostics', []):
        print('Diagnostic : ' + diagnostic)
    if problems:
        raise RuntimeError('\n'.join(dict.fromkeys(problems)))
    print('Pour chaque source, ouvre « Appairer » dans la page Bluetooth, puis sélectionne le récepteur sur le téléphone / ordinateur.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default='/etc/bureau-audio.env')
    parser.add_argument('--output', help='Stable ALSA card identifier and device, e.g. Headphones:0')
    parser.add_argument('--non-interactive', action='store_true')
    args = parser.parse_args()
    terminal = None
    try:
        if not args.non_interactive:
            try:
                terminal = Terminal()
            except OSError:
                pass
        client = Client(read_environment(args.env))
        # BlueZ/ALSA discovery can finish after the health endpoint is available.
        for attempt in range(15):
            status = client.request('/api/status')
            if status['configuration'].get('output_choices') and status.get('adapters'):
                break
            time.sleep(1)
        configure(client, args.output, args.non_interactive, terminal)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print('Configuration incomplète : ' + str(error), file=sys.stderr)
        return 1
    finally:
        if terminal:
            terminal.close()


if __name__ == '__main__':
    sys.exit(main())

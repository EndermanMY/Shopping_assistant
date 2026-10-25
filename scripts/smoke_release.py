"""Launch the compiled EXE with isolated data, never enable purchase tasks."""
import argparse
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    data = ROOT / 'data' / 'qa' / f'release-smoke-{int(time.time())}'
    data.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, SHOPPING_DATA_DIR=str(data), SHOPPING_PORT=str(port), PLAYWRIGHT_BROWSERS_PATH=str(bundle/'data/browsers'))
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    windows = Path(env.get('SystemRoot', r'C:\Windows'))
    env['PATH'] = os.pathsep.join((str(windows/'System32'), str(windows)))
    version = json.loads((bundle/'version.json').read_text(encoding='utf-8'))['version']
    report = {'version': version, 'checks': {}, 'purchase_tasks_started': 0}
    with (data/'stdout.log').open('w', encoding='utf-8') as stdout, (data/'stderr.log').open('w', encoding='utf-8') as stderr:
        process = subprocess.Popen([str(bundle/'ShoppingAssistant.exe')], cwd=data, env=env, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        client = httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=60)
        try:
            deadline = time.monotonic()+40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'Compiled process exited; inspect {data / "stderr.log"}')
                try:
                    health = client.get('/api/health').json()
                    if health.get('ok'):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('Compiled service did not become ready')
            assert health['version'] == version
            report['checks']['executable_starts_without_python_command'] = True
            html = client.get('/').text
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
            assert assets and all(client.get(path).status_code == 200 for path in assets)
            report['checks']['bundled_frontend_assets'] = True
            assert client.get('/api/overview').status_code == 401
            code = json.loads((data/'access.json').read_text(encoding='utf-8'))['code']
            client.post('/api/auth/login', json={'code': code}).raise_for_status()
            overview = client.get('/api/overview').json()
            assert overview['tasks'] == [] and overview['busy_task'] is None
            report['checks']['authentication_and_empty_private_data'] = True
            client.post('/api/account/login').raise_for_status()
            account = client.get('/api/account/status').json()
            assert account['browser_open'] and not account['logged_in']
            report['checks']['bundled_playwright_driver_and_chromium'] = True
            client.post('/api/account/close').raise_for_status()
            report['checks']['browser_closes'] = True
            client.post('/api/auth/logout').raise_for_status()
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    output = ROOT/'data/qa/release-smoke.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()

"""Owned project browser with a loopback-only, ephemeral CDP connection."""
import asyncio
import re
import socket
import subprocess
import sys
from urllib.parse import urlparse
from .process_job import BrowserJob


class NativeBrowser:
    def __init__(self):
        self.process = None
        self.connection = None
        self.drain = None
        self.port = None
        self.job = None

    async def start(self, chromium, executable, profile, direct=False):
        profile.mkdir(parents=True, exist_ok=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.port = port
        arguments = [str(executable), f'--user-data-dir={profile}',
            '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
            '--no-first-run', '--no-default-browser-check', '--lang=zh-CN', '--start-maximized']
        if direct:
            arguments.append('--no-proxy-server')
        arguments.append('about:blank')
        try:
            self.process = await asyncio.create_subprocess_exec(*arguments,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
            self.job = BrowserJob(self.process.pid)
            endpoint = await asyncio.wait_for(self._endpoint(port), timeout=20)
            # Prevent a full stderr pipe from blocking this owned GUI process.
            self.drain = asyncio.create_task(self._discard_stderr())
            self.connection = await chromium.connect_over_cdp(endpoint, timeout=10000, no_defaults=True)
            if not self.connection.contexts:
                raise RuntimeError('Browser context unavailable')
            return self.connection.contexts[0]
        except BaseException:
            await self.close()
            raise

    async def _endpoint(self, port):
        while True:
            line = await self.process.stderr.readline()
            if not line:
                raise RuntimeError('Independent browser exited before CDP started')
            match = re.search(rb'DevTools listening on (ws://[^\s]+)', line)
            if match:
                endpoint = match.group(1).decode('ascii')
                parsed = urlparse(endpoint)
                if parsed.hostname not in {'127.0.0.1','::1'} or parsed.port != port or not parsed.path.startswith('/devtools/browser/'):
                    raise RuntimeError('Unexpected debugging endpoint')
                return endpoint

    async def _discard_stderr(self):
        while await self.process.stderr.read(4096):
            pass

    async def close(self):
        if self.connection is not None:
            try:
                session = await self.connection.new_browser_cdp_session()
                await asyncio.wait_for(session.send('Browser.close'), timeout=5)
            except Exception:
                pass
        if self.process is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.terminate()
                await self.process.wait()
        if self.drain is not None:
            self.drain.cancel()
            try:
                await self.drain
            except (asyncio.CancelledError, Exception):
                pass
        self.process = self.connection = self.drain = None
        self.port = None
        if self.job is not None:
            self.job.close()
            self.job = None

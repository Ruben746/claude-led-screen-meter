"""Optional ChatGPT plan meter through the official Codex app-server protocol."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


class CodexError(Exception):
    pass


def codex_command():
    binary = shutil.which('codex.exe') or shutil.which('codex')
    if not binary:
        raise CodexError('Installe Codex CLI pour connecter ChatGPT : npm install -g @openai/codex')
    if os.name == 'nt' and Path(binary).suffix.lower() in ('.cmd', '.ps1'):
        script = Path(binary).parent / 'node_modules/@openai/codex/bin/codex.js'
        node = shutil.which('node')
        if not node or not script.is_file():
            raise CodexError('Installation Codex CLI introuvable. Réinstalle @openai/codex.')
        return [node, str(script)]
    return [binary]


def normalize_limits(data):
    buckets = data.get('rateLimitsByLimitId')
    if not buckets:
        legacy = data.get('rateLimits')
        buckets = {legacy.get('limitId') or 'codex': legacy} if legacy else {}
    result = []
    for key, bucket in buckets.items():
        windows = []
        for kind in ('primary', 'secondary'):
            w = bucket.get(kind)
            if not w or w.get('usedPercent') is None:
                continue
            windows.append({'kind': kind, 'used': max(0, min(100, float(w['usedPercent']))),
                            'minutes': w.get('windowDurationMins'), 'reset': w.get('resetsAt')})
        result.append({'id': key, 'name': bucket.get('limitName') or key,
                       'plan': bucket.get('planType'), 'windows': windows})
    return result


class CodexMeter:
    def __init__(self, home):
        self.home = Path(home)
        self.process = None
        self.reader = None
        self.pending = {}
        self.counter = 0
        self.lock = asyncio.Lock()
        self.enabled = (self.home / 'enabled').exists()
        self.connected = False
        self.login = None
        self.error = ''
        self.buckets = []
        self.last_ok = None
        self.fetching = False
        self.wake = asyncio.Event()

    def status(self):
        return {'connected': self.connected, 'error': self.error, 'buckets': self.buckets,
                'last_ok': self.last_ok, 'fetching': self.fetching, 'enabled': self.enabled}

    async def start(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                return
            self.home.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env['CODEX_HOME'] = str(self.home)
            for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
                env.pop(key, None)
            self.process = await asyncio.create_subprocess_exec(
                *codex_command(), '-c', 'cli_auth_credentials_store="file"', 'app-server',
                cwd=str(self.home), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.reader = asyncio.create_task(self.read_messages())
            try:
                await self.rpc('initialize', {'clientInfo': {'name':'led_meter','version':'1.0'}})
                self.process.stdin.write(b'{"method":"initialized"}\n')
                await self.process.stdin.drain()
            except BaseException:
                await self.close()
                raise

    async def read_messages(self):
        try:
            while line := await self.process.stdout.readline():
                msg = json.loads(line)
                if 'id' in msg and msg['id'] in self.pending:
                    future = self.pending.pop(msg['id'])
                    if not future.done():
                        if 'error' in msg:
                            future.set_exception(CodexError('Codex a refusé la requête. Vérifie la connexion au compte et réessaie.'))
                        else:
                            future.set_result(msg.get('result', {}))
                elif msg.get('method') == 'account/login/completed':
                    self.login = None
                    self.error = '' if msg.get('params', {}).get('success') else 'Connexion ChatGPT non terminée. Réessaie.'
                    self.wake.set()
        except (ValueError, OSError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(CodexError('Connexion au service Codex interrompue.'))
            self.pending.clear()

    async def rpc(self, method, params=None):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            message = {'id': ident, 'method': method}
            if params is not None:
                message['params'] = params
            self.process.stdin.write((json.dumps(message)+'\n').encode())
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(ident, None)

    async def connect(self):
        await self.start()
        if self.login:
            return self.login
        self.login = await self.rpc('account/login/start', {'type':'chatgptDeviceCode'})
        self.enabled = True
        (self.home/'enabled').touch()
        self.wake.set()
        return self.login

    async def disconnect(self):
        await self.start()
        if self.login:
            await self.rpc('account/login/cancel', {'loginId': self.login['loginId']})
        await self.rpc('account/logout')
        self.enabled = self.connected = False
        self.login = None
        self.buckets = []
        self.last_ok = None
        self.error = ''
        (self.home/'enabled').unlink(missing_ok=True)
        await self.close()

    async def refresh(self):
        self.fetching = True
        try:
            await self.start()
            account = (await self.rpc('account/read', {'refreshToken':True})).get('account')
            self.connected = bool(account and account.get('type') == 'chatgpt')
            if not self.connected:
                self.error = 'Termine la connexion ChatGPT.'
                return
            self.buckets = normalize_limits(await self.rpc('account/rateLimits/read'))
            self.last_ok = time.time()
            self.error = ''
        except (CodexError, OSError, asyncio.TimeoutError, ValueError, TypeError) as e:
            self.error = str(e) if isinstance(e, CodexError) else 'Impossible de récupérer les quotas ChatGPT. Nouvelle tentative automatique.'
        finally:
            self.fetching = False

    async def poll(self):
        while True:
            self.wake.clear()
            if self.enabled:
                await self.refresh()
            try:
                await asyncio.wait_for(self.wake.wait(), 60)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.process = None


class UsageSwitch:
    """Queue changed providers, showing each for at least eight seconds."""
    def __init__(self):
        self.previous = {}
        self.pending = []
        self.active = 'claude'
        self.until = 0

    def observe(self, provider, values):
        old = self.previous.get(provider)
        self.previous[provider] = values
        if old is not None and old != values and provider not in self.pending:
            self.pending.append(provider)

    def choose(self, now, available):
        self.pending = [p for p in self.pending if p in available]
        if self.active not in available:
            self.active = next(iter(available), 'claude')
            self.until = 0
        if self.pending and now >= self.until:
            self.active = self.pending.pop(0)
            self.until = now + 8
        return self.active

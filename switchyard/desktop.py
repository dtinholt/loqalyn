"""Native-app service lifecycle and authenticated loopback client.

AppKit lives on the main thread. All public methods here are blocking and are
called by the UI's workers; aiohttp and the gateway share one owned event loop.
The controller never attaches to, kills, or reconfigures an unrelated service.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .server import create_app
from .security import private_directory, private_file, loads


class DesktopError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class DesktopController:
    def __init__(self, data_dir: Path | None = None, port: int = 8741, *, use_keychain=True):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('Choose a port between 1 and 65535.')
        self.data_dir = Path(data_dir) if data_dir is not None else Path.home() / 'Library' / 'Application Support' / 'Switchyard'
        self.port = port
        self.endpoint = f'http://127.0.0.1:{port}'
        self._token = ''
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._startup = None
        self._serve_task = None
        self._stop_requested = False
        self._guard = threading.RLock()
        self._running = False
        self._socket = None
        self._lock_file = None
        self.use_keychain = use_keychain

    @property
    def token(self):
        return self._token

    def start(self):
        """Own a loopback listener or fail without touching another process."""
        with self._guard:
            if self._running:
                startup = None
            elif self._thread and self._thread.is_alive():
                startup = self._startup
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('127.0.0.1', self.port))
                    sock.listen(128)
                    sock.setblocking(False)
                except OSError as exc:
                    sock.close()
                    raise DesktopError(f'Port {self.port} is already in use or unavailable. Quit any other Switchyard app or yard service, then reopen Switchyard.', {'code': 'port_unavailable'}) from exc
                try:
                    private_directory(self.data_dir)
                    lock_path=self.data_dir/'desktop.lock'
                    private_file(lock_path,allow_missing=True)
                    self._lock_file = os.fdopen(os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600),'a')
                    fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, ValueError) as exc:
                    sock.close()
                    if self._lock_file:
                        self._lock_file.close()
                        self._lock_file = None
                    raise DesktopError('Switchyard cannot open its data folder, or another app is using it.', {'code': 'data_unavailable'}) from exc
                self._socket = sock
                self._stop_requested = False
                self._startup = startup = concurrent.futures.Future()
                self._thread = threading.Thread(target=self._thread_main, args=(startup,), name='switchyard-desktop-service', daemon=True)
                self._thread.start()
        # Discovery may take seconds. Never hold the lifecycle lock while waiting:
        # Quit must be able to cancel startup and close its partial resources.
        if startup is not None:
            try:
                startup.result(timeout=45)
            except concurrent.futures.TimeoutError:
                self.stop()
                raise DesktopError('The local service did not finish starting. Reopen Switchyard to try again.', {'code': 'startup_timeout'})
        if self._stop_requested:
            raise DesktopError('Local service startup was cancelled.', {'code': 'startup_cancelled'})
        return self.call('/switchyard/status')

    def _thread_main(self, startup):
        error = None
        try:
            asyncio.run(self._serve(startup))
        except asyncio.CancelledError:
            error = DesktopError('Local service startup was cancelled.', {'code': 'startup_cancelled'})
        except BaseException as exc:
            error = DesktopError(f'The local service could not start: {exc}', {'code': 'startup_failed'})
        finally:
            self._running = False
            if self._socket:
                self._socket.close()
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None
            # Failed/cancelled startup becomes observable only after cleanup.
            if not startup.done():
                startup.set_exception(error or DesktopError('Local service startup was cancelled.', {'code': 'startup_cancelled'}))

    async def _serve(self, startup):
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._serve_task = asyncio.current_task()
        if self._stop_requested:
            raise asyncio.CancelledError()
        # The explicit macOS backend avoids platform fallback credential stores.
        if sys.platform == 'darwin' and self.use_keychain:
            try:
                from keyring.backends.macOS import Keyring
                value = Keyring().get_password('switchyard', 'lmstudio')
                if value:
                    os.environ['SWITCHYARD_LM_TOKEN'] = value
            except Exception:
                pass  # Unavailable optional runtime credentials do not block launch.
        runner = None
        try:
            app = await create_app(self.data_dir)
            self._token = app['service'].token
            runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=3)
            await runner.setup()
            await web.SockSite(runner, self._socket).start()
            self._running = True
            startup.set_result(None)
            await self._stop_event.wait()
        finally:
            self._running = False
            if runner:
                await runner.cleanup()

    def stop(self):
        """Stop only this controller's service, cancel active requests, release port."""
        with self._guard:
            self._stop_requested = True
            thread, loop = self._thread, self._loop
            if loop and loop.is_running():
                def request_stop():
                    if self._stop_event:
                        self._stop_event.set()
                    if not self._running and self._serve_task and not self._serve_task.done():
                        self._serve_task.cancel()
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(request_stop)
            if thread and thread is not threading.current_thread():
                thread.join(timeout=12)
                if thread.is_alive():
                    raise DesktopError('The local service is still shutting down. Please wait before reopening it.', {'code': 'shutdown_timeout'})
            self._running = False
            self._thread = None
            self._loop = None
            self._stop_event = None
            self._serve_task = None

    def _path(self, path):
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '\\' in path or any(ord(c) < 32 for c in path):
            raise DesktopError('Only local Switchyard API paths are allowed.', {'code': 'invalid_path'})
        parts = urlsplit(path)
        if parts.scheme or parts.netloc or parts.fragment or '..' in parts.path.split('/'):
            raise DesktopError('Only local Switchyard API paths are allowed.', {'code': 'invalid_path'})
        return self.endpoint + path

    def _submit(self, coroutine):
        if not self._running or not self._loop or not self._loop.is_running():
            coroutine.close()
            raise DesktopError('The local service is not running.', {'code': 'not_running'})
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    @staticmethod
    async def _response(response):
        raw = bytearray()
        async for part in response.content.iter_chunked(65536):
            raw.extend(part)
            if len(raw) > 8 * 1024 * 1024:
                raise DesktopError('The service response is too large.', {'code': 'response_limit'})
        try:
            data = loads(raw,limit=8*1024*1024)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DesktopError(f'The service returned an unreadable response (HTTP {response.status}).', {'code': 'invalid_response'}) from exc
        if response.status >= 300:
            inner = data.get('error', {}) if isinstance(data, dict) else {}
            if not isinstance(inner, dict):
                inner = {'message': str(inner)}
            inner.setdefault('status', response.status)
            raise DesktopError(inner.get('message', f'Request failed (HTTP {response.status}).'), inner)
        return data

    async def _request(self, url, method, body):
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=7200)) as session:
                async with session.request(method, url, json=body, headers={'Authorization': 'Bearer ' + self.token}, allow_redirects=False) as response:
                    return await self._response(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DesktopError('The local service connection failed or timed out.', {'code': 'connection_failed'}) from exc

    def call(self, path, method='GET', body=None):
        url = self._path(path)
        if method not in ('GET', 'POST', 'PUT'):
            raise DesktopError('Unsupported API method.', {'code': 'invalid_method'})
        return self._submit(self._request(url, method, copy.deepcopy(body))).result(timeout=7210)

    async def _stream(self, body, on_delta, result):
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=7200)) as session:
                async with session.post(self.endpoint + '/v1/chat/completions', json=body, headers={'Authorization': 'Bearer ' + self.token}, allow_redirects=False) as response:
                    result['request_id'] = response.headers.get('X-Switchyard-Request-Id')
                    if response.status >= 300:
                        await self._response(response)
                    if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                        raise DesktopError('The local service did not return a text stream.', {'code': 'invalid_stream'})
                    completed = False
                    async for raw in response.content:
                        if not raw.startswith(b'data:'):
                            continue
                        value = raw[5:].strip()
                        if value == b'[DONE]':
                            completed = True
                            break
                        try:
                            chunk = loads(value)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise DesktopError('The model stream was interrupted or malformed.', {'code': 'invalid_stream'}) from exc
                        if not isinstance(chunk, dict):
                            raise DesktopError('The model returned an invalid stream chunk.', {'code': 'invalid_stream'})
                        if 'error' in chunk:
                            error = chunk['error']
                            raise DesktopError(error.get('message', 'Generation failed.') if isinstance(error, dict) else str(error), error if isinstance(error, dict) else {})
                        result['model'] = chunk.get('model') or result['model']
                        for choice in chunk.get('choices', []):
                            text = choice.get('delta', {}).get('content')
                            if isinstance(text, str):
                                result['content'] += text
                            if choice.get('finish_reason'):
                                result['finish_reason'] = choice['finish_reason']
                        on_delta(chunk)
                    if not completed:
                        raise DesktopError('The model stream ended before completion.', {'code': 'incomplete_stream'})
                    return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DesktopError('Generation lost its local connection or timed out.', {'code': 'connection_failed'}) from exc

    def stream(self, body, on_delta, cancel_event):
        result = {'cancelled': False, 'model': None, 'finish_reason': None, 'content': '', 'request_id': None}
        if cancel_event.is_set():
            return {**result, 'cancelled': True}
        if not isinstance(body, dict):
            raise DesktopError('A generation request must be a JSON object.', {'code': 'invalid_request'})
        body = copy.deepcopy(body)
        body['stream'] = True
        future = self._submit(self._stream(body, on_delta, result))
        while True:
            try:
                return future.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                if cancel_event.is_set():
                    future.cancel()
                    return {**result, 'cancelled': True}
            except concurrent.futures.CancelledError:
                return {**result, 'cancelled': True}

    def set_key(self, provider, secret):
        if provider not in ('openai', 'anthropic', 'lmstudio') or not isinstance(secret, str) or not secret.strip():
            raise DesktopError('Choose a provider and enter its key.', {'code': 'invalid_key'})
        if sys.platform != 'darwin':
            raise DesktopError('Credentials require the macOS Keychain.', {'code': 'keychain_required'})
        try:
            from keyring.backends.macOS import Keyring
            Keyring().set_password('switchyard', provider, secret.strip())
            if provider == 'lmstudio':
                os.environ['SWITCHYARD_LM_TOKEN'] = secret.strip()
                # Re-applying current policy refreshes runtime credentials safely.
                policy = self.call('/switchyard/policy')
                self.call('/switchyard/policy', 'PUT', policy)
                self.call('/switchyard/refresh', 'POST', {})
        except DesktopError:
            raise
        except Exception as exc:
            raise DesktopError('The key could not be saved to the macOS Keychain.', {'code': 'keychain_failed'}) from exc


def main():
    if '--cli' in sys.argv:
        sys.argv.remove('--cli')
        from .cli import main as cli_main
        cli_main(); return
    if sys.platform != 'darwin':
        raise SystemExit('The desktop interface requires macOS. Use yard serve on other systems.')
    from .desktop_ui import run_app
    directory=os.environ.get('SWITCHYARD_DIR')
    run_app(DesktopController(Path(directory) if directory else None, int(os.environ.get('SWITCHYARD_PORT','8741'))))


if __name__ == '__main__':
    main()

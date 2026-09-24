import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values
import claude_meter as meter


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_connection_or_settings_are_closed(self):
        for stage in ('connect', 'settings'):
            with self.subTest(stage=stage):
                display = meter.LedDisplay('test-display')
                client = meter.AsyncClient('test-display')
                session = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
                client._session = session
                display.on_connect = AsyncMock()
                failing = session.connect if stage == 'connect' else display.on_connect
                failing.side_effect = RuntimeError('connection lost')
                with patch.object(meter, 'AsyncClient', return_value=client):
                    with self.assertRaises(RuntimeError):
                        await display.connect()
                session.disconnect.assert_awaited_once()
                self.assertIsNone(display.client)

    async def test_orientations_and_migration(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(meter, 'ENV_PATH', str(Path(directory) / '.env')):
            state = meter.State()
            async with TestClient(TestServer(meter.make_app(meter.LedDisplay(''), state))) as client:
                for value in (0, 2):
                    response = await client.post('/api/set', json={'name': 'orientation', 'value': value})
                    self.assertEqual(response.status, 200)
                for value in (1, 3, 90, 180, 270, None, True, 2.0):
                    response = await client.post('/api/set', json={'name': 'orientation', 'value': value})
                    self.assertEqual(response.status, 400, value)
                response = await client.get('/api/state')
                self.assertFalse((await response.json())['device']['connected'])
            for old in ('1', '3'):
                with patch.dict(os.environ, {'LED_ORIENTATION': old}):
                    self.assertEqual(meter.State().orientation, 0)

    async def test_loop_reconnects_unchanged_frame_on_and_off(self):
        for power in (True, False):
            with self.subTest(power=power):
                state = meter.State()
                state.power = power
                display = SimpleNamespace(address='test', connected=True,
                                          send_image=AsyncMock(), set_power=AsyncMock())
                ticks = 0

                async def tick(_):
                    nonlocal ticks
                    ticks += 1
                    if ticks == 2:
                        display.connected = False
                    if ticks == 3:
                        raise asyncio.CancelledError

                with patch.object(meter, 'get_usage', return_value=(10, 20, '12:00')), \
                        patch.object(meter, 'save_frame', return_value='frame.png'), \
                        patch.object(meter.asyncio, 'sleep', side_effect=tick):
                    with self.assertRaises(asyncio.CancelledError):
                        await meter.display_loop(display, state)
                action = display.send_image if power else display.set_power
                self.assertEqual(action.await_count, 2)
                if not power:
                    display.send_image.assert_not_awaited()
                    display.set_power.assert_awaited_with(False)

    async def test_settings_and_device_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / '.env')
            display = meter.LedDisplay('')
            state = meter.State()
            with patch.object(meter, 'ENV_PATH', path), patch.object(meter, 'ADMIN_TOKEN', ''):
                async with TestClient(TestServer(meter.make_app(display, state))) as client:
                    values = dict(brightness=24, orientation=2, alternate=45, refresh=90,
                                  reset_anim=False, increase_anim=False, power=False)
                    for name, value in values.items():
                        response = await client.post('/api/set', json={'name': name, 'value': value})
                        self.assertEqual(response.status, 200)
                    response = await client.post('/api/device', json={'address': 'test-display'})
                    self.assertEqual(response.status, 200)
                saved = dotenv_values(path)
                self.assertEqual(saved['LED_ADDRESS'], 'test-display')
                with patch.dict(os.environ, saved):
                    reloaded = meter.State()
                for name, value in values.items():
                    self.assertEqual(getattr(reloaded, name), value, name)

    async def test_disconnected_session_is_detected_and_replaced(self):
        display = meter.LedDisplay('test-display')
        stale = SimpleNamespace(_session=SimpleNamespace(is_connected=False), disconnect=AsyncMock())
        fresh = SimpleNamespace(_session=SimpleNamespace(is_connected=True),
                                connect=AsyncMock(), send_image=AsyncMock())
        display.client = stale
        self.assertFalse(display.connected)
        with patch.object(meter, 'AsyncClient', return_value=fresh):
            await display.send_image('frame.png')
        stale.disconnect.assert_awaited_once()
        fresh.connect.assert_awaited_once()
        fresh.send_image.assert_awaited_once_with('frame.png')

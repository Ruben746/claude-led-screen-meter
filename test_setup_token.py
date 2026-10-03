import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
import claude_meter as m

TOKEN = 'sk-ant-oat01-test-only'

class SetupTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, value in {'SETUP_TOKEN_FILE': str(Path(self.tmp.name)/'token.json'), 'ENV_PATH': str(Path(self.tmp.name)/'.env'), 'ADMIN_TOKEN': 'admin'}.items():
            p = patch.object(m, key, value); p.start(); self.addCleanup(p.stop)
        self.addCleanup(m.AUTH.update, m.AUTH.copy())
        m.AUTH['mode'] = 'session'
        self.state = m.State()
        self.client = TestClient(TestServer(m.make_app(SimpleNamespace(address='', connected=False), self.state)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def submit(self, token=TOKEN, admin='admin'):
        return await self.client.post('/api/auth', json={'mode':'setup-token','setup_token':token,'token':admin})

    async def test_save_poll_restart_and_no_secret_in_state(self):
        with patch.object(m.requests, 'get', return_value=Mock(status_code=200, json=lambda: {'five_hour':{'utilization':23}})) as get, patch.object(m.requests, 'post') as post:
            response = await self.submit()
            self.assertEqual(response.status, 200)
            self.assertEqual(m.AUTH['mode'], 'setup-token')
            self.assertTrue(self.state.force_refetch)
            self.assertEqual(m.get_usage()[0], 23)
            self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer '+TOKEN)
            post.assert_not_called()
        self.assertEqual(m._load_setup_token(), TOKEN)
        self.assertIn('setup-token', Path(m.ENV_PATH).read_text())
        response = await self.client.get('/api/state')
        self.assertNotIn(TOKEN, await response.text())
        self.assertTrue((await response.json())['auth']['setup_token']['saved'])

    async def test_rejected_token_preserves_credentials_and_mode(self):
        Path(m.SETUP_TOKEN_FILE).write_text(json.dumps({'accessToken': 'previous'}))
        for status in (401,403,429):
            with patch.object(m.requests, 'get', return_value=Mock(status_code=status, headers={'retry-after':'60'})), patch.object(m.requests, 'post') as post:
                response = await self.submit()
                self.assertIn(response.status, (401,429))
                self.assertNotIn(TOKEN, await response.text())
                self.assertEqual(m.AUTH['mode'], 'session')
                self.assertEqual(m._load_setup_token(), 'previous')
                post.assert_not_called()

    async def test_admin_and_invalid_input(self):
        with patch.object(m.requests, 'get') as get:
            self.assertEqual((await self.submit(admin='wrong')).status,403)
            for token in ('', 'sk-ant-api03-key', TOKEN+'\nextra', None, 42):
                self.assertEqual((await self.submit(token)).status,400)
            get.assert_not_called()

    async def test_network_failure_redacts_secret(self):
        with patch.object(m.requests, 'get', side_effect=m.requests.Timeout(TOKEN)):
            response = await self.submit()
            self.assertEqual(response.status,502)
            self.assertNotIn(TOKEN,await response.text())
            self.assertEqual(m.AUTH['mode'],'session')

    async def test_expired_saved_token_is_not_refreshed(self):
        Path(m.SETUP_TOKEN_FILE).write_text(json.dumps({'accessToken': TOKEN}))
        with patch.object(m.requests,'get',return_value=Mock(status_code=401)), patch.object(m.requests,'post') as post:
            with self.assertRaisesRegex(m.AuthError,'claude setup-token'):
                m._get_usage_setup_token()
            post.assert_not_called()

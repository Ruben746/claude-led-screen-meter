import base64
import hashlib
import json
import tempfile
import time
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer

import claude_meter as meter


class OAuthTests(unittest.TestCase):
    def test_default_credentials_are_private_to_meter(self):
        env = os.environ.copy()
        env.pop("LED_OAUTH_FILE", None)
        env["LED_ENV_PATH"] = str(Path(self.tmp.name) / "missing.env")
        result = subprocess.run([sys.executable, "-c",
            "import claude_meter as m; assert m.OAUTH_FILE == m.METER_OAUTH_FILE"],
            env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in {
            'OAUTH_FILE': str(Path(self.tmp.name) / 'legacy.json'),
            'METER_OAUTH_FILE': str(Path(self.tmp.name) / 'meter.json'),
            'ENV_PATH': str(Path(self.tmp.name) / '.env'),
        }.items():
            p = patch.object(meter, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def response(self, **tokens):
        return Mock(status_code=200, json=Mock(return_value=tokens))

    def test_login_uses_pkce_and_saves_separate_refreshable_credentials(self):
        flow, url = meter.begin_oauth_login()
        query = parse_qs(urlparse(url).query)
        self.assertNotIn('user:inference', query['scope'][0])
        with patch.object(meter.requests, 'post', return_value=self.response(
                access_token='access', refresh_token='refresh', expires_in=3600)) as post:
            meter.finish_oauth_login(flow, 'code#' + query['state'][0])
        body = post.call_args.kwargs['json']
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'], [challenge])
        self.assertEqual(body['redirect_uri'], query['redirect_uri'][0])
        self.assertEqual(body['code'], 'code')
        self.assertFalse((Path(self.tmp.name) / 'legacy.json').exists())
        data, oauth = meter._load_oauth()
        self.assertEqual(oauth['refreshToken'], 'refresh')
        self.assertGreater(oauth['expiresAt'], time.time() * 1000)
        with patch.object(meter.requests, 'post', return_value=self.response(
                access_token='new-access', refresh_token='rotated', expires_in=3600)):
            meter._refresh_oauth(data, oauth)
        self.assertEqual(meter._load_oauth()[1]['refreshToken'], 'rotated')

    def test_wrong_state_and_expired_flow_never_exchange(self):
        flow, _ = meter.begin_oauth_login()
        with patch.object(meter.requests, 'post') as post:
            with self.assertRaises(ValueError):
                meter.finish_oauth_login(flow, 'code#wrong-state')
            flow['expires_at'] = 0
            with self.assertRaises(ValueError):
                meter.finish_oauth_login(flow, 'code')
            post.assert_not_called()

    def test_missing_refresh_token_does_not_replace_credentials(self):
        flow, _ = meter.begin_oauth_login()
        with patch.object(meter.requests, 'post', return_value=self.response(access_token='incomplete')):
            with self.assertRaises(ValueError):
                meter.finish_oauth_login(flow, 'code')
        self.assertFalse(Path(meter.METER_OAUTH_FILE).exists())

    def test_expiry_refreshes_before_usage_and_401_retries_once(self):
        meter._save_oauth({'claudeAiOauth': {'accessToken': 'old', 'refreshToken': 'refresh', 'expiresAt': 0}})
        with patch.object(meter.requests, 'post', return_value=self.response(
                access_token='fresh', refresh_token='rotated', expires_in=3600)) as post, \
                patch.object(meter.requests, 'get', return_value=self.response(five_hour={'utilization': 12})) as get:
            self.assertEqual(meter._get_usage_oauth()[0], 12)
            self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer fresh')
            self.assertEqual(post.call_count, 1)
        with patch.object(meter.requests, 'post', return_value=self.response(access_token='retry', expires_in=3600)), \
                patch.object(meter.requests, 'get', side_effect=[Mock(status_code=401), self.response()]) as get:
            meter._get_usage_oauth()
            self.assertEqual(get.call_count, 2)
            self.assertEqual(meter._load_oauth()[1]['refreshToken'], 'rotated')


class OAuthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = meter.State()
        self.client = TestClient(TestServer(meter.make_app(
            SimpleNamespace(address="", client=None), self.state)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.admin = patch.object(meter, "ADMIN_TOKEN", "admin")
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.addCleanup(meter.AUTH.update, meter.AUTH.copy())

    async def test_admin_required_for_both_endpoints(self):
        for path in ("/api/oauth/start", "/api/oauth/complete"):
            response = await self.client.post(path, json={})
            self.assertEqual(response.status, 403)

    async def test_complete_once_and_do_not_expose_tokens(self):
        response = await self.client.post("/api/oauth/start", json={"token": "admin"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        flow = await response.json()
        self.assertNotIn("verifier", flow)
        body = {"token": "admin", "flow_id": flow["flow_id"], "code": "code"}
        with patch.object(meter, "finish_oauth_login") as finish:
            response = await self.client.post("/api/oauth/complete", json=body)
            self.assertEqual(await response.json(), {"ok": True})
            self.assertTrue(self.state.force_refetch)
            response = await self.client.post("/api/oauth/complete", json=body)
            self.assertEqual(response.status, 400)
            self.assertEqual(finish.call_count, 1)

    async def test_failure_keeps_existing_mode(self):
        meter.AUTH["mode"] = "session"
        response = await self.client.post("/api/oauth/start", json={"token": "admin"})
        flow = await response.json()
        with patch.object(meter, "finish_oauth_login", side_effect=meter.requests.Timeout("secret")):
            response = await self.client.post("/api/oauth/complete", json={
                "token": "admin", "flow_id": flow["flow_id"], "code": "code"})
            self.assertEqual(response.status, 502)
            self.assertNotIn("secret", await response.text())
        self.assertEqual(meter.AUTH["mode"], "session")


if __name__ == '__main__':
    unittest.main()

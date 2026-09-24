import base64
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import claude_meter as meter

from spotify_meter import SpotifyClient, SpotifyError, SpotifyView, poll_spotify


class SpotifyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'spotify.json'
        self.client = SpotifyClient(self.path, 8080)
        self.client_id = 'a' * 32

    def response(self, status=200, **data):
        return Mock(status_code=status, headers={}, json=Mock(return_value=data))

    def login(self):
        url = self.client.begin(self.client_id)
        query = parse_qs(urlparse(url).query)
        with patch('spotify_meter.requests.post', return_value=self.response(
                access_token='access', refresh_token='refresh', expires_in=3600)) as post:
            self.client.finish(query['state'][0], 'code')
        return query, post

    def test_pkce_login_is_private_persistent_and_one_use(self):
        query, post = self.login()
        body = post.call_args.kwargs['data']
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'], [challenge])
        self.assertEqual(query['scope'], ['user-read-currently-playing'])
        self.assertEqual(query['redirect_uri'], ['http://127.0.0.1:8080/spotify/callback'])
        self.assertTrue(SpotifyClient(self.path, 8080).status()['connected'])
        self.assertNotIn('access', json.dumps(self.client.status()))
        self.assertNotIn('refresh', json.dumps(self.client.status()))
        with self.assertRaises(SpotifyError):
            self.client.finish(query['state'][0], 'code')

    def test_wrong_or_expired_state_does_not_exchange(self):
        self.client.begin(self.client_id)
        with patch('spotify_meter.requests.post') as post:
            with self.assertRaises(SpotifyError):
                self.client.finish('wrong', 'code')
            with patch('spotify_meter.time.monotonic', return_value=10**12):
                with self.assertRaises(SpotifyError):
                    self.client.finish(self.client.pending['state'], 'code')
            post.assert_not_called()

    def test_refresh_rotates_and_empty_or_paused_playback_is_safe(self):
        self.login()
        self.client.tokens['expires_at'] = 0
        with patch('spotify_meter.requests.post', return_value=self.response(
                access_token='new', refresh_token='rotated', expires_in=3600)), \
                patch('spotify_meter.requests.get', return_value=self.response(status=204)):
            self.assertIsNone(self.client.current_track())
        self.assertEqual(json.loads(self.path.read_text())['refresh_token'], 'rotated')
        with patch('spotify_meter.requests.get', return_value=self.response(is_playing=False, item=None)):
            self.assertIsNone(self.client.current_track())

    def test_failed_replacement_preserves_previous_connection(self):
        self.login()
        before = self.path.read_bytes()
        query = parse_qs(urlparse(self.client.begin('b' * 32)).query)
        with patch('spotify_meter.requests.post', return_value=self.response(access_token='incomplete')):
            with self.assertRaises(SpotifyError):
                self.client.finish(query['state'][0], 'code')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.status()['client_id'], self.client_id)

    def test_rate_limit_prevents_repeated_requests(self):
        self.login()
        response = self.response(status=429)
        response.headers = {'Retry-After': '300'}
        with patch('spotify_meter.requests.get', return_value=response) as get:
            for _ in range(2):
                with self.assertRaises(SpotifyError):
                    self.client.current_track()
            self.assertEqual(get.call_count, 1)

    def test_view_returns_to_meter_and_stops_when_disabled(self):
        view = SpotifyView()
        track = {'id': 'one', 'title': 'Titre', 'artist': 'Artiste', 'is_playing': True, 'cover': None}
        frame = view.frame(track, 100, True, 10, 60, True)
        self.assertEqual(frame.size, (96, 16))
        self.assertIsNone(view.frame(track, 111, True, 10, 60, True))
        track['id'] = 'two'
        self.assertIsNotNone(view.frame(track, 112, True, 10, 60, True))
        self.assertIsNone(view.frame(track, 113, False, 10, 60, True))

    def test_accents_do_not_add_replacement_characters(self):
        accented, plain = SpotifyView(), SpotifyView()
        track = {'id': 'one', 'title': 'Été', 'artist': '', 'is_playing': True}
        accented.frame(track, 100, True, 10, 60, True)
        plain.frame({**track, 'title': 'Ete'}, 100, True, 10, 60, True)
        self.assertEqual(accented.title_image.tobytes(), plain.title_image.tobytes())


class SpotifyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spotify = SpotifyClient(Path(self.directory.name) / 'spotify.json', 8080)
        self.state = meter.State()
        self.http = TestClient(TestServer(meter.make_app(meter.LedDisplay(''), self.state, self.spotify)))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        admin = patch.object(meter, 'ADMIN_TOKEN', 'admin')
        admin.start()
        self.addCleanup(admin.stop)

    async def test_routes_protect_credentials_and_expose_only_status(self):
        for route in ('/api/spotify/start', '/api/spotify/disconnect'):
            response = await self.http.post(route, json={})
            self.assertEqual(response.status, 403)
        response = await self.http.post('/api/spotify/start', json={'token': 'admin', 'client_id': 'a' * 32})
        self.assertEqual(response.status, 200)
        query = parse_qs(urlparse((await response.json())['url']).query)
        with patch('spotify_meter.requests.post', return_value=Mock(status_code=200, json=Mock(return_value={
                'access_token': 'private-access', 'refresh_token': 'private-refresh', 'expires_in': 3600}))):
            response = await self.http.get('/spotify/callback', params={'code': 'private-code', 'state': query['state'][0]},
                                           allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/#spotify')
        response = await self.http.get('/api/state')
        body = await response.json()
        self.assertTrue(body['spotify']['connected'])
        self.assertNotIn('private-', json.dumps(body))
        response = await self.http.post('/api/spotify/disconnect', json={'token': 'admin'})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.spotify.path.exists())

    async def test_invalid_callback_preserves_claude_mode_and_tokens(self):
        self.spotify.tokens = {'client_id': 'a' * 32, 'refresh_token': 'existing'}
        mode = meter.AUTH['mode']
        with patch('spotify_meter.requests.post') as post:
            response = await self.http.get('/spotify/callback?state=invalid&code=fake', allow_redirects=False)
        self.assertEqual(response.status, 302)
        post.assert_not_called()
        self.assertEqual(meter.AUTH['mode'], mode)
        self.assertTrue(self.spotify.status()['connected'])

    async def test_hidden_or_disabled_spotify_makes_no_requests(self):
        for visible, enabled in ((False, True), (True, False)):
            self.state.spotify_visible, self.state.spotify_enabled = visible, enabled
            fake = SimpleNamespace(status=lambda: {'connected': True}, current_track=Mock())
            with patch('spotify_meter.asyncio.sleep', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await poll_spotify(fake, self.state)
            fake.current_track.assert_not_called()

    async def test_stopping_spotify_during_a_poll_discards_inflight_track(self):
        self.state.spotify_visible = self.state.spotify_enabled = True
        def read_track():
            self.state.spotify_visible = False
            return {'id': 'discarded'}
        fake = SimpleNamespace(status=lambda: {'connected': True}, current_track=read_track)
        with patch('spotify_meter.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await poll_spotify(fake, self.state)
        self.assertIsNone(self.state.spotify_track)

    async def test_display_loop_returns_to_meter_after_song_timeout(self):
        st = self.state
        st.power = st.spotify_visible = st.spotify_enabled = True
        st.spotify_hold = 2
        st.spotify_track = {'id': 'one', 'title': 'Test', 'artist': '', 'is_playing': True,
                            'cover': Image.new('RGB', (16, 16), (10, 20, 30))}
        clock = [100]
        async def tick(_):
            clock[0] += 1
            if clock[0] >= 104:
                raise asyncio.CancelledError
        frames = []
        def save_frame(image):
            frames.append(image.copy())
            return 'test.png'
        display = SimpleNamespace(address='test', connected=True, send_image=AsyncMock())
        with patch.object(meter, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100)), \
                patch.object(meter, 'get_usage', return_value=(10, 20, '12:00')), \
                patch.object(meter, 'save_frame', side_effect=save_frame), \
                patch.object(meter.asyncio, 'sleep', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await meter.display_loop(display, st)
        self.assertEqual(frames[0].getpixel((0, 0)), (10, 20, 30))
        self.assertEqual(frames[-1].tobytes(), meter.render(10, 20, '10%').tobytes())


if __name__ == '__main__':
    unittest.main()

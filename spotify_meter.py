"""Optional Spotify sign-in, current track lookup and 96x16 rendering."""
import asyncio
import base64
import hashlib
import io
import json
import math
import os
import re
import secrets
import threading
import time
import unicodedata
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont


class SpotifyError(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = retry_after


class SpotifyClient:
    def __init__(self, path, port):
        self.path = Path(path)
        self.redirect_uri = f'http://127.0.0.1:{port}/spotify/callback'
        self.tokens = {}
        self.pending = None
        self.retry_at = 0
        self.lock = threading.RLock()
        self.cover_url, self.cover = '', None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                self.tokens = data
        except (OSError, ValueError):
            pass

    def status(self):
        return {'connected': bool(self.tokens.get('refresh_token')),
                'client_id': self.tokens.get('client_id', ''),
                'redirect_uri': self.redirect_uri}

    def _wait_limit(self):
        remaining = math.ceil(self.retry_at - time.monotonic())
        if remaining > 0:
            raise SpotifyError(f'Spotify demande de patienter encore {remaining} s.', remaining)

    def _check(self, response):
        if response.status_code == 429:
            value = response.headers.get('Retry-After', '')
            try:
                delay = max(1, math.ceil(float(value)))
            except (TypeError, ValueError, OverflowError):
                try:
                    delay = max(1, math.ceil(parsedate_to_datetime(value).timestamp() - time.time()))
                except (TypeError, ValueError, OverflowError, AttributeError):
                    delay = 60
            self.retry_at = time.monotonic() + delay
            raise SpotifyError(f'Spotify limite les requêtes. Nouvelle tentative dans {delay} s.', delay)
        if response.status_code in (400, 401):
            raise SpotifyError('Connexion Spotify expirée ou refusée. Reconnecte Spotify.', 60)
        if response.status_code == 403:
            raise SpotifyError('Accès Spotify refusé. Vérifie le compte et les utilisateurs autorisés de ton application.', 300)
        if response.status_code not in (200, 204):
            raise SpotifyError(f'Spotify indisponible (HTTP {response.status_code}).')

    def begin(self, client_id):
        with self.lock:
            self._wait_limit()
            client_id = str(client_id or self.tokens.get('client_id', '')).strip()
            if not re.fullmatch(r'[0-9a-fA-F]{32}', client_id):
                raise SpotifyError('Renseigne le Client ID de ton application Spotify (32 caractères).')
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            self.pending = {'state': state, 'verifier': verifier, 'client_id': client_id,
                            'expires_at': time.monotonic() + 600}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            return 'https://accounts.spotify.com/authorize?' + urlencode({
                'client_id': client_id, 'response_type': 'code', 'redirect_uri': self.redirect_uri,
                'scope': 'user-read-currently-playing', 'state': state,
                'code_challenge_method': 'S256', 'code_challenge': challenge})

    def _save(self, data):
        tmp = self.path.with_suffix('.json.tmp')
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
        os.replace(tmp, self.path)
        self.tokens = data

    def _token_data(self, response, client_id, previous_refresh=''):
        self._check(response)
        data = response.json()
        access = data.get('access_token')
        refresh = data.get('refresh_token') or previous_refresh
        if not access or not refresh:
            raise SpotifyError('Réponse Spotify incomplète. La connexion précédente est conservée.')
        return {'client_id': client_id, 'access_token': access, 'refresh_token': refresh,
                'expires_at': time.time() + float(data.get('expires_in', 3600))}

    def finish(self, state, code, denied=False):
        with self.lock:
            flow = self.pending
            if not flow or not secrets.compare_digest(str(state), flow['state']):
                raise SpotifyError('Cette connexion Spotify est inconnue. Recommence depuis le panneau.')
            self.pending = None
            if flow['expires_at'] <= time.monotonic():
                raise SpotifyError('Le lien Spotify a expiré. Recommence depuis le panneau.')
            if denied or not code:
                raise SpotifyError('Connexion Spotify annulée. La connexion précédente est conservée.')
            self._wait_limit()
            response = requests.post('https://accounts.spotify.com/api/token', data={
                'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.redirect_uri,
                'client_id': flow['client_id'], 'code_verifier': flow['verifier']}, timeout=15)
            self._save(self._token_data(response, flow['client_id']))

    def disconnect(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.tokens, self.pending = {}, None
            self.cover_url, self.cover = '', None

    def _refresh(self):
        response = requests.post('https://accounts.spotify.com/api/token', data={
            'grant_type': 'refresh_token', 'refresh_token': self.tokens['refresh_token'],
            'client_id': self.tokens['client_id']}, timeout=15)
        self._save(self._token_data(response, self.tokens['client_id'], self.tokens['refresh_token']))

    def current_track(self):
        with self.lock:
            if not self.status()['connected']:
                return None
            self._wait_limit()
            if self.tokens.get('expires_at', 0) <= time.time() + 60:
                self._refresh()
            def read():
                return requests.get('https://api.spotify.com/v1/me/player/currently-playing',
                                    headers={'Authorization': 'Bearer ' + self.tokens['access_token']}, timeout=10)
            response = read()
            if response.status_code == 401:
                self._refresh()
                response = read()
            self._check(response)
            if response.status_code == 204:
                return None
            data = response.json()
            item = data.get('item') or {}
            if not item.get('id') or item.get('type', 'track') != 'track':
                return None
            covers = (item.get('album') or {}).get('images') or []
            cover_url = covers[-1].get('url', '') if covers else ''
            if cover_url != self.cover_url:
                self.cover_url, self.cover = cover_url, fetch_cover(cover_url)
            return {'id': item['id'], 'title': str(item.get('name', ''))[:300],
                    'artist': ', '.join(str(a.get('name', '')) for a in item.get('artists', []))[:300],
                    'is_playing': bool(data.get('is_playing')), 'cover': self.cover}


def fetch_cover(url):
    # Artwork comes from Spotify's API, never from a submitted panel URL.
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'i.scdn.co':
        return None
    try:
        with requests.get(url, timeout=10, stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                return None
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 5_000_000:
                    return None
        with Image.open(io.BytesIO(content)) as image:
            return image.convert('RGB').resize((16, 16), Image.Resampling.NEAREST)
    except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError):
        return None


async def poll_spotify(client, st):
    while True:
        delay = 5
        if st.spotify_visible and st.spotify_enabled and client.status()['connected']:
            try:
                track = await asyncio.to_thread(client.current_track)
                if st.spotify_visible and st.spotify_enabled and client.status()['connected']:
                    st.spotify_track, st.spotify_error = track, ''
            except SpotifyError as error:
                st.spotify_track, st.spotify_error = None, str(error)
                delay = error.retry_after
            except (requests.RequestException, OSError, ValueError, KeyError):
                st.spotify_track = None
                st.spotify_error = 'Spotify ne répond pas. Nouvelle tentative automatique.'
                delay = 30
        else:
            st.spotify_track = None
        await asyncio.sleep(max(1, delay))


class SpotifyView:
    def __init__(self):
        self.last_id, self.started = None, 0
        self.title_image, self.title_key = None, None

    def frame(self, track, now, enabled, hold, speed, show_artist):
        if not enabled:
            self.last_id = None
            return None
        if not track or not track.get('is_playing'):
            return None
        if track['id'] != self.last_id:
            self.last_id, self.started = track['id'], now
        if now - self.started >= hold:
            return None
        title = track['title'] + (' - ' + track['artist'] if show_artist and track.get('artist') else '')
        if title != self.title_key:
            self.title_key = title
            title = ''.join(c for c in unicodedata.normalize('NFKD', title) if not unicodedata.combining(c))
            title = title.translate(str.maketrans({'’': "'", '–': '-', '—': '-', '…': '...'}))
            title = title.encode('ascii', 'replace').decode()
            font = ImageFont.load_default(size=10)
            box = font.getbbox(title)
            self.title_image = Image.new('RGB', (max(1, box[2] - box[0]), 16))
            ImageDraw.Draw(self.title_image).text((-box[0], (16 - box[3] + box[1]) // 2 - box[1]),
                                                 title, font=font, fill=(208, 208, 208))
        frame = Image.new('RGB', (96, 16))
        if track.get('cover') is not None:
            frame.paste(track['cover'], (0, 0))
        zone = Image.new('RGB', (78, 16))
        loop = self.title_image.width + 90
        x = 78 - int((now - self.started) * speed) % loop
        zone.paste(self.title_image, (x, 0))
        zone.paste(self.title_image, (x + loop, 0))
        frame.paste(zone, (18, 0))
        return frame

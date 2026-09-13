import contextlib
import io
import http.client
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import twitch_m3u as app


class Response(io.BytesIO):
    def __init__(self, body, url, status=200, headers=None):
        super().__init__(body)
        self.url, self.status = url, status
        self.headers = headers or {}

    def geturl(self):
        return self.url


class FakeResponse(io.BytesIO):
    """Stand-in for http.client.HTTPResponse."""
    def __init__(self, status=200, body=b'', headers=None, will_close=False):
        super().__init__(body)
        self.status, self.reason, self.will_close = status, 'reason', will_close
        self.msg = http.client.HTTPMessage()
        for k, v in (headers or {}).items():
            self.msg[k] = v
        self._closed = False

    def read(self, amt=None):
        data = super().read(amt)
        if amt is None or len(data) < amt or self.tell() == len(self.getvalue()):
            self._closed = True
        return data

    def isclosed(self):
        return self._closed


class FakeConn:
    """Scripted http.client.HTTPSConnection: each new connection pops the next
    item off `script`; an exception instance is raised on getresponse()."""
    script = []
    instances = []

    def __init__(self, host, port=None, timeout=None):
        self.host, self.port, self.timeout, self.sock = host, port, timeout, None
        self.requests, self.closed = [], False
        FakeConn.instances.append(self)

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, headers))
        self.body = body

    def getresponse(self):
        item = FakeConn.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True

    @classmethod
    @contextlib.contextmanager
    def install(cls, *script):
        cls.script, cls.instances = list(script), []
        app._pool.clear()
        with patch.object(app, '_Connection', cls):
            yield cls
        app._pool.clear()


class ResolverTests(unittest.TestCase):
    def setUp(self):
        app._dns.clear()

    def test_ipv4_only_and_stale_answer_survives_a_failed_lookup(self):
        v4 = [(2, 1, 6, '', ('1.2.3.4', 443)), (2, 1, 6, '', ('1.2.3.4', 443)), (2, 1, 6, '', ('5.6.7.8', 443))]
        with patch.object(app.socket, 'getaddrinfo', return_value=v4) as gai:
            self.assertEqual(app._resolve4('a.ttvnw.net'), ['1.2.3.4', '5.6.7.8'])
            self.assertEqual(gai.call_args.args[2], app.socket.AF_INET)
            self.assertEqual(app._resolve4('a.ttvnw.net'), ['1.2.3.4', '5.6.7.8'])
            self.assertEqual(gai.call_count, 1)                    # fresh answer reused
        app._dns['a.ttvnw.net'] = (app.time.monotonic() - app._DNS_FRESH - 1, ['1.2.3.4'])
        with patch.object(app.socket, 'getaddrinfo', side_effect=app.socket.gaierror('lost')):
            self.assertEqual(app._resolve4('a.ttvnw.net'), ['1.2.3.4'])  # stale beats failing
            with self.assertRaises(OSError):
                app._resolve4('never-seen.ttvnw.net')

    def test_cold_lookup_is_retried_before_giving_up(self):
        v4 = [(2, 1, 6, '', ('1.2.3.4', 443))]
        with patch.object(app.socket, 'getaddrinfo', side_effect=[app.socket.gaierror('lost'), v4]) as gai, \
                patch.object(app.time, 'sleep') as sleep:
            self.assertEqual(app._resolve4('cold.ttvnw.net'), ['1.2.3.4'])
            self.assertEqual(gai.call_count, 2)
            sleep.assert_called_once()
        with patch.object(app.socket, 'getaddrinfo', side_effect=app.socket.gaierror('lost')) as gai, \
                patch.object(app.time, 'sleep'):
            with self.assertRaises(OSError):
                app._resolve4('gone.ttvnw.net')
            self.assertEqual(gai.call_count, app._DNS_ATTEMPTS)

    def test_connect_tries_each_address(self):
        calls = []
        def create(addr, timeout=None, source_address=None):
            calls.append(addr)
            if addr[0] == '1.2.3.4':
                raise ConnectionRefusedError()
            return 'sock'
        with patch.object(app, '_resolve4', return_value=['1.2.3.4', '5.6.7.8']), \
                patch.object(app.socket, 'create_connection', side_effect=create):
            self.assertEqual(app._connect4(('a.ttvnw.net', 443), 5), 'sock')
        self.assertEqual(calls, [('1.2.3.4', 443), ('5.6.7.8', 443)])


class PoolTests(unittest.TestCase):
    def test_connection_is_reused_after_a_fully_read_response(self):
        with FakeConn.install(FakeResponse(body=b'#EXTM3U\n'), FakeResponse(body=b'#EXTM3U\n')) as fake:
            for _ in range(2):
                with app.open_media('https://a.ttvnw.net/p.m3u8?x=1') as r:
                    self.assertEqual(r.read(), b'#EXTM3U\n')
            self.assertEqual(len(fake.instances), 1)
            self.assertEqual([t for _, t, _ in fake.instances[0].requests], ['/p.m3u8?x=1'] * 2)
            self.assertEqual(fake.instances[0].requests[0][2]['User-Agent'], app.UA)

    def test_half_read_response_closes_instead_of_pooling(self):
        with FakeConn.install(FakeResponse(body=b'0123456789'), FakeResponse(body=b'x')) as fake:
            with app.open_media('https://a.ttvnw.net/seg.ts') as r:
                self.assertEqual(r.read(4), b'0123')
            self.assertTrue(fake.instances[0].closed)
            with app.open_media('https://a.ttvnw.net/seg.ts') as r:
                r.read()
            self.assertEqual(len(fake.instances), 2)

    def test_stale_pooled_connection_is_retried_once_on_a_fresh_one(self):
        with FakeConn.install(FakeResponse(body=b'a'), http.client.RemoteDisconnected(), FakeResponse(body=b'b')) as fake:
            with app.open_media('https://a.ttvnw.net/1') as r:
                r.read()
            with app.open_media('https://a.ttvnw.net/2') as r:
                self.assertEqual(r.read(), b'b')
            self.assertEqual(len(fake.instances), 2)
            self.assertTrue(fake.instances[0].closed)
        with FakeConn.install(http.client.RemoteDisconnected()):
            with self.assertRaises(http.client.HTTPException):
                app.open_media('https://a.ttvnw.net/3')

    def test_redirects_are_followed_validated_and_reported_as_final_url(self):
        with FakeConn.install(FakeResponse(302, headers={'Location': '/final/master.m3u8'}),
                              FakeResponse(body=b'#EXTM3U\n')) as fake:
            with app.open_media('https://a.ttvnw.net/start') as r:
                self.assertEqual(r.geturl(), 'https://a.ttvnw.net/final/master.m3u8')
                self.assertEqual(r.read(), b'#EXTM3U\n')
            self.assertEqual(len(fake.instances), 1)         # same host, connection reused

    def test_upstream_errors_raise_http_error_with_headers(self):
        with FakeConn.install(FakeResponse(416, headers={'Content-Range': 'bytes */10'})) as fake:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                app.open_media('https://a.ttvnw.net/seg.ts')
            self.assertEqual(cm.exception.code, 416)
            self.assertEqual(cm.exception.headers.get('Content-Range'), 'bytes */10')
            self.assertTrue(fake.instances[0].closed)

    def test_connection_close_responses_are_not_pooled(self):
        with FakeConn.install(FakeResponse(body=b'a', will_close=True), FakeResponse(body=b'b')) as fake:
            for _ in range(2):
                with app.open_media('https://a.ttvnw.net/x') as r:
                    r.read()
            self.assertEqual(len(fake.instances), 2)


class ProxyTests(unittest.TestCase):
    def test_variants_with_relative_urls_crlf_and_reordered_attributes(self):
        body = ('#EXTM3U\r\n'
                '#EXT-X-MEDIA:NAME="720p, HD",TYPE=VIDEO,GROUP-ID="720p"\r\n'
                '#EXT-X-STREAM-INF:CODECS="avc1,mp4a",VIDEO="720p",BANDWIDTH=1000,RESOLUTION=1280x720\r\n'
                '# a comment\r\n\r\n../video/720.m3u8?token=x\r\n'
                '#EXT-X-STREAM-INF:VIDEO="audio_only",BANDWIDTH=100\r\n'
                '//audio.ttvnw.net/audio.m3u8\r\n')
        base = 'https://video.ttvnw.net/live/master.m3u8'
        parsed = app.variants(body, base)
        self.assertEqual(parsed[0]['name'], '720p, HD')
        self.assertEqual(parsed[0]['url'], 'https://video.ttvnw.net/video/720.m3u8?token=x')
        self.assertEqual(app.pick_variant(body, '720', base), parsed[0]['url'])
        self.assertEqual(app.pick_variant(body, 'audio', base), 'https://audio.ttvnw.net/audio.m3u8')

    def test_resolve_uses_final_master_location(self):
        body = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvideo.m3u8\n'
        with patch.object(app, 'master_playlist', return_value=('https://a.ttvnw.net/redirect/master.m3u8', body)):
            self.assertEqual(app.resolve('test'), 'https://a.ttvnw.net/redirect/video.m3u8')

    def test_manifest_decoding_rejects_invalid_or_oversized_responses(self):
        self.assertEqual(app.decode_manifest(b'\xef\xbb\xbf#EXTM3U\r\n'), '#EXTM3U\n')
        for data in (b'<html>error</html>', b'#EXTM3U-not-a-header\n', b'#EXTM3U\n\xff'):
            with self.subTest(data=data), self.assertRaises(app.TwitchError):
                app.decode_manifest(data)
        with patch.object(app, 'MAX_MANIFEST_BYTES', 7), self.assertRaises(app.TwitchError):
            app.decode_manifest(b'#EXTM3U\n')

    def test_url_rewriting_preserves_quoted_commas_and_custom_attributes(self):
        body = '#EXTM3U\n#EXT-X-MEDIA:NAME="audio, commentary",X-URI="keep",URI="audio.m3u8?x=a,b"\n'
        result = app.rewrite_media_urls(body, 'https://a.ttvnw.net/live/index.m3u8', lambda u: 'local?u=' + urllib.parse.quote(u, safe=''))
        self.assertIn('NAME="audio, commentary",X-URI="keep",URI="local?', result)
        self.assertIn('audio.m3u8%3Fx%3Da%2Cb', result)

    def test_gql_does_not_inherit_environment_proxy(self):
        with patch.dict(app.os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:3128'}), \
                FakeConn.install(FakeResponse(body=b'{"data":{}}')) as fake:
            self.assertEqual(app._post_gql({'query': 'test'}), {'data': {}})
            self.assertEqual((fake.instances[0].host, fake.instances[0].port), ('gql.twitch.tv', 443))
            method, target, headers = fake.instances[0].requests[0]
            self.assertEqual((method, target), ('POST', '/gql'))
            self.assertEqual(headers['Client-ID'], app.CLIENT_ID)
            self.assertEqual(headers['X-Device-Id'], app._DEVICE_ID)
            self.assertEqual(fake.instances[0].body, b'{"query": "test"}')

    def test_player_types_are_parsed_with_platform_defaults(self):
        self.assertEqual(app._parse_player_types(''), app.DEFAULT_PLAYER_TYPES)
        self.assertEqual(app._parse_player_types(' popout/web, autoplay ,site, bad type!, mobile_feed/ios'),
                         (('popout', 'web'), ('autoplay', 'android'), ('site', 'web'), ('mobile_feed', 'ios')))

    def test_master_playlist_mints_the_channel_current_player_type(self):
        app._PLAYER_CURSOR.clear()
        with patch.object(app, 'PLAYER_TYPES', (('mobile_feed', 'android'), ('site', 'web'))), \
                patch.object(app, '_post_gql', return_value={'data': {'streamPlaybackAccessToken': {'value': 'tok', 'signature': 'sig'}}}) as gql, \
                patch.object(app, 'fetch_manifest', return_value=('https://usher.ttvnw.net/m.m3u8', '#EXTM3U\n')) as fetch:
            app.master_playlist('Test')
            variables = gql.call_args.args[0]['variables']
            self.assertEqual((variables['playerType'], variables['platform'], variables['login']), ('mobile_feed', 'android', 'test'))
            self.assertIn('query', gql.call_args.args[0])
            url = fetch.call_args.args[0]
            self.assertIn('play_session_id=', url)
            self.assertIn('device_id=' + app._DEVICE_ID, url)
            app.next_player_type('test')
            app.master_playlist('test')
            self.assertEqual(gql.call_args.args[0]['variables']['playerType'], 'site')
            # a different player type is a different session
            self.assertNotEqual(fetch.call_args_list[0].args[0], fetch.call_args_list[1].args[0])

    def test_all_hls_references(self):
        source = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"
#EXT-X-I-FRAME-STREAM-INF:URI="iframe.m3u8"
#EXT-X-KEY:METHOD=AES-128,URI="../key"
#EXT-X-MAP:URI="init.mp4"
#EXT-X-PART:DURATION=0.2,URI="part.mp4"
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="next.mp4"
#EXT-X-RENDITION-REPORT:URI="other.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000
video.m3u8
#EXTINF:6,
https://video.twitchcdn.net/segment.ts?token=abc
'''
        seen = []
        result = app.rewrite_media_urls(source, 'https://video.ttvnw.net/live/master.m3u8',
                                        lambda u: seen.append(u) or '/local')
        self.assertEqual(len(seen), 9)
        self.assertIn('https://video.ttvnw.net/key', seen)
        self.assertNotIn('https://', result)
        self.assertIn('#EXT-X-STREAM-INF:BANDWIDTH=1000', result)

    def test_reject_untrusted_targets_and_redirects(self):
        for url in ('http://video.ttvnw.net/a', 'https://127.0.0.1/a',
                    'https://ttvnw.net.evil.test/a', 'file:///etc/passwd',
                    'https://user@video.ttvnw.net/a', 'https://ttvnw.net:8080/a'):
            with self.subTest(url=url), self.assertRaises(app.TwitchError):
                app.validate_media_url(url)
        with FakeConn.install(FakeResponse(302, headers={'Location': 'https://127.0.0.1/private'})):
            with self.assertRaises(app.TwitchError):
                app.open_media('https://video.ttvnw.net/a')

    def test_signature_binds_entire_url(self):
        self.assertNotEqual(app.proxy_signature('https://a.ttvnw.net/a'),
                            app.proxy_signature('https://a.ttvnw.net/b'))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        class Handler(app.Handler):
            full_proxy = True
            access_key = 'test-key'
            cache = app._Cache()
        self.handler = Handler
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, path, headers=None):
        return urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {}))

    def media_path(self, url):
        return '/media?' + urllib.parse.urlencode(dict(url=url, sig=app.proxy_signature(url), key='test-key'))

    def test_auth_and_forged_links_never_fetch(self):
        with patch.object(app, 'open_media') as fetch:
            for path in ('/hls/test.m3u8', '/media?url=https://a.ttvnw.net/a&key=test-key&sig=bad'):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.get(path)
                self.assertEqual(ctx.exception.code, 403)
            fetch.assert_not_called()

    def test_master_nested_media_and_binary_range(self):
        master = 'https://usher.ttvnw.net/master.m3u8'
        variant = 'https://usher.ttvnw.net/video.m3u8'
        segment = 'https://usher.ttvnw.net/seg.ts'
        bodies = {
            master: b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvideo.m3u8\n',
            variant: b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:50\n#EXTINF:6,\nseg.ts\n',
            segment: b'\x00\xffmedia',
        }
        def fetch(url, headers):
            if url == segment:
                self.assertEqual(headers, {'Range': 'bytes=0-6'})
                return Response(bodies[url], url, 206, {'Content-Length': '7', 'Content-Range': 'bytes 0-6/20'})
            return Response(bodies[url], url)
        with patch.object(app, 'resolve', return_value=master), patch.object(app, 'open_media', side_effect=fetch):
            with self.get('/hls/test.m3u8?q=master&key=test-key') as r:
                body = r.read().decode()
                self.assertNotIn('MEDIA-SEQUENCE', body)
                local_variant = body.splitlines()[-1]
            with urllib.request.urlopen(local_variant) as r:
                body = r.read().decode()
                self.assertIn('MEDIA-SEQUENCE:0', body)
                local_segment = body.splitlines()[-1]
            with urllib.request.urlopen(urllib.request.Request(local_segment, headers={'Range': 'bytes=0-6'})) as r:
                self.assertEqual(r.status, 206)
                self.assertEqual(r.headers['Content-Range'], 'bytes 0-6/20')
                self.assertEqual(r.read(), bodies[segment])

    def test_redirect_routes_stay_proxied_and_vod_sequence_preserved(self):
        url = 'https://video.ttvnw.net/video.m3u8'
        manifest = b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:19\n#EXTINF:6,\na.ts\n#EXT-X-ENDLIST\n'
        with patch.object(app, 'resolve', return_value=url), patch.object(app, 'open_media', side_effect=lambda *a: Response(manifest, url)):
            for path in ('/live/test.m3u8', '/vod/123.m3u8'):
                with self.get(path + '?key=test-key') as r:
                    self.assertEqual(r.status, 200)
                    self.assertIsNone(r.headers.get('Location'))
                    self.assertIn('MEDIA-SEQUENCE:19', r.read().decode())

    def test_encrypted_live_sequence_preserved(self):
        url = 'https://video.ttvnw.net/encrypted.m3u8'
        manifest = b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:19\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:6,\na.ts\n'
        with patch.object(app, 'open_media', return_value=Response(manifest, url)):
            with self.get(self.media_path(url)) as r:
                body = r.read().decode()
                self.assertIn('MEDIA-SEQUENCE:19', body)
                self.assertNotIn('URI="key.bin"', body)

    def test_upstream_failure_does_not_redirect_or_expose_url(self):
        with patch.object(app, 'resolve', return_value='https://video.ttvnw.net/a?secret=hidden'), patch.object(app, 'open_media', side_effect=OSError('secret=hidden')):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get('/hls/test.m3u8?key=test-key')
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(ctx.exception.headers['Retry-After'], '3')
            self.assertNotIn(b'hidden', ctx.exception.read())
            self.assertIsNone(ctx.exception.headers.get('Location'))

    def test_stale_vpn_session_refreshes_once_and_preserves_sequence(self):
        old = 'https://a.ttvnw.net/live.m3u8?old=token'
        new = 'https://b.ttvnw.net/live.m3u8?new=token'
        body = b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:90\n#EXTINF:6,\nhttps://a.ttvnw.net/one.ts\n'
        app._NORMALISERS.clear()
        with patch.object(app, 'resolve', side_effect=[old, new]) as resolve, patch.object(app, 'open_media', side_effect=[
            Response(body, old),
            urllib.error.HTTPError(old, 403, 'expired', {}, None),
            Response(body.replace(b'one.ts', b'two.ts'), new),
        ]) as fetch:
            with self.get('/hls/test.m3u8?key=test-key') as r:
                self.assertIn('MEDIA-SEQUENCE:0', r.read().decode())
            with self.get('/hls/test.m3u8?key=test-key') as r:
                self.assertIn('MEDIA-SEQUENCE:1', r.read().decode())
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(fetch.call_count, 3)

    def test_transient_vpn_failure_does_not_refresh_tokens(self):
        url = 'https://a.ttvnw.net/live.m3u8'
        with patch.object(app, 'resolve', return_value=url) as resolve, patch.object(app, 'open_media', side_effect=urllib.error.HTTPError(url, 503, 'unavailable', {}, None)) as fetch:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get('/hls/test.m3u8?key=test-key')
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(ctx.exception.headers['Retry-After'], '3')
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(fetch.call_count, 1)

    def test_root_rejects_html_with_success_status(self):
        url = 'https://a.ttvnw.net/extensionless'
        with patch.object(app, 'resolve', return_value=url), patch.object(app, 'open_media', return_value=Response(b'<html>VPN gateway error</html>', url)):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get('/hls/test.m3u8?key=test-key')
            self.assertEqual(ctx.exception.code, 502)
            self.assertNotIn(b'VPN gateway', ctx.exception.read())

    def test_nested_bom_manifest_and_forwarded_origin(self):
        url = 'https://a.ttvnw.net/manifest'
        body = b'\xef\xbb\xbf#EXTM3U\r\n#EXT-X-MAP:URI="init.mp4"\r\n#EXTINF:6,\r\nsegment.ts\r\n'
        with patch.object(app, 'open_media', return_value=Response(body, 'https://b.ttvnw.net/final/master.m3u8')):
            with self.get(self.media_path(url), headers={'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'player.example'}) as r:
                result = r.read().decode()
            self.assertTrue(result.startswith('#EXTM3U\n'))
            segment = result.splitlines()[-1]
            self.assertTrue(segment.startswith('https://player.example/media?'))
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(segment).query)
            self.assertEqual(qs['url'], ['https://b.ttvnw.net/final/segment.ts'])

    def test_range_failure_preserves_content_range(self):
        url = 'https://a.ttvnw.net/seg.mp4'
        error = urllib.error.HTTPError(url, 416, 'range', {'Content-Range': 'bytes */10'}, None)
        with patch.object(app, 'open_media', side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(self.media_path(url), headers={'Range': 'bytes=20-'})
            self.assertEqual(ctx.exception.code, 416)
            self.assertEqual(ctx.exception.headers['Content-Range'], 'bytes */10')
            self.assertEqual(ctx.exception.read(), b'')

    def test_mid_segment_disconnect_does_not_append_error_document(self):
        class Interrupted(Response):
            def read(self, size=-1):
                if self.tell():
                    raise http.client.IncompleteRead(b'private upstream detail', 100)
                return super().read(size)
        url = 'https://a.ttvnw.net/segment.ts'
        response = Interrupted(b'x' * 100000, url, headers={'Content-Length': '100000'})
        with patch.object(app, 'open_media', return_value=response):
            with self.get(self.media_path(url)) as r:
                with self.assertRaises(http.client.IncompleteRead) as ctx:
                    r.read()
                self.assertEqual(ctx.exception.partial, b'x' * (64 * 1024))

    def test_if_range_forwarded_for_segments_but_not_manifests(self):
        segment = 'https://a.ttvnw.net/segment.ts'
        with patch.object(app, 'open_media', return_value=Response(b'data', segment)) as fetch:
            with self.get(self.media_path(segment), headers={'Range': 'bytes=0-3', 'If-Range': '"etag"'}) as r:
                r.read()
            self.assertEqual(fetch.call_args.args[1], {'Range': 'bytes=0-3', 'If-Range': '"etag"'})
        manifest = 'https://a.ttvnw.net/master.m3u8'
        with patch.object(app, 'open_media', return_value=Response(b'#EXTM3U\n', manifest)) as fetch:
            with self.get(self.media_path(manifest), headers={'Range': 'bytes=0-3', 'If-Range': '"etag"'}) as r:
                self.assertEqual(r.read(), b'#EXTM3U\n')
            self.assertEqual(fetch.call_args.args[1], {})

    def test_byte_range_manifest_sequence_is_preserved(self):
        url = 'https://a.ttvnw.net/live.m3u8'
        body = b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:19\n#EXTINF:6,\n#EXT-X-BYTERANGE:10@0\nseg.mp4\n#EXTINF:6,\n#EXT-X-BYTERANGE:10\nseg.mp4\n'
        with patch.object(app, 'open_media', return_value=Response(body, url)):
            with self.get(self.media_path(url)) as r:
                result = r.read().decode()
                self.assertIn('MEDIA-SEQUENCE:19', result)
                self.assertIn('#EXT-X-BYTERANGE:10\n', result)

    def test_non_ascii_credentials_return_forbidden(self):
        for path in ('/help?key=%C3%A9', '/media?key=test-key&sig=%C3%A9'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 403)

    def test_query_secrets_are_redacted(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.get('/help?key=test-key') as r:
                r.read()
        self.assertNotIn('test-key', output.getvalue())

    STITCHED = (b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-01-01T00:00:00Z",DURATION=30\n'
                b'#EXTINF:2,Amazon|stitched\nhttps://video.ttvnw.net/ad1.ts\n')
    CLEAN = b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:900\n#EXTINF:2,live\nhttps://video.ttvnw.net/live1.ts\n'

    def _fresh_player_state(self):
        app._PLAYER_CURSOR.clear()
        app._AD_SEEN_EVERYWHERE.clear()

    def test_stitched_session_moves_channel_to_next_player_type(self):
        self._fresh_player_state()
        urls = ['https://video.ttvnw.net/mobile.m3u8', 'https://video.ttvnw.net/popout.m3u8']
        bodies = {urls[0]: self.STITCHED, urls[1]: self.CLEAN}
        for full_proxy in (True, False):
            self._fresh_player_state()
            self.handler.full_proxy = full_proxy
            self.handler.cache = app._Cache()
            with patch.object(app, 'PLAYER_TYPES', (('mobile_feed', 'android'), ('popout', 'web'), ('site', 'web'))), \
                    patch.object(app, 'resolve', side_effect=urls) as resolve, \
                    patch.object(app, 'open_media', side_effect=lambda url, *a, **k: Response(bodies[url], url)) as fetch:
                with self.get('/hls/test.m3u8?key=test-key') as r:
                    body = r.read().decode()
                self.assertNotIn('stitched', body)
                self.assertIn('live1.ts', body)
                self.assertEqual(resolve.call_count, 2)
                self.assertEqual(app.player_type_for('test'), ('popout', 'web'))
                self.assertEqual(self.handler.cache.get(('test', 'best')), urls[1])
                # the next reload stays on the clean session without re-resolving
                with self.get('/hls/test.m3u8?key=test-key') as r:
                    self.assertIn('live1.ts', r.read().decode())
                self.assertEqual(resolve.call_count, 2)
                self.assertEqual(fetch.call_count, 3)

    def test_every_player_type_stitched_rides_through_and_backs_off(self):
        self._fresh_player_state()
        self.handler.cache = app._Cache()
        urls = ['https://video.ttvnw.net/a.m3u8', 'https://video.ttvnw.net/b.m3u8']
        with patch.object(app, 'PLAYER_TYPES', (('mobile_feed', 'android'), ('site', 'web'))), \
                patch.object(app, 'resolve', side_effect=urls) as resolve, \
                patch.object(app, 'open_media', side_effect=lambda url, *a, **k: Response(self.STITCHED, url)):
            with self.get('/hls/test.m3u8?key=test-key') as r:
                body = r.read().decode()
            self.assertIn('MEDIA-SEQUENCE:0', body)          # served, renumbered, not stalled
            self.assertIn('ad1.ts', body)
            self.assertEqual(resolve.call_count, 2)
            with self.get('/hls/test.m3u8?key=test-key') as r:
                r.read()
            self.assertEqual(resolve.call_count, 2)          # parked: no token churn per reload

    def test_vod_never_switches_player_type(self):
        self._fresh_player_state()
        url = 'https://video.ttvnw.net/vod.m3u8'
        with patch.object(app, 'resolve', return_value=url) as resolve, \
                patch.object(app, 'open_media', return_value=Response(self.STITCHED + b'#EXT-X-ENDLIST\n', url)):
            with self.get('/vod/123.m3u8?key=test-key') as r:
                self.assertIn('ad1.ts', r.read().decode())
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(app._PLAYER_CURSOR, {})

    def test_default_mode_keeps_legacy_redirect(self):
        self.handler.full_proxy = False
        handler = object.__new__(self.handler)
        handler.send_response = lambda status: self.assertEqual(status, 302)
        headers = {}
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        with patch.object(app, 'resolve', return_value='https://video.ttvnw.net/a'):
            handler._redirect_stream('test', 'best')
        self.assertEqual(headers['Location'], 'https://video.ttvnw.net/a')


if __name__ == '__main__':
    unittest.main()

import contextlib
import io
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


class ProxyTests(unittest.TestCase):
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
        with self.assertRaises(app.TwitchError):
            app._MediaRedirect().redirect_request(None, None, 302, '', {},
                                                   'https://127.0.0.1/private')

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
            self.assertEqual(ctx.exception.code, 502)
            self.assertNotIn(b'hidden', ctx.exception.read())
            self.assertIsNone(ctx.exception.headers.get('Location'))

    def test_query_secrets_are_redacted(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.get('/help?key=test-key') as r:
                r.read()
        self.assertNotIn('test-key', output.getvalue())

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

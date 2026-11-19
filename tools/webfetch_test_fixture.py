#!/usr/bin/env python3
"""Serve a synthetic WebFetch fixture inside the 214 test VM."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import platform
import ssl
from urllib.parse import parse_qs, urlsplit


ADDRESS = '127.0.0.2'
SAFETY_HOST = 'api.anthropic.com'
FIXTURE_HOST = 'webfetch-fixture.test'
REDIRECT_HOST = 'redirect-target.webfetch-fixture.test'
FIXTURE_PAGE = b"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Safe Code WebFetch Fixture</title>
<h1>Safe Code WebFetch Fixture</h1>
<p>The calibration phrase is violet lantern 214.</p>
</html>
"""
REDIRECT_PAGE = b"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Safe Code WebFetch Redirect Target</title>
<p>The redirect calibration phrase is copper orchard 214.</p>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type, *, location=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if location is not None:
            self.send_header('Location', location)
        self.end_headers()
        self.wfile.write(body)

    def json_reply(self, status, result):
        self.reply(status, json.dumps(result).encode(), 'application/json')

    def do_GET(self):
        credential_headers = ('Authorization', 'X-Api-Key', 'Cookie', 'Proxy-Authorization')
        if any(header in self.headers for header in credential_headers):
            return self.json_reply(403, {'error': 'Fixture does not accept credentials'})
        host = self.headers.get('Host', '').removesuffix(':443').lower()
        if host == FIXTURE_HOST and self.path == '/':
            return self.reply(200, FIXTURE_PAGE, 'text/html; charset=utf-8')
        if host == FIXTURE_HOST and self.path == '/redirect':
            return self.reply(302, b'', 'text/plain', location=f'https://{REDIRECT_HOST}/landing')
        if host == REDIRECT_HOST and self.path == '/landing':
            return self.reply(200, REDIRECT_PAGE, 'text/html; charset=utf-8')
        if host == SAFETY_HOST:
            url = urlsplit(self.path)
            if url.path == '/api/web/domain_info' and not (url.scheme or url.netloc):
                query = parse_qs(url.query, keep_blank_values=True)
                if query in ({'domain': [FIXTURE_HOST]}, {'domain': [REDIRECT_HOST]}):
                    return self.json_reply(200, {'can_fetch': True})
                return self.json_reply(403, {'can_fetch': False})
        self.json_reply(404, {'error': 'Fixture route not found'})


def main():
    if platform.node() != 'tom666':
        raise SystemExit('The fixture is only for the 214 test VM')
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(credentials / 'tls.crt', credentials / 'tls.key')
    with ThreadingHTTPServer((ADDRESS, 443), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == '__main__':
    main()

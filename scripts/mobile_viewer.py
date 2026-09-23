#!/usr/bin/env python3
"""Read-only HTTP view of the existing bot snapshot. No trading imports or API calls."""
import argparse
import json
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icon.svg': ('icon.svg', 'image/svg+xml'),
}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, snapshot, **kwargs):
        self.snapshot = snapshot
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        status = 200
        if path == '/api/status':
            try:
                # The bot atomically replaces this file; never open its credentials/logs.
                payload = json.loads(self.snapshot.read_text())
                body = json.dumps(payload, allow_nan=False).encode()
            except (OSError, ValueError):
                status = 503
                body = b'{"error":"Bot snapshot unavailable"}'
            content_type = 'application/json'
        elif path in ASSETS:
            name, content_type = ASSETS[path]
            body = (ROOT / 'mobile' / name).read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Avoid writing a log entry every two-second status poll.
        if len(args) > 1 and str(args[1]) != '200':
            super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'reports/btc15_live_latest.json')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, snapshot=args.snapshot))
    print(f'Read-only viewer: http://{args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

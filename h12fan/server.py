import base64
import hmac
import json
import logging
import mimetypes
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .config import Store
from .controller import Controller
from .ipmi import DemoIPMI, IPMI, IPMIError, secret

STATIC = Path(__file__).parent / 'static'


def handler(controller, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not log URLs, headers, bodies or credentials.
            pass

        def respond(self, code, data, content_type='application/json'):
            body = json.dumps(data, allow_nan=False).encode() if content_type == 'application/json' else data
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def authorised(self):
            if token:
                expected = 'Basic ' + base64.b64encode(('admin:' + token).encode()).decode()
                if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Basic realm="H12Fan", charset="UTF-8"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return False
            return True

        def do_GET(self):
            if not self.authorised():
                return
            path = urlsplit(self.path).path
            if path == '/api/state':
                self.respond(200, controller.snapshot())
            elif path == '/healthz':
                self.respond(200, {'status': 'ok'})
            elif path in ['/', '/app.js', '/style.css']:
                file = STATIC / ('index.html' if path == '/' else path[1:])
                self.respond(200, file.read_bytes(), mimetypes.guess_type(file)[0] or 'text/plain')
            else:
                self.respond(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.authorised():
                return
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                self.respond(403, {'error': 'Cross-origin request rejected'})
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.respond(415, {'error': 'Expected application/json'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('Request must be 1–65536 bytes')
                self.connection.settimeout(20)
                data = json.loads(self.rfile.read(length))
                path = urlsplit(self.path).path
                if path == '/api/config':
                    controller.update(data)
                elif path in ['/api/thresholds/apply', '/api/thresholds/capture']:
                    if not isinstance(data, dict) or not isinstance(data.get('fan'), str):
                        raise ValueError('Fan name required')
                    controller.apply_threshold(data['fan'], path.endswith('capture'))
                elif path == '/api/demo' and controller.ipmi.demo:
                    if data.get('power') not in ['on', 'off', 'reset']:
                        raise ValueError('Invalid demo action')
                    with controller.lock:
                        if data['power'] == 'reset':
                            for name in controller.ipmi.lowers:
                                controller.ipmi.lowers[name] = [700, 1000, 1200]
                            controller.ipmi.fan_mode = 0
                            controller.last_threshold = None
                        else:
                            controller.ipmi.power_state = data['power']
                else:
                    self.respond(404, {'error': 'Not found'})
                    return
                self.respond(200, {'ok': True})
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})
            except IPMIError as exc:
                self.respond(502, {'error': str(exc)})
            except Exception:
                logging.exception('API operation failed')
                self.respond(500, {'error': 'Operation failed; check service logs'})

    return Handler


def main():
    demo = os.getenv('H12FAN_DEMO', 'true').lower() == 'true'
    token = secret('H12FAN_WEB_PASSWORD')
    data_dir = Path(os.getenv('H12FAN_DATA', './data'))
    # Never carry simulated enabled control into a live deployment.
    controller = Controller(DemoIPMI() if demo else IPMI(), Store(data_dir / 'demo' if demo else data_dir))
    server = ThreadingHTTPServer((os.getenv('H12FAN_BIND', '127.0.0.1'), int(os.getenv('PORT', '8080'))), handler(controller, token))
    server.daemon_threads = True
    worker = threading.Thread(target=controller.run, daemon=True)
    worker.start()

    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'H12Fan listening on http://{server.server_address[0]}:{server.server_address[1]} ({"demo" if demo else "live"})', flush=True)
    try:
        server.serve_forever()
    finally:
        controller.shutdown()
        worker.join(timeout=15)
        server.server_close()


if __name__ == '__main__':
    main()

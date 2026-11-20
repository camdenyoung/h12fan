import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from h12fan.config import Store
from h12fan.controller import Controller
from h12fan.ipmi import DemoIPMI
from h12fan.server import handler, main


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.controller = Controller(DemoIPMI(),Store(cls.tmp.name))
        cls.controller.config['startup_seconds']=0
        cls.controller.tick()
        cls.server = ThreadingHTTPServer(('127.0.0.1',0), handler(cls.controller,'test-password-long'))
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def request(self,path,data=None,auth=True,**headers):
        if auth:
            headers['Authorization']='Basic '+base64.b64encode(b'admin:test-password-long').decode()
        if data is not None:
            headers.setdefault('Content-Type','application/json')
        req=Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        try:
            response=urlopen(req,timeout=3)
        except HTTPError as exc:
            response=exc
        return response.status,response.read(),response.headers

    def test_auth_required_for_assets_and_api(self):
        for path in ['/','/app.js','/api/state','/healthz']:
            self.assertEqual(self.request(path,auth=False)[0],401)

    def test_dashboard_and_security_headers(self):
        code,body,headers=self.request('/')
        self.assertEqual(code,200)
        self.assertIn(b'H12Fan',body)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('/app.js')[0],200)
        self.assertEqual(self.request('/style.css')[0],200)
        self.assertEqual(self.request('/../config.json')[0],404)

    def test_state_has_no_credentials(self):
        code,body,_=self.request('/api/state')
        self.assertEqual(code,200)
        self.assertNotIn(b'test-password-long',body)
        state=json.loads(body)
        self.assertEqual(state['power'],'on')
        self.assertTrue(state['demo'])

    def test_reject_cross_origin_and_non_json_writes(self):
        self.assertEqual(self.request('/api/config',{},Origin='https://other.example')[0],403)
        self.assertEqual(self.request('/api/config',{},**{'Content-Type':'text/plain'})[0],415)

    def test_validation_errors_are_actionable(self):
        code,body,_=self.request('/api/config',{})
        self.assertEqual(code,400)
        self.assertIn(b'configuration',body)

    def test_config_roundtrip_and_threshold_actions(self):
        config=self.controller.snapshot()['config']
        config['hysteresis']=4
        self.assertEqual(self.request('/api/config',config)[0],200)
        self.assertEqual(self.controller.config['hysteresis'],4)
        self.assertEqual(self.request('/api/thresholds/capture',{'fan':'FAN1'})[0],200)
        self.assertEqual(self.request('/api/thresholds/apply',{'fan':'FAN1'})[0],200)
        self.assertEqual(self.controller.ipmi.lowers['FAN1'],[400,800,1000])


class OptionalAuthenticationTests(unittest.TestCase):
    def test_blank_password_allows_reads_and_configuration_without_login(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(DemoIPMI(), Store(directory))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(controller, ''))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                for path in ['/', '/app.js', '/api/state', '/healthz']:
                    with urlopen(url + path, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIsNone(response.headers.get('WWW-Authenticate'))
                config = controller.snapshot()['config']
                config['hysteresis'] = 5
                request = Request(url + '/api/config', data=json.dumps(config).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(controller.config['hysteresis'], 5)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_live_startup_accepts_empty_and_short_passwords(self):
        for password in ['', 'x']:
            with self.subTest(password=password), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {'H12FAN_DEMO': 'false', 'H12FAN_DATA': directory}, clear=True), \
                     patch('h12fan.server.secret', return_value=password), \
                     patch('h12fan.server.IPMI', return_value=DemoIPMI()) as ipmi, \
                     patch('h12fan.server.ThreadingHTTPServer') as server, \
                     patch('h12fan.server.threading.Thread'), \
                     patch('h12fan.server.signal.signal'), \
                     patch('builtins.print'):
                    main()
                    ipmi.assert_called_once()
                    server.return_value.serve_forever.assert_called_once()

    def test_short_password_still_requires_correct_login(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(DemoIPMI(), Store(directory))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(controller, 'x'))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/api/state'
                for value in [None, 'admin:wrong']:
                    request = Request(url)
                    if value is not None:
                        request.add_header('Authorization', 'Basic ' + base64.b64encode(value.encode()).decode())
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=3)
                    self.assertEqual(caught.exception.code, 401)
                request = Request(url, headers={'Authorization': 'Basic ' + base64.b64encode(b'admin:x').decode()})
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

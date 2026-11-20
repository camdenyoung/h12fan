import base64
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from h12fan.config import Store
from h12fan.controller import Controller
from h12fan.ipmi import DemoIPMI
from h12fan.server import handler


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

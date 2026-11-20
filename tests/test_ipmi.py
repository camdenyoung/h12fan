import os
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess, TimeoutExpired
from h12fan.ipmi import IPMI, IPMIError, parse_sensors


class Tests(unittest.TestCase):
    def test_parse_real_sensor_format(self):
        rows=parse_sensors('CPU Temp | 42.000 | degrees C | ok | na | na | na | 85 | 90 | 95\nFAN1 | 1200.000 | RPM | ok | 400.000 | 800.000 | 1000.000 | na | na | na\nFANA | na | RPM | ns | na | na | na | na | na | na')
        self.assertEqual(rows[0]['value'],42)
        self.assertEqual(rows[1]['lower'],[400,800,1000])
        self.assertIsNone(rows[2]['value'])
        self.assertEqual(parse_sensors('garbage'),[])

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_password_is_environment_only(self,run):
        run.return_value=CompletedProcess([],0,'Chassis Power is on','')
        self.assertEqual(IPMI().power(),'on')
        args,kwargs=run.call_args
        self.assertNotIn('secret-value',args[0])
        self.assertIn('-E',args[0])
        self.assertEqual(kwargs['env']['IPMI_PASSWORD'],'secret-value')
        self.assertNotIn('shell',kwargs)

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_failure_output_redacted(self,run):
        run.return_value=CompletedProcess([],1,'','secret-value')
        with self.assertRaises(IPMIError) as caught: IPMI().power()
        self.assertNotIn('secret-value',str(caught.exception))

import os
import unittest
import time
from pathlib import Path
from h12fan.sdr import FanScale
from test_sdr import fan_record
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

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'}, clear=True)
    @patch('h12fan.ipmi.subprocess.run')
    def test_password_is_environment_only(self,run):
        run.return_value=CompletedProcess([],0,'Chassis Power is on','')
        self.assertEqual(IPMI().power(),'on')
        args,kwargs=run.call_args
        self.assertNotIn('secret-value',args[0])
        self.assertIn('-E',args[0])
        self.assertEqual(args[0][args[0].index('-I')+1],'lanplus')
        self.assertEqual(args[0][args[0].index('-C')+1],'3')
        self.assertEqual(kwargs['env']['IPMI_PASSWORD'],'secret-value')
        self.assertNotIn('shell',kwargs)

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_failure_output_redacted(self,run):
        run.return_value=CompletedProcess([],1,'','RAKP 2 HMAC is invalid; user secret-value')
        with self.assertRaises(IPMIError) as caught: IPMI().power()
        self.assertNotIn('secret-value',str(caught.exception))
        self.assertNotIn(' user ',str(caught.exception))
        self.assertIn('chassis power status',str(caught.exception))
        self.assertIn('cipher 3',str(caught.exception))
        self.assertIn('RAKP 2 HMAC is invalid',str(caught.exception))

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_failed_sensor_operation_uses_stdout_when_stderr_empty(self,run):
        run.return_value=CompletedProcess([],1,'Unable to read SDR: secret-value','')
        with self.assertRaises(IPMIError) as caught: IPMI().sensors()
        self.assertIn('sensor list',str(caught.exception))
        self.assertIn('Unable to read SDR',str(caught.exception))
        self.assertNotIn('secret-value',str(caught.exception))

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_timeout_names_operation_without_subprocess_details(self,run):
        run.side_effect=TimeoutExpired(['secret-value'],12,stderr='secret-value')
        with self.assertRaises(IPMIError) as caught: IPMI().power()
        self.assertIn('chassis power status',str(caught.exception))
        self.assertIn('12 seconds',str(caught.exception))
        self.assertNotIn('secret-value',str(caught.exception))

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value', 'IPMI_INTERFACE':'lan'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_lan_matches_host_connection_without_lanplus_cipher_option(self,run):
        run.return_value=CompletedProcess([],0,'temperature output','')
        self.assertEqual(IPMI().run('sdr','type','Temperature'),'temperature output')
        command=run.call_args.args[0]
        self.assertEqual(command[command.index('-I')+1],'lan')
        self.assertNotIn('-C',command)
        self.assertEqual(command[-3:],['sdr','type','Temperature'])
        self.assertNotIn('secret-value',command)

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value', 'IPMI_INTERFACE':'lanplus', 'IPMI_CIPHER':'17'})
    @patch('h12fan.ipmi.subprocess.run')
    def test_lanplus_uses_configured_cipher(self,run):
        run.return_value=CompletedProcess([],0,'Chassis Power is on','')
        self.assertEqual(IPMI().power(),'on')
        command=run.call_args.args[0]
        self.assertEqual(command[command.index('-I')+1],'lanplus')
        self.assertEqual(command[command.index('-C')+1],'17')

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value', 'IPMI_INTERFACE':'open'})
    def test_unsupported_interface_rejected(self):
        with self.assertRaisesRegex(ValueError,'IPMI_INTERFACE'):
            IPMI()


    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    def test_h12_metadata_loaded_once_and_expected_values_rounded(self):
        ipmi=IPMI()
        def dump(*args):
            self.assertEqual(args[:2],('sdr','dump'))
            Path(args[2]).write_bytes(fan_record())
            return ''
        with patch.object(ipmi,'run',side_effect=dump) as run:
            for _ in range(2):
                self.assertEqual(ipmi.expected_lower('FAN1',[400,800,1000],[420,840,None]),[420,840,None])
            run.assert_called_once()

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    def test_partial_threshold_writes_skip_lnc_and_verify_rounded_readback(self):
        ipmi=IPMI()
        ipmi._fan_scales={'FAN1':FanScale(140,0,0,0)}
        ipmi._scale_checked=time.monotonic()
        before={'name':'FAN1','value':1680,'lower':[560,1120,None]}
        after={'name':'FAN1','value':1680,'lower':[420,840,None]}
        with patch.object(ipmi,'sensors',side_effect=[[before],[after]]), patch.object(ipmi,'run') as run:
            self.assertEqual(ipmi.thresholds('FAN1',[400,800,1000]),[420,840,None])
        self.assertEqual([c.args for c in run.call_args_list],[('sensor','thresh','FAN1','lnr',400),('sensor','thresh','FAN1','lcr',800)])

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    def test_rounded_targets_do_not_hide_a_failed_write(self):
        ipmi=IPMI()
        ipmi._fan_scales={'FAN1':FanScale(140,0,0,0)}
        ipmi._scale_checked=time.monotonic()
        before={'name':'FAN1','value':1680,'lower':[560,1120,None]}
        with patch.object(ipmi,'sensors',return_value=[before]), patch.object(ipmi,'run'):
            with self.assertRaisesRegex(IPMIError,'read-back mismatch'):
                ipmi.thresholds('FAN1',[400,800,1000])

    @patch.dict(os.environ,{'IPMI_HOST':'192.0.2.1','IPMI_USER':'user','IPMI_PASSWORD':'secret-value'})
    def test_missing_scale_does_not_guess_quantisation(self):
        ipmi=IPMI()
        with patch.object(ipmi,'run',side_effect=IPMIError('dump unsupported')):
            self.assertEqual(ipmi.expected_lower('FAN1',[400,800,1000],[420,840,None]),[400,800,None])

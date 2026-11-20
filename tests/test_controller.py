import copy
import tempfile
from pathlib import Path
from h12fan.ipmi import parse_sensors
from h12fan.sdr import FanScale, masked_lower
import unittest
from unittest.mock import patch
from h12fan.config import Store, defaults, validate
from h12fan.controller import Controller, curve_duty, fan_unavailable_reason
from h12fan.ipmi import DemoIPMI, IPMIError


class RecordingIPMI(DemoIPMI):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.missing = False
        self.online = True
        self.reject = False

    def power(self):
        if not self.online:
            raise IPMIError('unreachable')
        return super().power()

    def sensors(self):
        rows = super().sensors()
        if self.missing:
            rows[0]['value'] = None
        return rows

    def set_duty(self, zone, duty):
        self.writes.append((zone, duty))
        super().set_duty(zone, duty)

    def thresholds(self, name, values):
        if self.reject:
            raise IPMIError('write rejected')
        return super().thresholds(name, values)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ipmi = RecordingIPMI()
        self.now = 100
        self.controller = Controller(self.ipmi, Store(self.tmp.name), lambda: self.now)
        self.controller.config['startup_seconds'] = 0

    def enable(self):
        self.controller.config.update(enabled=True)

    def test_step_boundaries_hysteresis_and_critical(self):
        z = defaults()['zones'][0]
        self.assertEqual(curve_duty(z, 39.9, None, 3), 35)
        self.assertEqual(curve_duty(z, 40, None, 3), 45)
        self.assertEqual(curve_duty(z, 39, 45, 3), 45)
        self.assertEqual(curve_duty(z, 36.9, 45, 3), 35)
        self.assertEqual(curve_duty(z, 30, 100, 3), 35)
        self.assertEqual(curve_duty(z, 85, 35, 3), 100)

    def test_hottest_selected_sensor(self):
        self.enable()
        self.controller.config['zones'][0]['sensors'] = ['System Temp', 'CPU Temp']
        self.controller.tick()
        rows = self.controller.state['sensors']
        expected = max(s['value'] for s in rows if s['name'] in ['CPU Temp','System Temp'])
        self.assertEqual(self.controller.state['zones'][0]['temperature'], expected)

    def test_takeover_seeds_both_zones_and_no_redundant_writes(self):
        self.enable()
        self.controller.tick()
        self.assertEqual(self.ipmi.writes[:2], [(0,100),(1,100)])
        count = len(self.ipmi.writes)
        self.controller.tick()
        self.assertEqual(len(self.ipmi.writes), count)
        self.assertEqual(self.controller.state['phase'], 'controlling')

    def test_off_does_not_write_or_restore(self):
        self.enable()
        self.ipmi.power_state = 'off'
        self.controller.tick()
        self.assertEqual(self.controller.state['phase'], 'powered off')
        self.assertTrue(self.controller.state['connected'])
        self.assertFalse(self.ipmi.writes)
        self.assertEqual(self.ipmi.mode(), 0)

    def test_startup_and_power_cycle(self):
        self.enable()
        self.controller.config['startup_seconds'] = 20
        self.controller.tick()
        self.assertEqual(self.controller.state['phase'], 'starting')
        self.assertFalse(self.ipmi.writes)
        self.now += 21
        self.controller.tick()
        self.assertEqual(self.controller.state['phase'], 'controlling')
        self.ipmi.power_state = 'off'
        self.controller.tick()
        self.assertEqual(self.ipmi.mode(), 0)
        self.ipmi.power_state = 'on'
        self.controller.tick()
        self.assertEqual(self.controller.state['phase'], 'starting')

    def test_missing_sensor_never_lowers_fans_and_releases_ownership(self):
        self.enable()
        self.controller.tick()
        self.ipmi.writes.clear()
        self.ipmi.missing = True
        self.controller.tick()
        self.assertEqual(self.ipmi.writes, [(0,100),(1,100)])
        self.assertEqual(self.ipmi.mode(), 0)
        self.assertEqual(self.controller.state['phase'], 'fault')
        self.assertTrue(self.controller.state['connected'])

    def test_unreachable_is_not_reported_as_powered_off(self):
        self.controller.tick()
        self.ipmi.online = False
        self.controller.tick()
        self.assertFalse(self.controller.state['connected'])
        self.assertEqual(self.controller.state['power'], 'unknown')

    def test_restore_only_recognised_defaults(self):
        f = self.controller.config['fans']['FAN1']
        f.update(factory=[700,1000,1200], restore=True)
        self.controller.tick()
        self.assertEqual(self.ipmi.lowers['FAN1'], [400,800,1000])
        self.ipmi.lowers['FAN1'] = [300,600,900]
        self.now += 61
        self.controller.tick()
        self.assertEqual(self.ipmi.lowers['FAN1'], [300,600,900])
        self.assertEqual(self.controller.state['thresholds']['FAN1']['status'], 'different / unrecognised')

    def test_threshold_retry_after_failure(self):
        self.controller.config['fans']['FAN1'].update(factory=[700,1000,1200],restore=True)
        self.ipmi.reject=True
        self.controller.tick()
        self.assertEqual(self.controller.state['thresholds']['FAN1']['status'], 'restore failed')
        self.ipmi.reject=False
        self.now+=61
        self.controller.tick()
        self.assertEqual(self.ipmi.lowers['FAN1'],[400,800,1000])

    def test_disabled_control_can_restore_thresholds_independently(self):
        self.controller.config['fans']['FAN1'].update(factory=[700,1000,1200],restore=True)
        self.controller.tick()
        self.assertEqual(self.ipmi.lowers['FAN1'],[400,800,1000])
        self.assertFalse(self.ipmi.writes)

    def test_firmware_reset_detected_even_when_requested_duty_unchanged(self):
        self.enable()
        self.controller.tick()
        self.ipmi.fan_mode = 0
        self.ipmi.writes.clear()
        self.controller.tick()
        self.assertEqual(self.ipmi.writes[:2],[(0,100),(1,100)])

    def test_disable_and_shutdown_restore_firmware(self):
        self.enable()
        self.controller.tick()
        config=copy.deepcopy(self.controller.config)
        config['enabled']=False
        self.controller.update(config)
        self.assertEqual(self.ipmi.mode(),0)
        config['enabled']=True
        self.controller.update(config)
        self.controller.tick()
        self.controller.shutdown()
        self.assertEqual(self.ipmi.mode(),0)

    def test_persistence_and_capture_then_apply(self):
        self.controller.tick()
        self.controller.apply_threshold('FAN1',capture=True)
        self.assertEqual(Store(self.tmp.name).load()['fans']['FAN1']['factory'],[700,1000,1200])
        self.controller.apply_threshold('FAN1')
        self.assertEqual(self.ipmi.lowers['FAN1'],[400,800,1000])

    def test_empty_header_does_not_block_zone_or_restore_thresholds(self):
        self.enable()
        self.controller.config['fans']['FAN5'].update(factory=[700,1000,1200],restore=True)
        rows = self.ipmi.sensors()
        row = next(s for s in rows if s['name']=='FAN5')
        row.update(value=None,status='ns')
        with patch.object(self.ipmi,'sensors',return_value=rows), patch.object(self.ipmi,'thresholds') as write:
            self.controller.tick()
        self.assertEqual(self.controller.state['phase'],'controlling')
        self.assertEqual(self.controller.state['thresholds']['FAN5']['status'],'unavailable')
        write.assert_not_called()

    def test_legacy_acknowledgement_flag_is_ignored_on_load(self):
        import json
        for acknowledged in (False, True):
            legacy = defaults()
            legacy.update(enabled=True, hardware_confirmed=acknowledged)
            self.controller.store.path.write_text(json.dumps(legacy))
            migrated = self.controller.store.load()
            self.assertTrue(migrated['enabled'])
            self.assertNotIn('hardware_confirmed', migrated)
            self.assertIn('hardware_confirmed', legacy)
            self.controller.store.save(migrated)
            self.assertNotIn('hardware_confirmed', json.loads(self.controller.store.path.read_text()))

    def test_unavailable_fan_reasons_distinguish_missing_sensor_reading_and_thresholds(self):
        self.assertEqual(fan_unavailable_reason(None), 'No matching RPM sensor found')
        row = {'value': None, 'unit': 'RPM', 'lower': [400, 800, 1000]}
        self.assertEqual(fan_unavailable_reason(row), 'Fan sensor has no RPM reading')
        row.update(value=1200, lower=[None, None, None])
        self.assertEqual(fan_unavailable_reason(row), 'BMC did not report any lower thresholds')
        row['lower'] = [420, 840, None]
        self.assertIsNone(fan_unavailable_reason(row))
        row.update(value=0, lower=[400, 800, 1000])
        self.assertIsNone(fan_unavailable_reason(row))

    def test_real_h12_rows_partial_thresholds_and_inactive_headers(self):
        rows=parse_sensors((Path(__file__).parent/'fixtures/h12-fans.txt').read_text())
        with patch.object(self.ipmi,'expected_lower',side_effect=lambda n,v,a: masked_lower(v,a,FanScale(140,0,0,0))):
            self.controller.check_thresholds(rows)
        statuses=self.controller.state['thresholds']
        for name in ['FAN1','FAN2','FANA','FANB']:
            self.assertEqual(statuses[name]['status'],'matched (BMC rounded)')
            self.assertEqual(statuses[name]['expected'],[420,840,None])
        for name in ['FAN3','FAN4','FAN5']:
            self.assertEqual(statuses[name]['status'],'unavailable')
            self.assertEqual(statuses[name]['reason'],'Fan sensor has no RPM reading')

    def test_partial_factory_capture_persists_and_restores_once(self):
        self.ipmi.lowers['FAN1']=[840,1120,None]
        with patch.object(self.ipmi,'expected_lower',side_effect=lambda n,v,a: masked_lower(v,a,FanScale(140,0,0,0))):
            self.controller.tick()
            self.controller.apply_threshold('FAN1',capture=True)
            config=self.controller.snapshot()['config']
            self.assertEqual(config['fans']['FAN1']['factory'],[840,1120,None])
            config['fans']['FAN1']['restore']=True
            self.controller.update(config)
            with patch.object(self.ipmi,'thresholds',wraps=self.ipmi.thresholds) as write:
                self.controller.tick()
                self.assertEqual(self.ipmi.lowers['FAN1'],[420,840,None])
                self.now+=61
                self.controller.tick()
                write.assert_called_once_with('FAN1',[400,800,1000])
            self.assertEqual(Store(self.tmp.name).load()['fans']['FAN1']['factory'],[840,1120,None])

    def test_obsolete_headers_migrated_without_losing_other_settings(self):
        import json
        config=defaults()
        config['fans']['FAN6']={'desired':[400,800,1000],'factory':None,'restore':False}
        config['fans']['FAN7']=copy.deepcopy(config['fans']['FAN6'])
        config['zones'][0]['name']='Zone 1 · FAN1–FAN7'
        config['zones'][0]['points'][0]['duty']=40
        config['fans']['FAN1']['desired']=[300,600,900]
        self.controller.store.path.write_text(json.dumps(config))
        migrated=self.controller.store.load()
        self.assertEqual(set(migrated['fans']), {'FAN1','FAN2','FAN3','FAN4','FAN5','FANA','FANB'})
        self.assertEqual(migrated['zones'][0]['name'],'Zone 1 · FAN1–FAN5')
        self.assertEqual(migrated['zones'][0]['points'][0]['duty'],40)
        self.assertEqual(migrated['fans']['FAN1']['desired'],[300,600,900])
        config['zones'][0]['name']='My CPU fans'
        self.assertEqual(validate(config)['zones'][0]['name'],'My CPU fans')

    def test_monitoring_reads_bmc_duties_without_writing(self):
        self.ipmi.duties={0:57,1:38}
        self.controller.tick()
        self.assertEqual(self.controller.state['phase'],'monitoring')
        self.assertEqual([r['duty'] for r in self.controller.state['bmc_duties']],[57,38])
        self.assertEqual(self.controller.state['zones'],[])
        self.assertEqual(self.ipmi.writes,[])

    def test_starting_reads_bmc_duties_without_enabling_control(self):
        self.enable()
        self.controller.config['startup_seconds']=20
        self.ipmi.duties={0:65,1:45}
        self.controller.tick()
        self.assertEqual(self.controller.state['phase'],'starting')
        self.assertEqual([r['duty'] for r in self.controller.state['bmc_duties']],[65,45])
        self.assertEqual(self.ipmi.writes,[])

    def test_off_and_unreachable_clear_previous_duty_readings(self):
        self.controller.tick()
        self.assertEqual(len(self.controller.state['bmc_duties']),2)
        self.ipmi.power_state='off'
        with patch.object(self.ipmi,'duty') as read:
            self.controller.tick()
            read.assert_not_called()
        self.assertEqual(self.controller.state['bmc_duties'],[])
        self.ipmi.power_state='on'
        self.controller.tick()
        self.ipmi.online=False
        self.controller.tick()
        self.assertEqual(self.controller.state['bmc_duties'],[])

    def test_one_duty_read_failure_does_not_hide_sensors_or_other_zone(self):
        def duty(zone):
            if zone==0:
                raise IPMIError('Zone duty read failed')
            return 37
        with patch.object(self.ipmi,'duty',side_effect=duty):
            self.controller.tick()
        self.assertEqual(self.controller.state['phase'],'monitoring')
        self.assertTrue(self.controller.state['sensors'])
        first, second=self.controller.state['bmc_duties']
        self.assertIsNone(first['duty'])
        self.assertIn('read failed',first['error'])
        self.assertEqual(second['duty'],37)

    def test_control_telemetry_uses_new_readback_instead_of_assigned_target(self):
        self.enable()
        self.controller.tick()
        self.assertEqual([r['readback'] for r in self.controller.state['zones']],
                         [self.ipmi.duties[0],self.ipmi.duties[1]])
        # A later BMC override can differ from the requested duty.
        self.ipmi.duties={0:83,1:76}
        self.controller.read_bmc_duties()
        self.assertEqual([r['readback'] for r in self.controller.state['zones']],[83,76])
        self.assertNotEqual(self.controller.state['zones'][0]['requested'],83)

    def test_invalid_config(self):
        mutations = [lambda c:c.update(enabled='yes'),
                     lambda c:c.update(poll_seconds=float('nan')),
                     lambda c:c['zones'][0]['points'].append({'temp':40,'duty':20}),
                     lambda c:c['fans']['FAN1'].update(restore=True),
                     lambda c:c['fans']['FAN1'].update(desired=[1000,800,400]),
                     lambda c:c['zones'][0].update(sensors=[])]
        for mutation in mutations:
            config=defaults();mutation(config)
            with self.assertRaises(ValueError): validate(config)


if __name__ == '__main__':
    unittest.main()

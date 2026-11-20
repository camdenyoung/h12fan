import copy
import tempfile
import unittest
from unittest.mock import patch
from h12fan.config import Store, defaults, validate
from h12fan.controller import Controller, curve_duty
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
        super().thresholds(name, values)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ipmi = RecordingIPMI()
        self.now = 100
        self.controller = Controller(self.ipmi, Store(self.tmp.name), lambda: self.now)
        self.controller.config['startup_seconds'] = 0

    def enable(self):
        self.controller.config.update(enabled=True, hardware_confirmed=True)

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
        self.controller.config['fans']['FAN7'].update(factory=[700,1000,1200],restore=True)
        rows = self.ipmi.sensors()
        row = next(s for s in rows if s['name']=='FAN7')
        row.update(value=None,status='ns')
        with patch.object(self.ipmi,'sensors',return_value=rows), patch.object(self.ipmi,'thresholds') as write:
            self.controller.tick()
        self.assertEqual(self.controller.state['phase'],'controlling')
        self.assertEqual(self.controller.state['thresholds']['FAN7']['status'],'unavailable')
        write.assert_not_called()

    def test_invalid_config(self):
        mutations = [lambda c:c.update(enabled=True),
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

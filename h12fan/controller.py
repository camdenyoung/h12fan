import copy
import threading
import time
from collections import deque
from .config import validate
from .ipmi import IPMIError


def fan_unavailable_reason(row):
    if row is None:
        return 'No matching RPM sensor found'
    if row['value'] is None:
        return 'Fan sensor has no RPM reading'
    if row['unit'].lower() != 'rpm':
        return 'Sensor does not report RPM units'
    if all(v is None for v in row['lower']):
        return 'BMC did not report any lower thresholds'
    return None


def curve_duty(zone, temperature, previous, hysteresis):
    if temperature >= zone['critical']:
        return 100
    points = zone['points']
    duty = points[0]['duty']
    for p in points:
        if temperature >= p['temp']:
            duty = p['duty']
    if previous is not None and duty < previous:
        # Each crossed step must clear its own downward hysteresis boundary.
        for p in points:
            if duty < p['duty'] <= previous and temperature >= p['temp'] - hysteresis:
                duty = max(duty, p['duty'])
    return duty


class Controller:
    def __init__(self, ipmi, store, clock=time.monotonic):
        self.ipmi, self.store, self.clock = ipmi, store, clock
        self.config = store.load()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        try:
            history = store.load_events()
        except (OSError, ValueError):
            history = [{'time': time.time(), 'message': 'Saved activity history could not be read; continuing with a new history', 'level': 'error'}]
        self.events = deque(history, maxlen=100)
        self._history_write_failed = False
        self.owned = False
        self.on_since = None
        self.last_threshold = None
        self.last_power = None
        self.previous = {}
        self.state = {'connected': False, 'power': 'unknown', 'phase': 'connecting',
                      'sensors': [], 'zones': [], 'last_success': None, 'error': None,
                      'thresholds': {}, 'bmc_duties': [], 'demo': ipmi.demo, 'host': ipmi.host}

    def event(self, message, level='info', *, deduplicate=True):
        if not deduplicate or not self.events or self.events[0]['message'] != message:
            self.events.appendleft({'time': time.time(), 'message': message, 'level': level})
            try:
                self.store.save_events(self.events)
                self._history_write_failed = False
            except (OSError, ValueError):
                # A full/unwritable volume must not stop cooling or misreport a duty write.
                if not self._history_write_failed:
                    self.events.appendleft({'time': time.time(), 'message': 'Activity history could not be saved; keeping events in memory until storage recovers', 'level': 'error'})
                self._history_write_failed = True

    def change_duty(self, zone, duty, reason, *, actual=None, force=False):
        if actual is None:
            try:
                actual = self.ipmi.duty(zone)
            except Exception:
                if not force:
                    raise
                # Safety writes must still be attempted when the old duty cannot be read.
        if actual == duty and not force:
            return
        self.ipmi.set_duty(zone, duty)  # The adapter verifies the BMC read-back.
        if actual != duty:
            old = 'unknown' if actual is None else f'{actual}%'
            self.event(f'Zone {zone} duty updated: {old} → {duty}% — {reason}', deduplicate=False)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({**self.state, 'config': self.config, 'events': list(self.events),
                                  'server_time': time.time()})

    def release(self):
        if self.owned:
            self.ipmi.set_mode(0)
            self.owned = False
            self.previous.clear()
            self.event('Released fan control to firmware Standard mode')

    def update(self, config):
        config = validate(config)
        with self.lock:
            # A failed release must not silently report control as disabled.
            if not config['enabled']:
                self.release()
            self.store.save(config)
            self.config = config
            self.previous.clear()
            self.last_threshold = None
            self.event('Configuration saved')

    def available(self):
        return self.state['connected'] and self.state['power'] == 'on' and self.state['phase'] != 'starting'

    def apply_threshold(self, name, capture=False):
        with self.lock:
            if not self.available():
                raise ValueError('Wait for a running server and fresh sensor readings')
            if name not in self.config['fans']:
                raise ValueError('Unknown fan')
            # Fetch fresh records before any explicit action; never act on stale cached data.
            if self.ipmi.power() != 'on':
                raise ValueError('Server is powered off')
            rows = self.ipmi.sensors()
            row = next((s for s in rows if s['name'] == name), None)
            reason = fan_unavailable_reason(row)
            if reason:
                raise ValueError(f'{name}: {reason}')
            fan = self.config['fans'][name]
            if capture:
                updated = copy.deepcopy(self.config)
                updated['fans'][name]['factory'] = row['lower']
                self.store.save(updated)
                self.config = updated
                self.event(f'{name}: recorded current readings as known factory thresholds')
            else:
                if self.owned:
                    for zone in (0, 1):
                        self.change_duty(zone, 100, 'preparing fan threshold update', force=True)
                    self.previous.clear()
                actual = self.ipmi.thresholds(name, fan['desired'])
                self.event(f'{name}: lower thresholds applied and verified: {actual} (unreported slots skipped)')
            self.last_threshold = None

    def check_thresholds(self, rows):
        status = {}
        protected = False
        by_name = {r['name']: r for r in rows}
        for name, fan in self.config['fans'].items():
            row = by_name.get(name)
            actual = row['lower'] if row else None
            reason = fan_unavailable_reason(row)
            if reason:
                status[name] = {'status': 'unavailable', 'actual': actual, 'reason': reason}
                continue
            try:
                expected = self.ipmi.expected_lower(name, fan['desired'], actual)
            except ValueError as exc:
                status[name] = {'status': 'invalid target', 'actual': actual, 'reason': str(exc)}
                continue
            if actual == expected:
                label = 'matched (BMC rounded)' if any(v is not None and v != fan['desired'][i] for i, v in enumerate(expected)) else 'matched'
            elif fan['factory'] is not None and actual == fan['factory']:
                label = 'factory defaults'
                if fan['restore']:
                    try:
                        if self.owned and not protected:
                            # Threshold read-back can take time: keep cooling conservative.
                            for zone in (0, 1):
                                self.change_duty(zone, 100, 'preparing automatic threshold restoration', force=True)
                            self.previous.clear()
                            protected = True
                        actual = self.ipmi.thresholds(name, fan['desired'])
                        label = 'restored'
                        self.event(f'{name}: factory defaults detected; saved thresholds restored and verified')
                    except IPMIError as exc:
                        label = 'restore failed'
                        self.event(str(exc), 'error')
            else:
                label = 'different / unrecognised'
            status[name] = {'status': label, 'actual': actual, 'expected': expected}
        self.state['thresholds'] = status

    def fail_safe(self):
        if self.owned:
            # Attempt both zones even if one command fails, then return to firmware.
            for zone in (0, 1):
                try:
                    self.change_duty(zone, 100, 'fault recovery', force=True)
                except Exception:
                    pass
            try:
                self.release()
            except Exception:
                self.event('Unable to restore firmware control; last duty may remain active', 'error')

    def read_bmc_duties(self):
        readings = []
        for zone in (0, 1):
            try:
                duty = self.ipmi.duty(zone)
                if not isinstance(duty, int) or isinstance(duty, bool) or not 0 <= duty <= 100:
                    raise IPMIError(f'Zone {zone}: BMC reported an invalid duty value')
                readings.append({'id': zone, 'duty': duty, 'updated_at': time.time(), 'error': None})
            except Exception as exc:
                # A duty telemetry failure must not hide otherwise usable sensor data.
                readings.append({'id': zone, 'duty': None, 'updated_at': None, 'error': str(exc)})
        self.state['bmc_duties'] = readings
        for target in self.state['zones']:
            target['readback'] = readings[target['id']]['duty']

    def tick(self):
        with self.lock:
            power_read = False
            try:
                was_connected = self.state['connected']
                power = self.ipmi.power()
                power_read = True
                self.state.update(connected=True, power=power, error=None)
                if power != self.last_power or not was_connected:
                    self.event(f'BMC connected; server power {power}')
                    self.on_since = self.clock() if power == 'on' else None
                    self.previous.clear()
                    self.last_threshold = None
                self.last_power = power
                if power == 'off':
                    self.release()
                    self.state.update(phase='powered off', sensors=[], zones=[], thresholds={}, last_success=time.time())
                    return
                rows = self.ipmi.sensors()
                self.state.update(sensors=rows, last_success=time.time())
                if self.clock() - self.on_since < self.config['startup_seconds']:
                    self.state.update(phase='starting', zones=[])
                    return
                self.state['phase'] = 'monitoring'
                if self.last_threshold is None or self.clock() - self.last_threshold >= self.config['threshold_check_seconds']:
                    self.check_thresholds(rows)
                    self.last_threshold = self.clock()
                    # Use fresh temperatures after potentially slow threshold writes.
                    rows = self.ipmi.sensors()
                    self.state.update(sensors=rows, last_success=time.time())
                if not self.config['enabled']:
                    self.release()
                    self.state['zones'] = []
                    return
                temperatures = {r['name']: r['value'] for r in rows
                                if r['unit'].lower() == 'degrees c'}
                targets = []
                for z in self.config['zones']:
                    values = [temperatures.get(n) for n in z['sensors']]
                    if any(v is None for v in values):
                        raise IPMIError(f"Zone {z['id']}: required temperature sensor unavailable")
                    temp = max(values)
                    sensor = max(z['sensors'], key=lambda name: temperatures[name])
                    targets.append({'id': z['id'], 'temperature': temp,
                                    'sensor': sensor,
                                    'requested': curve_duty(z, temp, self.previous.get(z['id']), self.config['hysteresis']),
                                    'critical': temp >= z['critical']})
                # Read mode each poll to detect BMC resets. Seed both zones at 100% on takeover.
                if self.ipmi.mode() != 1 or not self.owned:
                    self.owned = True  # Also recover if mode setting succeeds but verification fails.
                    self.ipmi.set_mode(1)
                    for zone in (0, 1):
                        self.change_duty(zone, 100, 'taking over manual fan control', force=True)
                for target in targets:
                    zone, duty = target['id'], target['requested']
                    actual = self.ipmi.duty(zone)
                    if actual != duty:
                        reason = 'critical temperature override' if target['critical'] else 'temperature curve'
                        self.change_duty(zone, duty,
                                         f"{target['sensor']} {target['temperature']:g}°C; {reason}", actual=actual)
                    self.previous[zone] = duty
                self.state.update(phase='controlling', zones=targets)
            except Exception as exc:
                self.state.update(phase='fault', error=str(exc), zones=[])
                # A failed sensor/control operation is not evidence that the chassis is off.
                self.state['connected'] = power_read
                if not power_read:
                    self.state['power'] = 'unknown'
                self.last_threshold = None
                self.event(str(exc), 'error')
                self.fail_safe()
            finally:
                if power_read and self.state['power'] == 'on':
                    self.read_bmc_duties()
                else:
                    self.state['bmc_duties'] = []

    def run(self):
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(self.config['poll_seconds'])

    def shutdown(self):
        self.stop.set()
        with self.lock:
            try:
                self.release()
            except Exception:
                self.fail_safe()

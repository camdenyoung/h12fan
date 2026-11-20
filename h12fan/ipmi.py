import math
import os
import subprocess
import time
import tempfile
from pathlib import Path
from .sdr import fan_scales, masked_lower


class IPMIError(RuntimeError):
    pass


def secret(name, default=''):
    path = os.getenv(name + '_FILE')
    return Path(path).read_text().rstrip('\r\n') if path else os.getenv(name, default)


def numeric(text):
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def parse_sensors(output):
    sensors = []
    for line in output.splitlines():
        cols = [v.strip() for v in line.split('|')]
        if len(cols) < 10:
            continue
        # ipmitool sensor list: name, reading, units, status, LNR, LCR, LNC, UNC, UCR, UNR
        sensors.append({'name': cols[0], 'value': numeric(cols[1]), 'unit': cols[2],
                        'status': cols[3], 'lower': [numeric(v) for v in cols[4:7]]})
    return sensors


class IPMI:
    demo = False

    def __init__(self):
        self.host = secret('IPMI_HOST')
        self.user = secret('IPMI_USER')
        self.password = secret('IPMI_PASSWORD')
        if not self.host or not self.user or not self.password:
            raise ValueError('Set IPMI_HOST, IPMI_USER and IPMI_PASSWORD (or their _FILE variants)')
        self.port = int(os.getenv('IPMI_PORT', '623'))
        self.interface = os.getenv('IPMI_INTERFACE', 'lanplus').strip().lower()
        if self.interface not in ('lan', 'lanplus'):
            raise ValueError('IPMI_INTERFACE must be lan or lanplus')
        self.cipher = int(os.getenv('IPMI_CIPHER', '3'))
        self._fan_scales = {}
        self._scale_checked = None

    def run(self, *args):
        env = os.environ.copy()
        env['IPMI_PASSWORD'] = self.password
        command = ['ipmitool', '-I', self.interface, '-H', self.host, '-p', str(self.port),
                   '-U', self.user, '-E']
        if self.interface == 'lanplus':
            command.extend(['-C', str(self.cipher)])
        command.extend(['-N', '2', '-R', '1', *map(str, args)])
        operation = ' '.join(map(str, args))
        connection = f'lanplus, cipher {self.cipher}' if self.interface == 'lanplus' else 'lan'
        context = f'IPMI {operation} ({connection})'
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=12)
        except subprocess.TimeoutExpired as exc:
            raise IPMIError(f'{context} timed out after 12 seconds') from exc
        except OSError as exc:
            raise IPMIError(f'{context}: ipmitool is unavailable or could not be executed') from exc
        if result.returncode:
            # Keep the failing operation and utility's reason, without echoing credentials.
            detail = result.stderr or result.stdout or 'ipmitool returned no error details'
            message = f'{context} failed (exit {result.returncode}): {detail}'
            for value in sorted({self.password, self.user}, key=len, reverse=True):
                if value:
                    message = message.replace(value, '[redacted]')
            # Redact before normalising/truncating, including credentials containing newlines.
            message = ' '.join(''.join(c if c.isprintable() else ' ' for c in message).split())
            raise IPMIError(message[:1000])
        return result.stdout.strip()

    def power(self):
        value = self.run('chassis', 'power', 'status').lower()
        if value.endswith(' is on'):
            return 'on'
        if value.endswith(' is off'):
            return 'off'
        raise IPMIError('Unrecognised chassis power response')

    def sensors(self):
        rows = parse_sensors(self.run('sensor', 'list'))
        if not rows:
            raise IPMIError('No readable IPMI sensor records')
        return rows

    def mode(self):
        return int(self.run('raw', '0x30', '0x45', '0x00').strip(), 16)

    def set_mode(self, mode):
        self.run('raw', '0x30', '0x45', '0x01', hex(mode))
        if self.mode() != mode:
            raise IPMIError('Fan mode read-back did not match')

    def duty(self, zone):
        return int(self.run('raw', '0x30', '0x70', '0x66', '0x00', hex(zone)).strip(), 16)

    def set_duty(self, zone, duty):
        self.run('raw', '0x30', '0x70', '0x66', '0x01', hex(zone), hex(duty))
        if self.duty(zone) != duty:
            raise IPMIError(f'Zone {zone}: duty read-back did not match')

    def expected_lower(self, name, values, actual):
        expected = masked_lower(values, actual)
        if expected == actual:
            return expected
        if self._scale_checked is None or time.monotonic() - self._scale_checked > 300:
            try:
                with tempfile.TemporaryDirectory(prefix='h12fan-sdr-') as directory:
                    path = Path(directory) / 'sensors.sdr'
                    self.run('sdr', 'dump', str(path))
                    self._fan_scales = fan_scales(path.read_bytes())
            except (IPMIError, OSError, ValueError):
                # Monitoring still works; uncertain rounding must not pass verification.
                self._fan_scales = {}
            self._scale_checked = time.monotonic()
        return masked_lower(values, actual, self._fan_scales.get(name))

    def thresholds(self, name, values):
        row = next((s for s in self.sensors() if s['name'] == name), None)
        if row is None or row['value'] is None or all(v is None for v in row['lower']):
            raise IPMIError(f'{name}: no active fan or readable lower thresholds')
        expected = self.expected_lower(name, values, row['lower'])
        # Never write the unsupported/unreported slots. Order writes to avoid crossing
        # existing thresholds while changing an increasing lower-threshold sequence.
        slots = ['lnr', 'lcr', 'lnc']
        indices = [i for i in range(3) if row['lower'][i] is not None]
        decreasing = [i for i in indices if expected[i] < row['lower'][i]]
        increasing = [i for i in reversed(indices) if expected[i] > row['lower'][i]]
        for i in decreasing + increasing:
            self.run('sensor', 'thresh', name, slots[i], values[i])
        after = next((s for s in self.sensors() if s['name'] == name), None)
        if after is None or after['lower'] != expected:
            actual = after['lower'] if after else None
            raise IPMIError(f'{name}: threshold read-back mismatch; expected {expected}, received {actual}')
        return after['lower']


class DemoIPMI:
    demo = True
    host = 'Simulated BMC'

    def __init__(self):
        self.power_state = 'on'
        self.fan_mode = 0
        self.duties = {0: 100, 1: 100}
        self.lowers = {n: [700, 1000, 1200] for n in ['FAN1', 'FAN2', 'FAN3', 'FAN4', 'FAN5', 'FANA', 'FANB']}

    def power(self):
        return self.power_state

    def sensors(self):
        wave = math.sin(time.monotonic() / 25)
        rows = [{'name': n, 'value': round(t + wave * 4, 1) if self.power_state == 'on' else None,
                 'unit': 'degrees C', 'status': 'ok', 'lower': [None] * 3}
                for n, t in [('CPU Temp', 49), ('System Temp', 35), ('Peripheral Temp', 39)]]
        for name, lower in self.lowers.items():
            zone = 1 if name in ['FANA', 'FANB'] else 0
            rows.append({'name': name, 'value': round(3200 * self.duties[zone] / 100) if self.power_state == 'on' else None,
                         'unit': 'RPM', 'status': 'ok', 'lower': list(lower)})
        return rows

    def mode(self):
        return self.fan_mode

    def set_mode(self, mode):
        self.fan_mode = mode

    def duty(self, zone):
        return self.duties[zone]

    def set_duty(self, zone, duty):
        self.duties[zone] = duty

    def expected_lower(self, name, values, actual):
        return masked_lower(values, actual)

    def thresholds(self, name, values):
        if name not in self.lowers:
            raise IPMIError('Fan not found')
        self.lowers[name] = self.expected_lower(name, values, self.lowers[name])
        return list(self.lowers[name])

import math
import os
import subprocess
import time
from pathlib import Path


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
        self.cipher = int(os.getenv('IPMI_CIPHER', '17'))

    def run(self, *args):
        env = os.environ.copy()
        env['IPMI_PASSWORD'] = self.password
        command = ['ipmitool', '-I', 'lanplus', '-H', self.host, '-p', str(self.port),
                   '-U', self.user, '-E', '-C', str(self.cipher), '-N', '2', '-R', '1', *map(str, args)]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IPMIError('IPMI command unavailable or timed out') from exc
        if result.returncode:
            # Do not relay subprocess output: firmware and utilities may echo credentials.
            raise IPMIError('IPMI command failed; check connectivity, credentials and BMC support')
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

    def thresholds(self, name, values):
        self.run('sensor', 'thresh', name, 'lower', *values)
        row = next((s for s in self.sensors() if s['name'] == name), None)
        if row is None or row['lower'] != values:
            raise IPMIError(f'{name}: threshold read-back did not match (possible sensor quantisation)')


class DemoIPMI:
    demo = True
    host = 'Simulated BMC'

    def __init__(self):
        self.power_state = 'on'
        self.fan_mode = 0
        self.duties = {0: 100, 1: 100}
        self.lowers = {n: [700, 1000, 1200] for n in ['FAN1', 'FAN2', 'FAN3', 'FAN4', 'FAN5', 'FAN6', 'FAN7', 'FANA', 'FANB']}

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

    def thresholds(self, name, values):
        if name not in self.lowers:
            raise IPMIError('Fan not found')
        self.lowers[name] = list(values)

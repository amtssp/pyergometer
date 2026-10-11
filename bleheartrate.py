import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time


# On Windows a file cannot be opened while another process swaps it with os.replace,
# so readers and the writer retry briefly instead of failing a whole control step.
FILE_RETRY_ATTEMPTS = 50
FILE_RETRY_DELAY = 0.02


def read_shared_json(path):
    """Read a JSON file that another process replaces atomically."""
    for attempt in range(FILE_RETRY_ATTEMPTS):
        try:
            with open(path, 'rb') as shared_file:
                return json.loads(shared_file.read().decode('utf-8'))
        except PermissionError:
            if attempt == FILE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(FILE_RETRY_DELAY)


def replace_shared_file(source, destination):
    for attempt in range(FILE_RETRY_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == FILE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(FILE_RETRY_DELAY)


HEART_RATE_SERVICE_UUID = '0000180d-0000-1000-8000-00805f9b34fb'
HEART_RATE_MEASUREMENT_UUID = '00002a37-0000-1000-8000-00805f9b34fb'


def parse_heart_rate_measurement(data):
    if not data:
        raise ValueError('Bluetooth heart-rate measurement is empty')
    flags = data[0]
    size = 2 if flags & 0x01 else 1
    if len(data) < size + 1:
        raise ValueError('Bluetooth heart-rate measurement is incomplete')
    pulse = int.from_bytes(data[1:size + 1], byteorder='little')
    if pulse < 20 or pulse > 250:
        raise ValueError('Bluetooth heart rate is outside the supported range')
    return pulse


def _bleak_modules():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise RuntimeError('Install the project requirements to use Bluetooth pulse') from error
    return BleakClient, BleakScanner


def scan_heart_rate_devices(timeout=8.0):
    command = [
        sys.executable, os.path.abspath(__file__),
        '--scan', '--timeout', str(timeout)
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout + 15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Bluetooth scan could not start: %s' % error) from error
    if result.returncode != 0:
        message = result.stderr.decode('utf-8', 'replace').strip()
        raise RuntimeError(
            message or 'Bluetooth scan worker exited with code %d' % result.returncode)
    try:
        return json.loads(result.stdout.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError('Bluetooth scan returned invalid data: %s' % error) from error


def _scan_heart_rate_devices(timeout):
    unused_client, scanner_type = _bleak_modules()

    async def scan():
        devices = await scanner_type.discover(timeout=timeout, return_adv=True)
        found = []
        for device, advertisement in devices.values():
            if not device.address:
                continue
            service_uuids = [
                value.lower() for value in (advertisement.service_uuids or [])
            ]
            found.append({
                'name': advertisement.local_name or device.name or 'Ukendt Bluetooth-enhed',
                'address': device.address,
                'heart_rate_service': HEART_RATE_SERVICE_UUID in service_uuids,
                'rssi': advertisement.rssi
            })
        return found

    return sorted(
        asyncio.run(scan()),
        key=lambda item: (
            not item['heart_rate_service'],
            -(item['rssi'] if item['rssi'] is not None else -127),
            item['name'].lower()
        ))


class HeartRateMonitor:
    def __init__(self, address, sample_path):
        self.address = address
        self.sample_path = sample_path
        self.status_path = sample_path + '.status'
        self.process = None

    def start(self):
        self.process = subprocess.Popen([
            sys.executable, os.path.abspath(__file__),
            '--monitor', self.address, self.sample_path, self.status_path
        ])

    def wait_for_sample(self, timeout):
        deadline = time.monotonic() + timeout
        while self.process and self.process.poll() is None:
            if self.status()['fresh']:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return self.status()['fresh']

    def status(self):
        try:
            data = read_shared_json(self.status_path)
        except IOError:
            data = {}
        except (ValueError, TypeError) as error:
            return {
                'connected': False,
                'fresh': False,
                'pulse': None,
                'age': None,
                'error': 'Urets Bluetooth-status kunne ikke læses: %s' % error
            }

        sample_time = data.get('timestamp')
        age = None if not isinstance(sample_time, (int, float)) else max(
            0, time.time() - sample_time)
        connected = data.get('connected') is True
        fresh = connected and age is not None and age <= 3
        error = data.get('error')
        if self.process and self.process.poll() is not None:
            connected = False
            fresh = False
            error = error or 'Bluetooth-forbindelsen stoppede (kode %d)' % self.process.returncode
        return {
            'connected': connected,
            'fresh': fresh,
            'pulse': data.get('pulse'),
            'age': age,
            'error': error if error else (
                None if fresh else 'Ingen frisk pulsmåling fra uret')
        }

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class _HeartRateWorker:
    def __init__(self, address, sample_path, status_path):
        self.address = address
        self.sample_path = sample_path
        self.status_path = status_path
        self.connected = False
        self.error = None
        self.last_pulse = None
        self.last_sample_at = None

    def run(self):
        try:
            asyncio.run(self._watch())
        except Exception as error:
            self.connected = False
            self.error = 'Bluetooth-pulsmåleren stoppede: %s' % error
            self._write_connection_state()

    def _write_payload(self, path, payload):
        content = json.dumps(payload).encode('utf-8')
        directory = os.path.dirname(os.path.abspath(path))
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='pyergometer-hr-', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(descriptor, 'wb') as sample_file:
                sample_file.write(content)
                sample_file.flush()
            replace_shared_file(temporary_path, path)
        except Exception:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise

    def _write_connection_state(self):
        payload = {
            'pulse': self.last_pulse,
            'timestamp': self.last_sample_at,
            'connected': self.connected,
            'error': self.error
        }
        self._write_payload(self.sample_path, payload)
        self._write_payload(self.status_path, payload)

    def _write_sample(self, pulse):
        sample_time = time.time()
        self.error = None
        payload = {
            'pulse': pulse,
            'timestamp': sample_time,
            'connected': self.connected,
            'error': self.error
        }
        self._write_payload(self.sample_path, payload)
        self._write_payload(self.status_path, payload)
        self.last_pulse = pulse
        self.last_sample_at = sample_time

    async def _watch(self):
        client_type, unused_scanner = _bleak_modules()
        while True:
            try:
                async with client_type(self.address) as client:
                    characteristic = client.services.get_characteristic(
                        HEART_RATE_MEASUREMENT_UUID)
                    if characteristic is None:
                        raise RuntimeError('Uret tilbyder ikke standardtjenesten for Bluetooth-puls')

                    def on_measurement(unused_sender, data):
                        try:
                            self._write_sample(parse_heart_rate_measurement(data))
                        except (IOError, OSError, ValueError) as error:
                            self.error = 'Kunne ikke læse pulsmåling fra uret: %s' % error

                    self.connected = True
                    self.error = None
                    await client.start_notify(
                        HEART_RATE_MEASUREMENT_UUID, on_measurement)
                    while client.is_connected:
                        await asyncio.sleep(0.25)
                    self.connected = False
                    self.error = 'Forbindelsen til uret blev afbrudt'
                    self._write_connection_state()
            except Exception as error:
                self.connected = False
                self.error = 'Forbindelsen til uret fejlede: %s' % error
                self._write_connection_state()
            await asyncio.sleep(2)


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--scan':
    try:
        timeout = float(sys.argv[sys.argv.index('--timeout') + 1])
        sys.stdout.write(json.dumps(_scan_heart_rate_devices(timeout)))
    except Exception as error:
        sys.stderr.write('Bluetooth scan failed: %s\n' % error)
        sys.exit(1)
elif __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--monitor':
    worker = _HeartRateWorker(sys.argv[2], sys.argv[3], sys.argv[4])
    worker.run()

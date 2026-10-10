import datetime
import json
import os
import sys
import tempfile
import time

from bleheartrate import read_shared_json


class Ergometer:
    def __init__(self, device=None, session=None, log=None, live_log=None,
                 live_control=None, heart_rate_file=None):
        self.device = device
        self.session = session
        self.pc = 0
        self.live_log_file = open(live_log, 'w') if live_log else None
        self.live_control = live_control
        self.live_control_position = 0
        self.heart_rate_file = heart_rate_file
        self.last_watch_pulse = None
        self.heart_rate_fresh = not heart_rate_file
        self.stop_requested = False

        if log:
            self.logtemp = tempfile.NamedTemporaryFile(mode='w', delete=False)
            self.log = log
        else:
            self.logtemp = None
            self.log = None

    def run(self):
        sys.stderr.write('Waiting for session to start')
        while not self.stop_requested:
            self.poll_live_updates()
            if self.stop_requested:
                break
            self.device.status()
            if self.heart_rate_file:
                self.refresh_heart_rate()
                if not self.heart_rate_fresh:
                    self.device.pulse = 0

            if self.device.rpm < 20 or self.device.pulse < 20:
                sys.stderr.write('.')
                time.sleep(1.0)
            else:
                sys.stderr.write('..ok\n')
                self.begin = datetime.datetime.utcnow()
                self.begin_monotonic = time.monotonic()
                break

        if self.stop_requested and not hasattr(self, 'begin'):
            self.begin = datetime.datetime.utcnow()
            self.begin_monotonic = time.monotonic()
        next_control = self.begin_monotonic
        while (not self.stop_requested and
               time.monotonic() - self.begin_monotonic < self.session.duration):
            self.poll_live_updates()
            if self.stop_requested:
                break
            now = time.monotonic()
            if now >= next_control:
                self.control_step(int(now - self.begin_monotonic))
                next_control = now + 1.0
            time.sleep(0.05)

        self.end = datetime.datetime.utcnow()
        self.finish()

    def refresh_heart_rate(self):
        if not self.heart_rate_file:
            self.heart_rate_fresh = True
            return True
        try:
            sample = read_shared_json(self.heart_rate_file)
            pulse = sample['pulse']
            timestamp = sample['timestamp']
            connected = sample['connected']
            if (isinstance(pulse, bool) or not isinstance(pulse, int) or
                    pulse < 20 or pulse > 250 or
                    isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or
                    not isinstance(connected, bool)):
                raise ValueError('Bluetooth pulse sample has invalid values')
        except (IOError, ValueError, KeyError, TypeError) as error:
            sys.stderr.write('\nCould not read Bluetooth pulse sample: %s\n' % error)
            self.heart_rate_fresh = False
            return False

        self.heart_rate_fresh = connected and time.time() - timestamp <= 3
        if self.heart_rate_fresh:
            self.last_watch_pulse = pulse
            self.device.pulse = pulse
        elif self.last_watch_pulse is not None:
            self.device.pulse = self.last_watch_pulse
        return self.heart_rate_fresh

    def control_step(self, elapsed):
        self.device.status()
        if self.heart_rate_file:
            self.refresh_heart_rate()

        if self.device.rpm < 20 or self.device.pulse < 20:
            sys.stderr.write('.')
            return

        step = self.session.step(elapsed)
        sys.stderr.write('\n%d/%d bpm\t%d W\t%d rpm' % (
            int(self.device.pulse), int(step['pulse']),
            int(self.device.power), self.device.rpm))

        if self.logtemp:
            self.logtemp.write('%d %d %d %d\n' % (
                int(self.device.pulse), int(step['pulse']),
                int(self.device.power), int(self.device.rpm)))
            self.logtemp.flush()

        if self.live_log_file:
            sample = {
                'elapsed': elapsed,
                'pulse': int(self.device.pulse),
                'target_pulse': int(step['pulse'])
            }
            self.live_log_file.write(json.dumps(sample) + '\n')
            self.live_log_file.flush()

        self.pc = elapsed + 1
        if self.heart_rate_file and not self.heart_rate_fresh:
            return

        if step['power'] > 0.0:
            self.device.set_power(absolute=step['power'])
        elif step['pulse'] > 0.0:
            difference = step['pulse'] - self.device.pulse
            if -5.0 <= difference <= 0:
                return

            absolute_difference = abs(difference)
            if absolute_difference > 10.0:
                relative_change = 0.30
            elif absolute_difference > 5.0:
                relative_change = 0.15
            elif absolute_difference > 0.0:
                relative_change = 0.03
            else:
                relative_change = 0.0

            self.device.set_power(
                relative=relative_change if difference > 0 else -relative_change)

    def poll_live_updates(self):
        if not self.live_control:
            return
        try:
            with open(self.live_control, 'rb') as control_file:
                control_file.seek(self.live_control_position)
                chunk = control_file.read()
        except IOError as error:
            sys.stderr.write('\nCould not read live workout updates: %s\n' % error)
            return

        if not chunk:
            return
        newline = chunk.rfind(b'\n')
        if newline < 0:
            return
        complete_chunk = chunk[:newline + 1]
        for line in complete_chunk.splitlines():
            if not line.strip():
                continue
            try:
                plan = json.loads(line)
                if isinstance(plan, dict) and plan.get('stop') is True:
                    self.stop_requested = True
                else:
                    self.session.update_live_plan(plan)
            except (ValueError, TypeError, KeyError) as error:
                sys.stderr.write('\nCould not apply live workout update: %s\n' % error)
            if self.stop_requested:
                break
        self.live_control_position += len(complete_chunk)

    def finish(self):
        print('\nfinishing...')

        if self.live_log_file:
            self.live_log_file.close()
            self.live_log_file = None

        self.device.pulse = 0
        self.session.step(self.pc)['pulse'] = 0
        self.device.set_power(absolute=25)
        self.device.rpm = 0

        if self.log:
            self.logtemp.close()

            fmt = '%Y-%m-%dT%H:%M:%S'
            data = {
                'begin': self.begin.strftime(fmt),
                'end': self.end.strftime(fmt),
                'duration': self.session.duration,
                'name': self.session.name,
                'steps': []
            }
            with open(self.logtemp.name) as log_file:
                for line in log_file.read().split('\n'):
                    if len(line) < 1:
                        continue
                    parts = line.split(' ')
                    data['steps'].append({
                        'pulse': parts[0],
                        'target_pulse': parts[1],
                        'power': parts[2],
                        'rpm': parts[3]
                    })

            with open(self.log, 'w') as output:
                output.write(json.dumps(data))
            os.remove(self.logtemp.name)

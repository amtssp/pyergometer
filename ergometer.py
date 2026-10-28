import datetime
import json
import os
import sys
import tempfile
import time

from bleheartrate import read_shared_json


# Above the pulse limit a watt step is lowered by this much per second, and raised
# back by the recovery amount once the pulse is a few beats below the limit again.
LIMIT_REDUCTION_STEP = 10
LIMIT_RECOVERY_STEP = 5
LIMIT_HYSTERESIS = 5

# Pulse control: watts changed per second for a pulse this many beats from target.
# Far from target the load moves fast; close to it slowly, as the pulse lags behind.
PULSE_CONTROL_STEPS = ((20, 1.0), (10, 0.5), (5, 0.15), (0, 0.03))


class Ergometer:
    def __init__(self, device=None, session=None, log=None, live_log=None,
                 live_control=None, heart_rate_file=None, pulse_limit=None):
        self.device = device
        self.pulse_limit = pulse_limit
        self.limit_reduction = 0
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

            # A watt-controlled start only needs pedalling; a pulse target also needs a pulse.
            needs_pulse = self.session.step(0)['pulse'] > 0
            if self.device.rpm < 20 or (needs_pulse and self.device.pulse < 20):
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
        # With a watch, the bike's own pulse reading is never used: without a chest
        # strap it reports a made-up value (59 bpm).
        self.device.pulse = self.last_watch_pulse if self.last_watch_pulse is not None else 0
        if os.path.exists(self.heart_rate_file) and os.path.getsize(self.heart_rate_file) == 0:
            # The watch has not delivered its first pulse yet (a watt workout starts without it).
            self.heart_rate_fresh = False
            return False
        try:
            sample = read_shared_json(self.heart_rate_file)
            pulse = sample['pulse']
            timestamp = sample['timestamp']
            connected = sample['connected']
            if connected is False and pulse is None:
                # The watch is still connecting and has not measured a pulse yet.
                self.heart_rate_fresh = False
                return False
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
        return self.heart_rate_fresh

    def control_step(self, elapsed):
        self.device.status()
        if self.heart_rate_file:
            self.refresh_heart_rate()

        step = self.session.step(elapsed)
        recovery = step.get('recovery', False)
        # During a pulse-drop measurement the rider sits still, so pedalling is not required.
        if not recovery and (self.device.rpm < 20 or (step['pulse'] > 0 and self.device.pulse < 20)):
            sys.stderr.write('.')
            return

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
                'target_pulse': int(step['pulse']),
                'target_power': int(step['power']),
                'rpm': int(self.device.rpm),
                'power': int(self.device.power),
                'limited': self.limit_reduction > 0,
                'recovery': recovery
            }
            self.live_log_file.write(json.dumps(sample) + '\n')
            self.live_log_file.flush()

        self.pc = elapsed + 1
        if recovery:
            self.device.set_power(absolute=step['power'])
            return
        if step['power'] > 0.0:
            self.device.set_power(absolute=self.limited_power(step['power']))
            return
        self.limit_reduction = 0
        if self.heart_rate_file and not self.heart_rate_fresh:
            return

        if step['pulse'] > 0.0:
            difference = step['pulse'] - self.device.pulse
            if -5.0 <= difference <= 0:
                return

            absolute_difference = abs(difference)
            relative_change = 0.0
            for beats, change in PULSE_CONTROL_STEPS:
                if absolute_difference > beats:
                    relative_change = change
                    break

            self.device.set_power(
                relative=relative_change if difference > 0 else -relative_change)

    def limited_power(self, power):
        """Lower a fixed load while the pulse is above the limit, then restore it gradually."""
        pulse_known = self.device.pulse >= 20 and (
            not self.heart_rate_file or self.heart_rate_fresh)
        if self.pulse_limit and pulse_known:
            if self.device.pulse >= self.pulse_limit:
                self.limit_reduction += LIMIT_REDUCTION_STEP
            elif self.device.pulse < self.pulse_limit - LIMIT_HYSTERESIS:
                self.limit_reduction = max(0, self.limit_reduction - LIMIT_RECOVERY_STEP)
        self.limit_reduction = min(self.limit_reduction, max(0, power - 25))
        return power - self.limit_reduction

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

import datetime
import json
import os
import sys
import tempfile
import time

from bleheartrate import read_shared_json
from liveplan import skip_to_recovery
from pulsemodel import SteadyWindow, estimate_watts, merge_points, valid_points


# Above the pulse limit a watt step is lowered by this much per second, and raised
# back by the recovery amount once the pulse is a few beats below the limit again.
LIMIT_REDUCTION_STEP = 10
LIMIT_RECOVERY_STEP = 5
LIMIT_HYSTERESIS = 5

# In a ramp test, a cadence this low for this many seconds means the rider cannot go
# on, so the workout moves on to its pulse drop as if Stop had been pressed.
EXHAUSTED_RPM = 15
EXHAUSTED_SECONDS = 5

# Pulse control: watts changed per second for a pulse this many beats from target.
# Far from target the load moves fast; close to it slowly, as the pulse lags behind.
# It comes down faster than it goes up: a quick rest is safe, a quick rise tires the legs.
PULSE_CONTROL_STEPS = ((20, 1.0), (10, 0.5), (5, 0.15), (0, 0.03))
PULSE_CONTROL_STEPS_DOWN = ((20, 2.0), (10, 0.75), (5, 0.15), (0, 0.03))
# A watch whose reading hangs makes control keep raising the load. When the pulse has
# not moved more than STALL_BEATS for STALL_SECONDS while the load rose by STALL_WATTS
# since it last moved, the load waits until the pulse moves.
STALL_SECONDS = 40
STALL_BEATS = 2
STALL_WATTS = 15

# When the pulse target changes by this much, the load jumps to the one expected to
# hold the new target (from the rider's steady points) and stays there while the pulse
# travels, until it is within HOLD_BEATS of the target or HOLD_SECONDS have passed.
PHASE_CHANGE_BEATS = 8
HOLD_BEATS = 10
HOLD_SECONDS = 90
# Going up, the load starts a little above the expected one for RISE_EXTRA_SECONDS so
# the lagging pulse rises sooner; going down, below it so the rider gets the rest at once.
RISE_EXTRA = 0.08
RISE_EXTRA_MAX_WATTS = 15
RISE_EXTRA_SECONDS = 45
FALL_SHARE = 0.75
# A target far from every point is a guess, so the load only moves part of the way.
GUESS_SHARE = 0.75


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
        # The chart plan from the workout page, which a jump to the pulse drop changes.
        self.live_plan = None
        self.skipped_at = None
        self.low_cadence_seconds = 0
        # Steady points (pulse, watts) from earlier workouts and from this one.
        self.pulse_points = []
        self.steady_window = SteadyWindow()
        self.last_target = None
        self.hold = None
        # (second, pulse, load) when the pulse last moved, to notice a hanging pulse.
        self.pulse_moved = None

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
        if not recovery and step.get('test') == 'ramp' and self.device.rpm < EXHAUSTED_RPM:
            self.low_cadence_seconds += 1
            if self.low_cadence_seconds >= EXHAUSTED_SECONDS and self.jump_to_recovery(elapsed):
                step = self.session.step(elapsed)
                recovery = step.get('recovery', False)
        else:
            self.low_cadence_seconds = 0
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
        if not self.heart_rate_file or self.heart_rate_fresh:
            point = self.steady_window.add(sample)
            if point:
                self.pulse_points = merge_points(self.pulse_points, [point])
        if self.live_log_file:
            if self.skipped_at is not None:
                # Tells the workout page to move its pulse drop forward the same way.
                sample['skipped_to_recovery'] = self.skipped_at
                self.skipped_at = None
            self.live_log_file.write(json.dumps(sample) + '\n')
            self.live_log_file.flush()

        self.pc = elapsed + 1
        if recovery or step['pulse'] <= 0.0:
            self.last_target = None
            self.hold = None
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
            target = step['pulse']
            if self.last_target is None or abs(target - self.last_target) >= PHASE_CHANGE_BEATS:
                self.start_phase(elapsed, target, first=self.last_target is None)
            self.last_target = target
            if self.pulse_moved is None or abs(self.device.pulse - self.pulse_moved[1]) > STALL_BEATS:
                self.pulse_moved = (elapsed, self.device.pulse, self.device.power)
            if self.hold:
                if self.hold['settle'] is not None and elapsed >= self.hold['settle_at']:
                    # The extra load has had its time; hold the expected load from here.
                    self.device.set_power(absolute=self.hold['settle'])
                    self.hold['settle'] = None
                if abs(target - self.device.pulse) > HOLD_BEATS and elapsed < self.hold['until']:
                    return
                settle = self.hold['settle']
                self.hold = None
                if settle is not None:
                    # The extra load has done its job; go on from the expected one.
                    self.device.set_power(absolute=settle)
                    return

            difference = target - self.device.pulse
            if -5.0 <= difference <= 0:
                return

            if difference > 5 and self.pulse_hangs(elapsed):
                return  # wait for the pulse to follow before adding more load

            absolute_difference = abs(difference)
            relative_change = 0.0
            for beats, change in (PULSE_CONTROL_STEPS if difference > 0 else PULSE_CONTROL_STEPS_DOWN):
                if absolute_difference > beats:
                    relative_change = change
                    break

            self.device.set_power(
                relative=relative_change if difference > 0 else -relative_change)

    def pulse_hangs(self, elapsed):
        """Whether the pulse stood still for STALL_SECONDS while the load kept rising."""
        second, unused_pulse, power = self.pulse_moved
        return elapsed - second >= STALL_SECONDS and self.device.power - power >= STALL_WATTS

    def start_phase(self, elapsed, target, first=False):
        """Jump to the load expected to hold a new pulse target, if the rider's points tell it."""
        self.hold = None
        self.pulse_moved = None  # the jump itself is not a rise the pulse failed to follow
        expected, known = estimate_watts(self.pulse_points, target)
        if expected is None:
            return  # a new rider: the load is found by control alone
        current = float(self.device.power)
        if not known:
            expected = current + GUESS_SHARE * (expected - current)
        pulse = self.device.pulse
        wanted = expected
        settle = None
        if target - pulse > HOLD_BEATS:
            if known and not first:
                wanted = expected + min(expected * RISE_EXTRA, RISE_EXTRA_MAX_WATTS)
                settle = expected
            wanted = max(wanted, current)
        elif pulse - target > HOLD_BEATS:
            wanted = min(expected * FALL_SHARE, current)
        else:
            self.device.set_power(absolute=expected)
            return
        self.device.set_power(absolute=wanted)
        self.hold = {'until': elapsed + HOLD_SECONDS, 'settle': settle,
                     'settle_at': elapsed + RISE_EXTRA_SECONDS}
        sys.stderr.write('\nNew pulse target %d: load %d W (expected %d W%s)\n' % (
            target, wanted, expected, '' if known else ', a guess'))

    def jump_to_recovery(self, elapsed):
        plan = skip_to_recovery(self.live_plan, elapsed) if self.live_plan else None
        if plan is None:
            return False
        self.session.update_live_plan(plan)
        self.live_plan = plan
        self.skipped_at = elapsed
        self.low_cadence_seconds = 0
        sys.stderr.write('\nJumping to the pulse drop at %d s\n' % elapsed)
        return True

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
                elif isinstance(plan, dict) and 'pulse_points' in plan:
                    # What earlier workouts learned; points from this one win.
                    self.pulse_points = merge_points(valid_points(plan['pulse_points']),
                                                     self.pulse_points)
                elif isinstance(plan, dict) and plan.get('skip_to_recovery') is True:
                    # Stop with a pulse drop still to come jumps to it; otherwise it stops.
                    if not hasattr(self, 'begin') or not self.jump_to_recovery(
                            int(time.monotonic() - self.begin_monotonic)):
                        self.stop_requested = True
                else:
                    self.session.update_live_plan(plan)
                    self.live_plan = plan
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

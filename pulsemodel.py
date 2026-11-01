"""Which load holds which pulse for a rider, learned from steady stretches of riding.

A point is a minute where both the pulse and the load stayed nearly the same (after
the load had already stayed put for half a minute), so the load is the one that holds
that pulse. Pulse control uses the points to jump straight
to a fitting load when the target changes, instead of creeping towards it.
"""

STEADY_SECONDS = 60
STEADY_PULSE_RANGE = 6
STEADY_POWER_RANGE = 10
# The pulse lags behind the load, so the load must have stayed put this much longer,
# and the pulse may not still be on its way (first and last 15 s at most 3 beats apart).
SETTLE_SECONDS = 30
TREND_SECONDS = 15
TREND_BEATS = 3
# One point per band of this many beats; a newer point replaces an older one.
BAND_BEATS = 5
# A target this close to a point counts as known; further away the load is a guess.
KNOWN_BEATS = 10
# Watts per beat when the points are too close together to show the slope.
DEFAULT_WATTS_PER_BEAT = 3.0
SLOPE_LIMITS = (1.5, 8.0)
CONFLICT_WATTS = 15
MIN_SLOPE_SPAN = 15
MIN_WATTS = 25
MAX_WATTS = 400


class SteadyWindow(object):
    """Collects samples one second at a time and reports each steady minute."""

    def __init__(self):
        self.samples = []

    def add(self, sample):
        """Add one sample; returns a point when the last minute was steady, else None."""
        usable = (sample.get('pulse', 0) >= 20 and sample.get('power', 0) >= MIN_WATTS and
                  sample.get('rpm', 60) >= 20 and
                  not sample.get('recovery') and not sample.get('limited'))
        if not usable or (self.samples and sample['elapsed'] != self.samples[-1]['elapsed'] + 1):
            self.samples = []
        if not usable:
            return None
        self.samples.append(sample)
        if len(self.samples) < SETTLE_SECONDS + STEADY_SECONDS:
            return None
        loads = [s['power'] for s in self.samples]
        measured = self.samples[SETTLE_SECONDS:]
        pulses = [s['pulse'] for s in measured]
        trend = (sum(pulses[-TREND_SECONDS:]) - sum(pulses[:TREND_SECONDS])) / float(TREND_SECONDS)
        if (max(loads) - min(loads) <= STEADY_POWER_RANGE and
                max(pulses) - min(pulses) <= STEADY_PULSE_RANGE and abs(trend) <= TREND_BEATS):
            self.samples = []
            powers = [s['power'] for s in measured]
            return {'pulse': round(float(sum(pulses)) / len(pulses), 1),
                    'watts': round(float(sum(powers)) / len(powers), 1)}
        self.samples.pop(0)
        return None


def steady_points(samples):
    """The steady points in one workout's samples."""
    window = SteadyWindow()
    points = []
    for sample in samples:
        point = window.add(sample)
        if point:
            points.append(point)
    return merge_points([], points)


def merge_points(older, newer):
    """Points from both lists, one per pulse band, the newer winning; sorted by pulse.

    A higher pulse needs a clearly higher load. Two points that disagree (a pulse
    reading that hung, say) cannot both be right, so the older of them is dropped.
    """
    bands = {}
    for order, point in enumerate(list(older) + list(newer)):
        bands[int(round(point['pulse'] / BAND_BEATS))] = (order, point)
    kept = sorted(bands.values(), key=lambda item: item[1]['pulse'])
    conflict = True
    while conflict:
        conflict = False
        for low, high in zip(kept, kept[1:]):
            beats = high[1]['pulse'] - low[1]['pulse']
            if high[1]['watts'] - low[1]['watts'] < SLOPE_LIMITS[0] * beats - CONFLICT_WATTS:
                kept.remove(min(low, high, key=lambda item: item[0]))
                conflict = True
                break
    return [point for order, point in kept]


def valid_points(points):
    """Points from outside (a control message), keeping only well-formed ones."""
    if not isinstance(points, list):
        return []
    return merge_points([], [
        {'pulse': float(point['pulse']), 'watts': float(point['watts'])} for point in points
        if isinstance(point, dict) and
        all(isinstance(point.get(key), (int, float)) and not isinstance(point.get(key), bool)
            for key in ('pulse', 'watts')) and
        20 <= point['pulse'] <= 250 and MIN_WATTS <= point['watts'] <= MAX_WATTS])


def _slope(points):
    if len(points) < 2 or points[-1]['pulse'] - points[0]['pulse'] < MIN_SLOPE_SPAN:
        return DEFAULT_WATTS_PER_BEAT
    mean_pulse = sum(p['pulse'] for p in points) / len(points)
    mean_watts = sum(p['watts'] for p in points) / len(points)
    spread = sum((p['pulse'] - mean_pulse) ** 2 for p in points)
    slope = sum((p['pulse'] - mean_pulse) * (p['watts'] - mean_watts) for p in points) / spread
    return max(SLOPE_LIMITS[0], min(SLOPE_LIMITS[1], slope))


def estimate_watts(points, pulse):
    """The load expected to hold ``pulse``, and whether a point lies close to it.

    Returns (None, False) without points.
    """
    if not points:
        return None, False
    ordered = sorted(points, key=lambda point: point['pulse'])
    known = min(abs(point['pulse'] - pulse) for point in ordered) <= KNOWN_BEATS
    watts = None
    for low, high in zip(ordered, ordered[1:]):
        if low['pulse'] <= pulse <= high['pulse'] and high['pulse'] > low['pulse']:
            share = (pulse - low['pulse']) / (high['pulse'] - low['pulse'])
            watts = low['watts'] + share * (high['watts'] - low['watts'])
            break
    if watts is None:
        end = ordered[0] if pulse < ordered[0]['pulse'] else ordered[-1]
        watts = end['watts'] + _slope(ordered) * (pulse - end['pulse'])
    return max(MIN_WATTS, min(MAX_WATTS, watts)), known

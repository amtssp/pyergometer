import math

# Load during a pulse-drop measurement: the ergometer's minimum while the rider sits still.
RECOVERY_POWER = 25


def _whole_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be a whole number' % name)
    if value < minimum or value > maximum:
        raise ValueError('%s must be between %d and %d' % (name, minimum, maximum))
    return value


def ramp_value(start, end, duration, second):
    """Value of a linear ramp ``second`` seconds into a ramp lasting ``duration`` seconds."""
    return int(math.floor(start + (end - start) * float(second) / duration + 0.5))


def skip_to_recovery(plan, elapsed):
    """The chart plan with the pulse drop moved forward to start at ``elapsed``.

    The block running at ``elapsed`` ends there, the blocks before the next pulse
    drop are left out and the rest moves forward. Returns None when no pulse drop
    follows, or when one is already running.
    """
    blocks = plan['blocks']
    if any(block['type'] == 'recovery' and block['start'] <= elapsed < block['end']
           for block in blocks):
        return None
    target = next((index for index, block in enumerate(blocks)
                   if block['type'] == 'recovery' and block['start'] > elapsed), None)
    if target is None:
        return None
    shift = blocks[target]['start'] - elapsed
    kept = []
    for index, block in enumerate(blocks):
        block = dict(block)
        if block['end'] <= elapsed:
            kept.append(block)
        elif index < target and block['start'] < elapsed:
            if block['type'] == 'ramp':
                # Keep the ramp's slope; it only stops early.
                block['end_value'] = ramp_value(block['start_value'], block['end_value'],
                                                block['end'] - block['start'],
                                                elapsed - block['start'])
            block['end'] = elapsed
            kept.append(block)
        elif index >= target:
            block['start'] -= shift
            block['end'] -= shift
            kept.append(block)
    result = dict(plan)
    result['blocks'] = kept
    result['duration'] = plan['duration'] - shift
    return result


def session_steps(plan):
    """Expand a chart plan into one {'time', 'power', 'pulse'} step per second.

    Block types: 'interval' (pulse target), 'break' (pause at a fixed load),
    'power' (fixed load in a watt-controlled workout), 'ramp' (a pulse or
    power target that changes linearly from start_value to end_value) and
    'recovery' (sit still at the minimum load while the pulse drop is measured;
    its steps carry 'recovery': True).
    """
    if not isinstance(plan, dict):
        raise ValueError('Live workout plan must be an object')
    duration = _whole_number(plan.get('duration'), 'Workout duration', 1, 43200)
    blocks = plan.get('blocks')
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('Live workout plan must contain blocks')

    steps = []
    elapsed = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError('Live workout block %d is invalid' % (index + 1))
        start = _whole_number(block.get('start'), 'Block start', 0, duration)
        end = _whole_number(block.get('end'), 'Block end', 1, duration)
        if start != elapsed or end <= start:
            raise ValueError('Live workout blocks must be contiguous and ordered')

        block_type = block.get('type')
        if block_type == 'interval':
            pulse = _whole_number(block.get('pulse'), 'Target pulse', 1, 220)
            values = [(0, pulse)] * (end - start)
        elif block_type == 'break':
            power = _whole_number(block.get('power'), 'Break load', 25, 400)
            values = [(power, 0)] * (end - start)
        elif block_type == 'power':
            power = _whole_number(block.get('power'), 'Load', 25, 1000)
            values = [(power, 0)] * (end - start)
        elif block_type == 'recovery':
            values = [(RECOVERY_POWER, 0)] * (end - start)
        elif block_type == 'ramp':
            kind = block.get('kind')
            if kind == 'pulse':
                low, high = 1, 220
            elif kind == 'power':
                low, high = 25, 1000
            else:
                raise ValueError('Live workout ramp %d has an invalid kind' % (index + 1))
            first = _whole_number(block.get('start_value'), 'Ramp start', low, high)
            last = _whole_number(block.get('end_value'), 'Ramp end', low, high)
            values = []
            for second in range(end - start):
                value = ramp_value(first, last, end - start, second)
                values.append((value, 0) if kind == 'power' else (0, value))
        else:
            raise ValueError('Live workout block %d has an invalid type' % (index + 1))

        recovery = block_type == 'recovery'
        test = block.get('test')
        for offset, (power, pulse) in enumerate(values):
            step = {'time': start + offset, 'power': power, 'pulse': pulse,
                    'recovery': recovery}
            if test:
                step['test'] = test
            steps.append(step)
        elapsed = end

    if elapsed != duration:
        raise ValueError('Live workout blocks must cover the full duration')
    return steps

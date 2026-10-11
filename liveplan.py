import math


def _whole_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be a whole number' % name)
    if value < minimum or value > maximum:
        raise ValueError('%s must be between %d and %d' % (name, minimum, maximum))
    return value


def ramp_value(start, end, duration, second):
    """Value of a linear ramp ``second`` seconds into a ramp lasting ``duration`` seconds."""
    return int(math.floor(start + (end - start) * float(second) / duration + 0.5))


def session_steps(plan):
    """Expand a chart plan into one {'time', 'power', 'pulse'} step per second.

    Block types: 'interval' (pulse target), 'break' (pause at a fixed load),
    'power' (fixed load in a watt-controlled workout) and 'ramp' (a pulse or
    power target that changes linearly from start_value to end_value).
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

        for offset, (power, pulse) in enumerate(values):
            steps.append({'time': start + offset, 'power': power, 'pulse': pulse})
        elapsed = end

    if elapsed != duration:
        raise ValueError('Live workout blocks must cover the full duration')
    return steps

def _whole_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be a whole number' % name)
    if value < minimum or value > maximum:
        raise ValueError('%s must be between %d and %d' % (name, minimum, maximum))
    return value


def session_steps(plan):
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
            power = 0
        elif block_type == 'break':
            power = _whole_number(block.get('power'), 'Break load', 25, 400)
            pulse = 0
        else:
            raise ValueError('Live workout block %d has an invalid type' % (index + 1))

        for second in range(start, end):
            steps.append({'time': second, 'power': power, 'pulse': pulse})
        elapsed = end

    if elapsed != duration:
        raise ValueError('Live workout blocks must cover the full duration')
    return steps

"""Fail closed over every explicit authorization producer, without raw output."""
import os

from ui_common import loads, require

PRODUCERS = frozenset(
    ('authorization-sqlite', 'authorization-postgres-review', 'authorization-postgres-comparison')
)
LIMIT = 16 * 1024


def validate(raw):
    value = loads(raw, LIMIT)
    require(type(value) is dict and set(value) == PRODUCERS, 'Authorization producer set mismatch')
    for producer in PRODUCERS:
        result = value[producer]
        require(
            type(result) is dict
            and set(result) == {'result', 'outputs'}
            and type(result['outputs']) is dict
            and result['result'] == 'success',
            'Authorization producer not successful',
        )
    return True


def main():
    try:
        validate(os.environ.get('AUTHORIZATION_NEEDS_JSON', '').encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        print('AUTHORIZATION_GATE_REJECTED')
        return 1
    print('AUTHORIZATION_GATE_SUCCESS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

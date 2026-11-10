"""Deterministic classification gold scoring, with no persistence or authority.

Callers must establish trusted reference provenance, equal task/configuration
and current authorization before invoking this pure comparison.
"""

from types import SimpleNamespace

from . import service

RULE = 'classification-gold-exact-controls/v1'


def score(config, task_data, reference_result, submission_result):
    """Score nonempty classification controls equally against a reference."""
    try:
        schema = service.controls(config)

        def normalize(result):
            row = SimpleNamespace(result_snapshot={
                'task': {'data': task_data},
                'annotation': {'was_cancelled': False, 'result': result},
            })
            return service.answers(row, schema)

        expected = normalize(reference_result)
        actual = normalize(submission_result)
        controls = [
            {'from_name': name, 'to_name': control['target'],
             'expected': expected[name], 'actual': actual[name],
             'matched': expected[name] == actual[name]}
            for name, control in sorted(schema.items())
        ]
        return {'rule': RULE, 'status': 'scored', 'reason': None,
                'matched_controls': sum(item['matched'] for item in controls),
                'total_controls': len(controls), 'controls': controls}
    except (service.Incomparable, TypeError, ValueError, KeyError, AttributeError) as exc:
        reason = exc.reason if isinstance(exc, service.Incomparable) else 'missing_or_malformed'
        return {'rule': RULE, 'status': 'unavailable', 'reason': reason,
                'matched_controls': None, 'total_controls': None, 'controls': []}

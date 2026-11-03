import contextlib
import io
import itertools
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import authorization_gate as gate


class AuthorizationGateTests(unittest.TestCase):
    def payload(self, states=('success',) * 3):
        return {name: {'result': state, 'outputs': {}} for name, state in zip(sorted(gate.PRODUCERS), states)}

    def test_all_64_real_dependency_combinations_only_all_success_passes(self):
        states = ('success', 'failure', 'cancelled', 'skipped')
        passed = 0
        for values in itertools.product(states, repeat=3):
            with self.subTest(values=values):
                raw = json.dumps(self.payload(values)).encode()
                if values == ('success',) * 3:
                    self.assertTrue(gate.validate(raw))
                    passed += 1
                else:
                    with self.assertRaises(ValueError):
                        gate.validate(raw)
        self.assertEqual(passed, 1)

    def test_missing_extra_unknown_or_weakly_typed_dependency_denies(self):
        ordinary = self.payload()
        variants = [{}, [], {**ordinary, 'extra': {'result': 'success', 'outputs': {}}}]
        for name in ordinary:
            variants.append({k: v for k, v in ordinary.items() if k != name})
            for state in ('unknown', 'neutral', 'timed_out', None, True, 1):
                variants.append({**ordinary, name: {'result': state, 'outputs': {}}})
            for node in (None, 'success', {'result': 'success'}, {'result': 'success', 'outputs': []}):
                variants.append({**ordinary, name: node})
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate.validate(json.dumps(value).encode())

    def test_duplicate_malformed_and_byte_overflow_reject_without_normalizing(self):
        raw = json.dumps(self.payload()).encode()
        self.assertTrue(gate.validate(raw + b' ' * (gate.LIMIT - len(raw))))
        for value in (raw + b' ' * (gate.LIMIT + 1 - len(raw)), b'{', b'NaN', b'\xff', b'{"a":1,"a":1}'):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                gate.validate(value)

    def test_entrypoint_exit_and_output_never_disclose_payload(self):
        for payload, expected in ((json.dumps(self.payload()), 0), ('{"sensitive":"NEVER_PRINT"}', 1), ('', 1)):
            output = io.StringIO()
            with patch.dict(os.environ, {'AUTHORIZATION_NEEDS_JSON': payload}), contextlib.redirect_stdout(output):
                self.assertEqual(gate.main(), expected)
            self.assertNotIn('NEVER_PRINT', output.getvalue())
            self.assertEqual(output.getvalue().strip(), 'AUTHORIZATION_GATE_SUCCESS' if expected == 0 else 'AUTHORIZATION_GATE_REJECTED')


if __name__ == '__main__':
    unittest.main()

import copy
import unittest
from unittest.mock import patch

from classification_disagreements import fields

XML = '<View><Text name="doc" value="$text"/><TextArea name="name" toName="doc" maxSubmissions="1"/><TextArea name="city" toName="doc" maxSubmissions="1"/><TextArea name="code" toName="doc" maxSubmissions="1"/></View>'


def snapshot(values=(' 张三 ', '上海\r\n浦东', '001')):
    return {
        'project': {'id': 1, 'label_config': XML},
        'task': {'id': 2, 'data': {'text': 'Synthetic 😀 document'}},
        'annotation': {
            'was_cancelled': False,
            'result': [
                {'id': str(i), 'type': 'textarea', 'from_name': name, 'to_name': 'doc', 'value': {'text': [v]}}
                for i, (name, v) in enumerate(zip(('name', 'city', 'code'), values))
            ],
        },
    }


def run(*rows):
    return fields.public(fields.prepare(list(range(1, len(rows) + 1)), rows), (1, 2))


class TestFields(unittest.TestCase):
    def test_formula_and_three_people_pairs(self):
        r = run(snapshot(), snapshot(('张三', '上海\n浦东', '1')), snapshot())
        self.assertEqual(r['pairs'][0]['raw_rate'], fields.fraction(0, 3))
        self.assertEqual(r['pairs'][0]['normalized_rate'], fields.fraction(2, 3))
        self.assertEqual(r['pairs'][1]['raw_rate'], fields.fraction(3, 3))
        self.assertEqual(r['total_pairs'], 3)
        self.assertEqual(r['cost_per_pass'], {'normalized_values': 9, 'field_equalities': 18})
        self.assertEqual(
            [e['kind'] for e in r['selected_pair']['fields']], ['format_only', 'value_difference', 'format_only']
        )

    def test_empty_missing_and_uninformative_not_perfect(self):
        empty = snapshot(('', ' \t\v\f\r\n', ''))
        r = run(empty, empty)
        self.assertEqual(r['reason'], 'no_values')
        self.assertIsNone(r['pairs'][0]['raw_rate'])
        self.assertEqual(r['pairs'][0]['excluded_empty_fields'], 3)
        missing = snapshot(('a', 'b'))
        r = run(snapshot(), missing, snapshot())
        self.assertEqual(r['reason'], 'missing_field')
        self.assertIsNone(r['pairs'])
        self.assertEqual(r['total_pairs'], 3)
        self.assertEqual(r['configured_fields'], 3)
        one = snapshot(('', '', 'a'))
        r = run(empty, one)
        self.assertEqual(r['pairs'][0]['denominator'], 1)
        self.assertEqual(r['pairs'][0]['raw_rate'], fields.fraction(0, 1))

    def test_normalization_is_exactly_versioned(self):
        for a, b in [
            ('A', 'a'),
            ('001', '1'),
            ('2026/01/01', '2026-01-01'),
            ('é', 'e\u0301'),
            ('x  y', 'x y'),
            ('\u00a0x', 'x'),
        ]:
            self.assertNotEqual(fields.normalize(a), fields.normalize(b))
        self.assertEqual(fields.normalize('\t a\r\nb\rc \f'), 'a\nb\nc')

    def test_no_silent_type_coercion_duplicates_or_missing(self):
        for value in (None, [], ['x', 'y'], [1], 'x'):
            row = snapshot()
            row['annotation']['result'][0]['value']['text'] = value
            self.assertEqual(run(row, snapshot())['reason'], 'missing_or_malformed')
        row = snapshot()
        row['annotation']['result'][1] = row['annotation']['result'][0]
        self.assertEqual(run(row, snapshot())['reason'], 'missing_or_malformed')

    def test_rejects_dynamic_region_media_mixed_and_nonidentical_sources(self):
        for before, after in [
            ('maxSubmissions="1"', ''),
            ('value="$text"', 'value="$text suffix"'),
            ('value="$text"', 'value="$__proto__"'),
            ('<TextArea name="name"', '<TextArea perRegion="true" name="name"'),
            ('<TextArea name="name"', '<TextArea perItem="true" name="name"'),
            ('<Text name="doc"', '<Text encoding="base64" name="doc"'),
            ('<TextArea name="name"', '<TextArea visibleWhen="choice-selected" name="name"'),
        ]:
            row = snapshot()
            row['project']['label_config'] = XML.replace(before, after)
            self.assertEqual(run(row, copy.deepcopy(row))['reason'], 'unsupported_config')
        row = snapshot()
        row['project']['label_config'] += ' '
        self.assertEqual(run(row, snapshot())['reason'], 'config_mismatch')
        row = snapshot()
        row['task']['data']['other'] = 'different'
        self.assertEqual(run(row, snapshot())['reason'], 'task_data_mismatch')

    def test_maximum_cohort_field_cost_and_values_budget(self):
        row = snapshot()
        row['project']['label_config'] = (
            '<View><Text name="doc" value="$text"/>'
            + ''.join(f'<TextArea name="f{i}" toName="doc" maxSubmissions="1"/>' for i in range(20))
            + '</View>'
        )
        row['annotation']['result'] = [
            {
                'type': 'textarea',
                'from_name': f'f{i}',
                'to_name': 'doc',
                'value': {'text': ['x' * 4096 if i < 16 else '']},
            }
            for i in range(20)
        ]
        prepared = fields.prepare(list(range(1, 21)), [row] * 20)
        public = fields.public(prepared, (1, 20))
        self.assertEqual(public['cost_per_pass'], {'normalized_values': 400, 'field_equalities': 7600})
        self.assertEqual(public['pairs'][0]['denominator'], 16)
        with patch.object(fields, 'MAX_VALUES_BYTES', 65535):
            with self.assertRaises(fields.Error):
                fields.prepare([1, 2], [row, row])
        row['project']['label_config'] = row['project']['label_config'].replace(
            'maxSubmissions="1"', 'maxSubmissions="2"'
        )
        self.assertEqual(run(row, row)['reason'], 'unsupported_config')

    def test_inclusive_value_document_input_and_total_budgets(self):
        row = snapshot(('x' * 4096, 'b', 'c'))
        self.assertEqual(run(row, row)['availability'], 'available')
        for constant, limit in [
            ('MAX_VALUE_BYTES', 4095),
            ('MAX_VALUES_BYTES', 4097),
            ('MAX_TEXT_BYTES', 1),
            ('MAX_FIELDS', 2),
            ('MAX_INPUTS', 1),
            ('MAX_XML_BYTES', 1),
            ('MAX_SNAPSHOT_BYTES', 1),
            ('MAX_OUTPUT', 1),
        ]:
            with self.subTest(constant=constant), patch.object(fields, constant, limit):
                with self.assertRaises(fields.Error):
                    run(row, row)
        prepared = fields.prepare(list(range(1, 21)), [snapshot()] * 20)
        r = fields.public(prepared, (1, 20))
        self.assertEqual(len(r['pairs']), 190)
        self.assertEqual(r['cost_per_pass']['field_equalities'], 1140)


if __name__ == '__main__':
    unittest.main()

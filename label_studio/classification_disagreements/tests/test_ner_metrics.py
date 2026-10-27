"""Also runnable with unittest and no Django/settings/database."""
import copy
import itertools
import random
import unittest
from unittest.mock import patch

from classification_disagreements import ner

XML = '<View><Labels name="ner" toName="text"><Label value="PER"/><Label value="ORG"/><Label value="人"/></Labels><Text name="text" value="$text"/></View>'
TEXT = 'Alice joined Acme 😀 李 e\u0301\r\n尾'


def snapshot(spans=(), text=TEXT):
    return {'project': {'id': 1, 'label_config': XML}, 'task': {'id': 2, 'data': {'text': text}},
            'annotation': {'was_cancelled': False, 'result': [
                {'id': str(i), 'type': 'labels', 'from_name': 'ner', 'to_name': 'text',
                 'value': {'start': start, 'end': end, 'labels': [label], 'text': text[start:end]}}
                for i, (start, end, label) in enumerate(spans)]}}


def run(*rows):
    prepared = ner.prepare(list(range(1, len(rows) + 1)), list(rows))
    ner.check_engine(prepared)
    return prepared


class TestNerMetrics(unittest.TestCase):
    def test_exact_and_shifted_and_three_person_denominators(self):
        a = snapshot([(0, 5, 'PER'), (13, 17, 'ORG')])
        b = snapshot([(0, 5, 'PER'), (13, 16, 'ORG')])
        c = snapshot([(0, 5, 'PER')])
        prepared = run(a, b, c)
        self.assertEqual([v['f1'] for v in prepared['logical']['pairs']],
                         [ner.fraction(1, 2), ner.fraction(2, 3), ner.fraction(2, 3)])
        self.assertEqual(prepared['logical']['total_pairs'], 3)
        self.assertEqual(prepared['logical']['details'][0]['groups'][0]['kind'], 'boundary_difference')
        self.assertNotIn('average', prepared['logical'])

    def test_empty_is_not_missing_and_never_perfect(self):
        a, empty = snapshot([(0, 5, 'PER')]), snapshot()
        self.assertEqual(run(empty, a)['logical']['pairs'][0]['f1'], ner.fraction(0, 1))
        result = run(empty, empty, a)['logical']
        self.assertEqual(result['comparable_pairs'], 2)
        self.assertEqual(result['unavailable_pairs'], 1)
        self.assertIsNone(result['pairs'][0]['f1'])
        self.assertEqual(run(empty, empty)['logical']['reason'], 'no_entities')
        self.assertEqual(run(a)['logical']['reason'], 'insufficient_inputs')
        missing = snapshot()
        missing['annotation'].pop('result')
        self.assertEqual(run(a, missing)['logical']['reason'], 'missing_or_malformed')

    def test_explanation_is_symmetric_and_ambiguous_not_greedily_scored(self):
        cases = [([(0, 5, 'PER')], [(0, 5, 'ORG')], 'label_difference'),
                 ([(0, 5, 'PER')], [(1, 5, 'ORG')], 'boundary_and_label_difference'),
                 ([(0, 5, 'PER')], [(0, 2, 'PER'), (2, 5, 'PER')], 'ambiguous_overlap')]
        for left, right, kind in cases:
            with self.subTest(kind=kind):
                result = run(snapshot(left), snapshot(right))['logical']
                self.assertEqual(result['pairs'][0]['matching_entities'], 0)
                self.assertEqual(result['pairs'][0]['f1'], ner.fraction(0, 1))
                self.assertEqual(result['details'][0]['groups'][0]['kind'], kind)
                self.assertEqual(run(snapshot(right), snapshot(left))['logical']['pairs'][0]['f1'],
                                 result['pairs'][0]['f1'])
        detail = run(snapshot([(0, 2, 'PER')]), snapshot([(2, 5, 'PER')]))['logical']['details'][0]
        self.assertEqual([v['kind'] for v in detail['groups']], ['left_only', 'right_only'])

    def test_codepoints_combining_characters_crlf_and_optional_html_are_literal(self):
        text = 'A😀李e\u0301\r\n<&尾'
        spans = [(1, 2, 'PER'), (3, 5, 'ORG'), (7, 9, '人')]
        first = snapshot(spans, text)
        result = run(first, copy.deepcopy(first))['logical']
        self.assertEqual(result['pairs'][0]['f1'], ner.fraction(1, 1))
        self.assertEqual(result['text'], text)
        invalid = copy.deepcopy(first)
        invalid['annotation']['result'][0]['value']['end'] = 3
        self.assertEqual(run(first, invalid)['logical']['reason'], 'invalid_span')

    def test_malformed_entities_never_produce_partial_scores(self):
        first = snapshot([(0, 5, 'PER')])
        mutations = [lambda v: v.update(start=True), lambda v: v.update(end=5.0),
                     lambda v: v.update(start=-1), lambda v: v.update(end=1000),
                     lambda v: v.update(start=5), lambda v: v.pop('text'),
                     lambda v: v.update(labels=['PER', 'ORG']), lambda v: v.update(labels=['unknown']),
                     lambda v: v.update(text='different'), lambda v: v.update(startOffset=0)]
        for mutate in mutations:
            item = copy.deepcopy(first)
            mutate(item['annotation']['result'][0]['value'])
            with self.subTest(mutate=mutate):
                self.assertEqual(run(first, item)['logical']['availability'], 'unavailable')
                self.assertIsNone(run(first, item)['logical']['pairs'])
        for spans in ([(0, 5, 'PER'), (0, 5, 'PER')], [(0, 5, 'PER'), (1, 2, 'ORG')]):
            self.assertEqual(run(first, snapshot(spans))['logical']['reason'], 'overlapping_or_duplicate_spans')

    def test_configuration_and_text_mismatch_do_not_select_a_common_subset(self):
        first = snapshot([(0, 5, 'PER')])
        configs = [XML.replace('<Text ', '<Text valueType="url" '),
                   XML.replace('<Text ', '<Text encoding="base64" '),
                   XML.replace('<Labels ', '<Labels choice="multiple" '),
                   XML.replace('<Labels ', '<Labels visibleWhen="x" '),
                   XML.replace('<Label value="PER"', '<Label alias="P" value="PER"'),
                   XML.replace('</View>', '<Choices name="other" toName="text"><Choice value="x"/></Choices></View>')]
        for xml in configs:
            rows = [copy.deepcopy(first), copy.deepcopy(first)]
            for row in rows:
                row['project']['label_config'] = xml
            self.assertEqual(run(*rows)['logical']['reason'], 'unsupported_config')
        second = snapshot([(0, 5, 'PER')], TEXT + 'new')
        self.assertEqual(run(first, second)['logical']['reason'], 'task_data_mismatch')
        second = copy.deepcopy(first)
        second['project']['label_config'] += ' '
        self.assertEqual(run(first, second)['logical']['reason'], 'config_mismatch')
        first['annotation']['was_cancelled'] = True
        self.assertEqual(run(first, first)['logical']['reason'], 'cancelled_or_missing')

    def test_interpolated_or_path_values_never_read_a_different_literal_data_key(self):
        # The editor resolves "$text suffix" as interpolation, not a literal key.
        # A matching prefix span does not prove the rest of that text was shown.
        for value in ('$text suffix', '$text-extra', '$document.text', '$items[0]', '$文本', '$__proto__'):
            first = snapshot([(0, 5, 'PER')], 'Alice hidden context')
            first['project']['label_config'] = XML.replace('value="$text"', f'value="{value}"')
            first['task']['data'][value[1:]] = 'Alice hidden context'
            first['task']['data']['text'] = 'Alice'
            with self.subTest(value=value), patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
                result = run(first, copy.deepcopy(first))['logical']
                self.assertEqual(result['availability'], 'unavailable')
                self.assertEqual(result['reason'], 'unsupported_config')
                self.assertIsNone(result['pairs'])
                engine.assert_not_called()

    def test_exact_byte_entity_work_and_output_edges_precede_engine(self):
        first = snapshot([(0, 5, 'PER')])
        size = len(ner.canonical(first)) * 2
        with patch.object(ner, 'MAX_SNAPSHOT_BYTES', size):
            run(first, first)
        for name, maximum in [('MAX_SNAPSHOT_BYTES', size - 1), ('MAX_TEXT_BYTES', len(TEXT.encode()) - 1),
                              ('MAX_XML_BYTES', len(XML.encode()) - 1), ('MAX_ENTITIES', 0)]:
            with self.subTest(name=name), patch.object(ner, name, maximum), patch.object(ner, 'Evaluator') as engine:
                with self.assertRaises(ner.Error):
                    run(first, first)
                engine.assert_not_called()
        prepared = run(first, first)
        with patch.object(ner, 'MAX_WORK', prepared['work_units']):
            run(first, first)
        with patch.object(ner, 'MAX_WORK', prepared['work_units'] - 1), patch.object(ner, 'Evaluator') as engine:
            with self.assertRaises(ner.Error):
                run(first, first)
            engine.assert_not_called()
        size = len(ner.canonical(prepared['logical']))
        self.assertEqual(ner.bounded(prepared['logical'], size), size)
        with self.assertRaises(ner.Error):
            ner.bounded(prepared['logical'], size - 1)
        with patch.object(ner, 'MAX_OUTPUT', 10), patch.object(ner, 'Evaluator') as engine:
            with self.assertRaises(ner.Error):
                run(first, first)
            engine.assert_not_called()

    def test_scorer_call_count_and_fail_closed_contract(self):
        rows = [snapshot(), snapshot(), snapshot([(0, 5, 'PER')])]
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            prepared = run(*rows)
            self.assertEqual(engine.call_count, 2)
            ner.check_engine(prepared)
            self.assertEqual(engine.call_count, 4)
        with patch.object(ner, 'Evaluator') as engine:
            engine.return_value.evaluate.return_value = ({'strict': {'correct': 100}},)
            with self.assertRaisesRegex(ner.Error, ''):
                run(snapshot([(0, 5, 'PER')]), snapshot([(0, 5, 'PER')]))

    def test_deterministic_finite_oracle_for_pinned_library_and_permutations(self):
        rng = random.Random(81)
        text = 'x' * 40
        for case in range(128):
            samples = [[(pos, pos + 1, rng.choice(['PER', 'ORG', '人']))
                        for pos in sorted(rng.sample(range(40), rng.randrange(12)))] for _ in range(3)]
            rows = [snapshot(spans, text) for spans in samples]
            result = run(*rows)['logical']
            for score, (a, b) in zip(result['pairs'], itertools.combinations(range(3), 2)):
                m = len(set(samples[a]) & set(samples[b]))
                expected = ner.fraction(2 * m, len(samples[a]) + len(samples[b])) if samples[a] or samples[b] else None
                self.assertEqual(score['f1'], expected, case)
            reversed_entities = copy.deepcopy(rows)
            for row in reversed_entities:
                row['annotation']['result'].reverse()
            self.assertEqual(run(*reversed_entities)['logical'], result)

    def test_pair_projection_does_not_return_other_raw_entities(self):
        prepared = run(snapshot([(0, 5, 'PER')]), snapshot(), snapshot([(13, 17, 'ORG')]))
        public = ner.public(prepared, (1, 3))
        self.assertNotIn('sources', public)
        self.assertNotIn('details', public)
        self.assertEqual(public['selected_pair']['left'], 1)
        self.assertEqual(public['selected_pair']['right'], 3)
        with self.assertRaises(ner.Error):
            ner.public(prepared, (1, 99))

    def test_actual_entity_text_and_maximum_pair_call_boundaries(self):
        first = snapshot([(i, i + 1, 'PER') for i in range(200)], 'x' * 201)
        self.assertEqual(run(first, first)['logical']['pairs'][0]['matching_entities'], 200)
        excess = snapshot([(i, i + 1, 'PER') for i in range(201)], 'x' * 201)
        with patch.object(ner, 'Evaluator') as engine:
            with self.assertRaises(ner.Error):
                run(first, excess)
            engine.assert_not_called()
        first = snapshot([(0, 1, 'PER')], '😀' * 16384)
        self.assertEqual(run(first, first)['logical']['pairs'][0]['f1'], ner.fraction(1, 1))
        excess = snapshot([(0, 1, 'PER')], '😀' * 16384 + 'x')
        with self.assertRaises(ner.Error):
            run(excess, excess)
        first = snapshot([(0, 1, 'PER')])
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            prepared = run(*[first for _ in range(20)])
            self.assertEqual(len(prepared['logical']['pairs']), 190)
            ner.check_engine(prepared)
            self.assertEqual(engine.call_count, 380)

    def test_actual_label_and_xml_budget_edges(self):
        def configured(xml):
            first = snapshot([(0, 5, 'PER')])
            first['project']['label_config'] = xml
            return first
        labels = ''.join(f'<Label value="L{i}"/>' for i in range(98))
        xml = XML.replace('<Label value="人"/>', labels)
        self.assertEqual(run(configured(xml), configured(xml))['logical']['pairs'][0]['f1'], ner.fraction(1, 1))
        excess = xml.replace('</Labels>', '<Label value="extra"/></Labels>')
        with self.assertRaises(ner.Error):
            run(configured(excess), configured(excess))
        xml = XML + ' ' * (65536 - len(XML.encode()))
        self.assertEqual(run(configured(xml), configured(xml))['logical']['availability'], 'available')
        with self.assertRaises(ner.Error):
            run(configured(xml + ' '), configured(xml + ' '))
        with self.assertRaises(ner.Error):
            run(*[snapshot() for _ in range(21)])

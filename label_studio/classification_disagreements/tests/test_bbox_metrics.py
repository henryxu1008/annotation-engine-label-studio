"""Golden contracts for the still-unintegrated bbox comparator."""
import itertools
import random
from copy import deepcopy
from fractions import Fraction
from io import BytesIO

import pytest
from classification_disagreements import bbox
from PIL import Image

XML = '<View><Image name="img" value="$image"/><RectangleLabels name="box" toName="img"><Label value="Car"/><Label value="Bus"/></RectangleLabels></View>'


def snapshot(regions):
    return {'project': {'id': 1, 'label_config': XML}, 'task': {'id': 1, 'data': {'image': '/data/upload/test.png'}},
            'annotation': {'was_cancelled': False, 'result': regions}}


def image_bytes():
    out = BytesIO()
    Image.new('RGB', (100, 80), 'white').save(out, format='PNG')
    return out.getvalue()


def box(x=0, y=0, width=20, height=20, label='Car'):
    return {'type': 'rectanglelabels', 'from_name': 'box', 'to_name': 'img', 'original_width': 100,
            'original_height': 80, 'image_rotation': 0,
            'value': {'x': x, 'y': y, 'width': width, 'height': height,
                      'rotation': 0, 'rectanglelabels': [label]}}


def result(*items):
    return bbox.prepare(list(range(1, len(items) + 1)), [snapshot(item) for item in items], image_bytes())


def test_exact_iou_threshold_label_and_three_pairs():
    first, second, third = [box()], [box(width=10)], [box(label='Bus')]
    outcome = result(first, second, third)
    assert outcome['availability'] == 'available' and len(outcome['pairs']) == 3
    assert outcome['pairs'][0]['matches'][0]['iou'] == {'numerator': '1', 'denominator': '2'}
    assert outcome['pairs'][0]['f1'] == {'numerator': '1', 'denominator': '1'}
    assert outcome['pairs'][1]['f1'] == {'numerator': '0', 'denominator': '1'}


def test_competing_matches_maximize_cardinality_before_iou():
    # A single perfect match must not defeat two threshold matches.
    left = [('Car', 0, 0, 20, 20), ('Car', 10, 0, 20, 20)]
    right = [('Car', 0, 0, 20, 20), ('Car', 0, 0, 10, 20)]
    assert bbox.iou(left[0], right[1]) == Fraction(1, 2)
    assert bbox.iou(left[1], right[0]) == Fraction(1, 3)
    # Shift second box so both alternatives cross the threshold.
    left[1] = ('Car', 5, 0, 15, 20)
    comparison = bbox.compare(left, right)
    assert len(comparison['matches']) == 2


def test_input_and_pair_order_do_not_change_score():
    a, b = [box(), box(x=60)], [box(x=60), box()]
    forward, backward = result(a, b), result(list(reversed(b)), list(reversed(a)))
    assert forward['pairs'][0]['f1'] == backward['pairs'][0]['f1'] == {'numerator': '1', 'denominator': '1'}
    assert forward['boxes'][0] == backward['boxes'][0]


@pytest.mark.parametrize('mutate,reason', [
    (lambda s: s['task']['data'].update(image=None), 'missing_image_reference'),
    (lambda s: s['annotation'].update(result=[]), 'empty_or_missing_result'),
    (lambda s: s['annotation']['result'][0]['value'].update(rotation=45), 'invalid_box'),
    (lambda s: s['annotation']['result'][0].update(original_width=200), 'invalid_box'),
    (lambda s: s['annotation']['result'].append(deepcopy(s['annotation']['result'][0])), 'duplicate_box'),
])
def test_unsupported_or_unverifiable_inputs_are_unavailable(mutate, reason):
    a = snapshot([box()])
    mutate(a)
    output = bbox.prepare([1, 2], [a, deepcopy(a)], image_bytes())
    assert output['availability'] == 'unavailable' and output['reason'] == reason
    assert output['pairs'] is None


def test_changed_image_task_data_and_config_are_unavailable():
    a, b = snapshot([box()]), snapshot([box()])
    b['task']['data']['other'] = 'changed'
    assert bbox.prepare([1, 2], [a, b], image_bytes())['reason'] == 'task_data_mismatch'
    b = deepcopy(a)
    b['project']['label_config'] += ' '
    assert bbox.prepare([1, 2], [a, b], image_bytes())['reason'] == 'config_mismatch'


@pytest.mark.parametrize(('section', 'other_id'), [('task', 2), ('project', 2), ('task', True), ('project', True)])
def test_identical_data_from_different_scope_is_not_comparable(section, other_id):
    a, b = snapshot([box()]), snapshot([box()])
    b[section]['id'] = other_id
    assert bbox.prepare([1, 2], [a, b], image_bytes())['reason'] == 'scope_mismatch'


def test_image_value_list_is_not_treated_as_one_static_image():
    a, b = snapshot([box()]), snapshot([box()])
    xml = XML.replace('value="$image"', 'value="$image" valueList="$images"')
    a['project']['label_config'] = b['project']['label_config'] = xml
    assert bbox.prepare([1, 2], [a, b], image_bytes())['reason'] == 'unsupported_config'


def test_one_input_is_not_fabricated_as_agreement():
    output = result([box()])
    assert output['availability'] == 'unavailable' and output['reason'] == 'insufficient_inputs'


def test_media_bytes_are_inspected_and_digested_without_resolving_url():
    a, b = snapshot([box()]), snapshot([box()])
    a['task']['data']['image'] = b['task']['data']['image'] = 's3://configured-bucket/image.png'
    output = bbox.prepare([1, 2], [a, b], image_bytes())
    assert output['availability'] == 'available'
    assert len(output['image']['sha256']) == 64
    invalid = bbox.prepare([1, 2], [a, b], b'not-an-image')
    assert invalid['availability'] == 'unavailable' and invalid['reason'] == 'unverified_image'


def test_jpeg_bytes_are_supported():
    stream = BytesIO()
    Image.new('RGB', (100, 80), 'white').save(stream, format='JPEG')
    a, b = snapshot([box()]), snapshot([box()])
    outcome = bbox.prepare([1, 2], [a, b], stream.getvalue())
    assert outcome['availability'] == 'available'


def test_box_outside_image_cannot_be_rounded_back_into_range():
    item = box(x=90.00000049, width=10.00000049)
    assert result([item], [item])['reason'] == 'invalid_box'


def test_matching_objective_against_exhaustive_oracle():
    rng = random.Random(81)
    for _ in range(100):
        left = sorted((rng.choice(('Car', 'Bus')), rng.randrange(0, 6), 0, rng.randrange(1, 7), 4)
                      for _ in range(rng.randrange(1, 4)))
        right = sorted((rng.choice(('Car', 'Bus')), rng.randrange(0, 6), 0, rng.randrange(1, 7), 4)
                       for _ in range(rng.randrange(1, 4)))
        valid = {(a, b): bbox.iou(x, y) for a, x in enumerate(left) for b, y in enumerate(right)
                 if x[0] == y[0] and bbox.iou(x, y) >= Fraction(1, 2)}
        objectives = [(0, 0)]
        for size in range(1, min(len(left), len(right)) + 1):
            for aa in itertools.combinations(range(len(left)), size):
                for bb in itertools.permutations(range(len(right)), size):
                    pairs = list(zip(aa, bb))
                    if all(pair in valid for pair in pairs):
                        objectives.append((size, sum(valid[p].numerator * bbox.SCALE // valid[p].denominator
                                                     for p in pairs)))
        found = bbox.compare(left, right)['matches']
        assert (len(found), sum(valid[(m['left'], m['right'])].numerator * bbox.SCALE //
                                valid[(m['left'], m['right'])].denominator for m in found)) == max(objectives)


def test_invalid_number_is_not_scored():
    item = box()
    item['value']['x'] = float('nan')
    output = result([item], [box()])
    assert output['availability'] == 'unavailable' and output['pairs'] is None


def test_matching_budget_denies_before_scorer(monkeypatch):
    monkeypatch.setattr(bbox, 'MAX_WORK', 0)
    monkeypatch.setattr(bbox, 'compare', lambda *_: pytest.fail('must not enter scorer'))
    with pytest.raises(bbox.Error, match='BBOX_TOO_LARGE'):
        result([box()], [box()])


def test_external_iou_engine_disagreement_fails_closed(monkeypatch):
    class Wrong:
        shape = (1, 1)

        def __getitem__(self, index):
            return 0.0

    monkeypatch.setattr(bbox.coco_mask, 'iou', lambda *_: Wrong())
    with pytest.raises(bbox.Error, match='BBOX_METRIC_ENGINE_FAILURE'):
        result([box()], [box()])

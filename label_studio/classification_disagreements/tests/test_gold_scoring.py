from classification_disagreements import gold_scoring


CONFIG = ('<View><Text name="text" value="$text"/>'
          '<Choices name="sentiment" toName="text"><Choice value="Positive"/>'
          '<Choice value="Negative"/></Choices></View>')


def answer(value):
    return [{'from_name': 'sentiment', 'to_name': 'text', 'type': 'choices', 'value': {'choices': [value]}}]


def test_exact_gold_match_and_explanation():
    result = gold_scoring.score(CONFIG, {'text': '合成 Synthetic'}, answer('Positive'), answer('Negative'))
    assert (result['matched_controls'], result['total_controls']) == (0, 1)
    assert result['controls'][0]['expected'] == ['Positive'] and result['controls'][0]['actual'] == ['Negative']
    assert gold_scoring.score(CONFIG, {'text': 'x'}, answer('Positive'), answer('Positive'))['matched_controls'] == 1


def test_invalid_or_missing_never_becomes_zero_or_perfect():
    for reference, candidate in (([], answer('Positive')), (answer('Positive'), []),
                                 (answer('Unknown'), answer('Positive'))):
        result = gold_scoring.score(CONFIG, {'text': 'x'}, reference, candidate)
        assert result['status'] == 'unavailable' and result['matched_controls'] is None and not result['controls']


def test_multiple_choices_are_order_independent():
    config = CONFIG.replace('toName="text"', 'toName="text" choice="multiple"')
    first, second = answer('Positive'), answer('Negative')
    first[0]['value']['choices'] = ['Positive', 'Negative']
    second[0]['value']['choices'] = ['Negative', 'Positive']
    assert gold_scoring.score(config, {'text': 'x'}, first, second)['matched_controls'] == 1


def test_controls_are_equal_weight_and_remote_text_is_unavailable():
    config = CONFIG.replace('</View>', '<Choices name="tone" toName="text">'
                            '<Choice value="Formal"/><Choice value="Casual"/></Choices></View>')
    tone = {'from_name': 'tone', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['Formal']}}
    result = gold_scoring.score(config, {'text': 'x'}, answer('Positive') + [tone], answer('Negative') + [tone])
    assert (result['matched_controls'], result['total_controls']) == (1, 2)
    assert [item['from_name'] for item in result['controls']] == ['sentiment', 'tone']
    result = gold_scoring.score(config.replace('<Text ', '<Text valueType="url" '), {'text': 'x'},
                                answer('Positive') + [tone], answer('Negative') + [tone])
    assert result['status'] == 'unavailable' and result['reason'] == 'unsupported_config'

"""Conservative, backend-consistent evidence for duplicate visible text."""

from django.db import connection
from rest_framework.exceptions import ValidationError

from . import service


def reference_text_lengths(reference):
    """Return distinct encoded lengths for static Text controls, without persisting text."""
    try:
        contents = reference.manifest['contents']
        schema = service.controls(contents['config'])
        data = contents['task_data']
        texts = [data[item['field']] for item in schema.values()]
        if not texts or any(not isinstance(text, str) for text in texts):
            raise ValueError()
    except (KeyError, TypeError, ValueError, service.Incomparable) as exc:
        raise ValidationError({'detail': 'Quality text evidence is unavailable.'}) from exc
    if connection.vendor == 'postgresql':
        expression = "octet_length(convert_to(%s, 'UTF8'))"
    elif connection.vendor == 'sqlite':
        expression = 'length(CAST(%s AS BLOB))'
    else:
        raise ValidationError({'detail': 'Quality text evidence is unavailable.'})
    with connection.cursor() as cursor:
        cursor.execute('SELECT ' + ', '.join([expression] * len(texts)), texts)
        values = cursor.fetchone()
    if not values or any(type(value) is not int or value < 0 for value in values):
        raise ValidationError({'detail': 'Quality text evidence is unavailable.'})
    return set(values)

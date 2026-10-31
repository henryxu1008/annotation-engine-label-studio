"""Bounded read adapters shared by frozen NER and field review strategies."""
from django.db.models import BigIntegerField, BooleanField, Case, F, JSONField, Q, Sum, Value, When, Window

from . import ner_priority, service


def strategy(plan):
    from . import field_priority

    if field_priority.is_plan(plan):
        return field_priority
    if ner_priority.is_plan(plan):
        return ner_priority
    return None


def is_plan(plan):
    return strategy(plan) is not None


def marker():
    from . import field_priority

    return ner_priority.marker() | field_priority.marker()


def query(project):
    return ner_priority.query(project, protected_marker=marker())


hydrate = ner_priority.hydrate


def visible_query(project, principal, actor, role):
    from . import field_priority

    rows = query(project)
    for adapter in (ner_priority, field_priority):
        if not adapter.available(project.pk, principal, actor):
            # Missing legacy JSON paths yield SQL NULL. Normalize the positive
            # marker before negating it, so only explicit protected plans leave
            # the collection when their strategy is unavailable.
            name = f'_disabled_{adapter.KIND}'
            rows = rows.alias(
                **{
                    name: Case(
                        When(adapter.marker(), then=Value(True)), default=Value(False), output_field=BooleanField()
                    )
                }
            ).filter(**{name: False})
    return ner_priority.filter_visible(rows, actor, role)


def load(query, *, plan, for_serialization=False):
    return strategy(plan).load(query, for_serialization=for_serialization)


def page(view, rows):
    """Paginate IDs first; gate the entire field-plan page before decoding JSON."""
    from . import field_priority as field

    identifiers = view.paginate_queryset(rows.order_by('-pk').values_list('pk', flat=True))
    selected = (
        rows.filter(pk__in=identifiers)
        .annotate(
            _field_size=Case(
                When(field.marker(), then=F('_ner_plan_size')), default=Value(0), output_field=BigIntegerField()
            ),
        )
        .annotate(_field_total=Window(Sum('_field_size')))
    )
    selected = selected.annotate(
        _ner_plan=Case(
            When(field.marker() & Q(_field_total__gt=field.MAX_PAGE_STORED), then=Value(None)),
            default=F('_ner_plan'),
            output_field=JSONField(),
        )
    )
    result, used = [], 0
    iterator = selected.order_by('-pk').iterator(chunk_size=1)
    try:
        for batch in iterator:
            if batch._field_total > field.MAX_PAGE_STORED:
                service.fail('REVIEW_FIELD_PRIORITY_TOO_LARGE', 422)
            batch = hydrate(batch)
            if field.is_plan(batch.plan):
                used += field.canonical_size(batch.plan)
                if used > field.MAX_PAGE_BYTES:
                    service.fail('REVIEW_FIELD_PRIORITY_TOO_LARGE', 422)
            result.append(batch)
    finally:
        iterator.close()
    if {b.pk for b in result} != set(identifiers):
        service.fail('REVIEW_PRIORITY_READ_CHANGED')
    return result

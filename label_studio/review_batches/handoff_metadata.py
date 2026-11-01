"""Shared bounded JSON materialization for typed handoff ledgers."""

from django.db import connection, models
from django.db.models import BigIntegerField, Case, F, Q, Sum, Value, When, Window
from django.db.models.expressions import RawSQL
from django.shortcuts import get_object_or_404


class BoundedMetadata:
    """One materialization; caches never cross the final current-source replay."""

    def __init__(self, canonical_limit, stored_limit, bounded, error):
        self.canonical_limit, self.stored_limit = canonical_limit, stored_limit
        self.bounded, self.error = bounded, error
        self.canonical = self.stored = 0
        self.cache = {}
        self.locked = set()

    def add(self, key, value, stored):
        if key in self.cache:
            return
        size = self.bounded(value, self.canonical_limit)
        if (
            type(stored) is not int
            or stored < 0
            or self.canonical + size > self.canonical_limit
            or self.stored + stored > self.stored_limit
        ):
            self.error('BUDGET_EXCEEDED', 422)
        self.canonical += size
        self.stored += stored
        self.cache[key] = value

    def rows(self, query, fields, limits, *, lock=False):
        """Gate a complete metadata union inside the JSON-hydrating SELECT.

        Unlike a Python loop of individually fitting rows this also catches
        concurrent growth spread over multiple rows before any JSON decoding.
        Existing single-ledger callers retain their original load/row paths.
        """
        table = connection.ops.quote_name(query.model._meta.db_table)
        measure = 'OCTET_LENGTH(CAST({} AS TEXT))' if connection.vendor == 'postgresql' else 'LENGTH(CAST({} AS BLOB))'
        query = query.select_related(None)
        if lock:
            # PostgreSQL forbids FOR UPDATE on a window-function SELECT.
            # Hold these ordered scalar row locks, then hydrate in a fresh gate.
            list(query.order_by('pk').select_for_update().values_list('pk', flat=True))
        expressions = []
        for field in fields:
            expression = 'COALESCE(' + measure.format(table + '.' + connection.ops.quote_name(field)) + ',0)'
            expressions.append(expression)
            query = query.annotate(**{f'_meta_size_{field}': RawSQL(expression, [], output_field=BigIntegerField())})
        total = RawSQL('+'.join(expressions), [], output_field=BigIntegerField())
        query = query.annotate(_meta_total=Window(Sum(total)))
        remaining = self.stored_limit - self.stored
        inspected = list(query.values_list(*(f'_meta_size_{f}' for f in fields)))
        if (
            any(type(n) is not int or not 0 <= n <= limits[f][1] for sizes in inspected for f, n in zip(fields, sizes))
            or sum(sum(sizes) for sizes in inspected) > remaining
        ):
            self.error('BUDGET_EXCEEDED', 422)
        gate = Q(_meta_total__lte=remaining)
        for field in fields:
            gate &= Q(**{f'_meta_size_{field}__lte': limits[field][1]})
        for field in fields:
            query = query.annotate(
                **{
                    f'_meta_value_{field}': Case(
                        When(gate, then=F(field)), default=Value(None), output_field=models.JSONField()
                    )
                }
            )
        query = query.defer(*fields)
        result = []
        for row in query.order_by('pk').iterator(chunk_size=1):
            if type(row._meta_total) is not int or not 0 <= row._meta_total <= remaining:
                self.error('BUDGET_EXCEEDED', 422)
            for field in fields:
                size = getattr(row, f'_meta_size_{field}')
                if type(size) is not int or not 0 <= size <= limits[field][1]:
                    self.error('BUDGET_EXCEEDED', 422)
                value = getattr(row, f'_meta_value_{field}')
                self.bounded(value, limits[field][0])
                self.add((row._meta.db_table, row.pk, field), value, size)
                setattr(row, field, value)
            result.append(row)
        return result

    def load(self, query, identifier, field, canonical, stored, *, lock=False, row_total=False):
        key = (query.model._meta.db_table, identifier, field)
        if key in self.cache:
            if lock and key not in self.locked:
                # A cached read is not a row lock. Select IDs, never annotations/JSON.
                get_object_or_404(query.select_for_update().values('pk'), pk=identifier)
                self.locked.add(key)
            return self.cache[key]
        table = connection.ops.quote_name(query.model._meta.db_table)
        column = table + '.' + connection.ops.quote_name(field)
        sql = (
            f'OCTET_LENGTH(CAST({column} AS TEXT))'
            if connection.vendor == 'postgresql'
            else f'LENGTH(CAST({column} AS BLOB))'
        )
        ceiling = min(stored, self.stored_limit - self.stored)
        gates = {'_handoff_size__lte': ceiling}
        if row_total:
            gates['_target_bytes__lte'] = self.stored_limit - self.stored
        query = query.annotate(_handoff_size=RawSQL(sql, [], output_field=models.BigIntegerField()))
        query = query.annotate(
            _handoff_value=Case(When(**gates, then=F(field)), default=Value(None), output_field=models.JSONField())
        ).defer(field)
        if lock:
            query = query.select_for_update()
        row = get_object_or_404(query, pk=identifier)
        if type(row._handoff_size) is not int or not 0 <= row._handoff_size <= ceiling:
            self.error('BUDGET_EXCEEDED', 422)
        if row_total and row._target_bytes > self.stored_limit - self.stored:
            self.error('BUDGET_EXCEEDED', 422)
        value = row._handoff_value
        self.bounded(value, canonical)
        self.add(key, value, row._handoff_size)
        setattr(row, field, value)
        self.cache[key] = row
        if lock:
            self.locked.add(key)
        return row

    def row(self, query, identifier, fields, limits, guarded, hydrate, *, lock=False):
        """Reserve the whole row, including dependent winner/reason metadata."""
        query = guarded(query, fields[1:])
        table = connection.ops.quote_name(query.model._meta.db_table)
        measure = 'OCTET_LENGTH(CAST({} AS TEXT))' if connection.vendor == 'postgresql' else 'LENGTH(CAST({} AS BLOB))'
        total = '+'.join(
            'COALESCE(' + measure.format(table + '.' + connection.ops.quote_name(field)) + ',0)' for field in fields
        )
        query = query.annotate(_target_bytes=RawSQL(total, [], output_field=models.BigIntegerField()))
        remaining = self.stored_limit - self.stored
        for field in fields[1:]:
            output = query.model._meta.get_field(field)
            stored = limits.get(field, (16 * 1024, 16 * 1024))[1]
            query = query.annotate(
                **{
                    f'_safe_{field}': Case(
                        When(**{'_target_bytes__lte': remaining, f'_size_{field}__lte': stored}, then=F(field)),
                        default=Value(None),
                        output_field=output,
                    )
                }
            )
        row = self.load(query, identifier, fields[0], *limits[fields[0]], lock=lock, row_total=True)
        hydrate(row, fields)
        for field in fields[1:]:
            self.add((row._meta.db_table, identifier, field), getattr(row, field), getattr(row, f'_size_{field}') or 0)
        return row

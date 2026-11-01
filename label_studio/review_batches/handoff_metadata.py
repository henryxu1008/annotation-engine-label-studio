"""Shared bounded JSON materialization for typed handoff ledgers."""

from django.db import connection, models
from django.db.models import Case, F, Value, When
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

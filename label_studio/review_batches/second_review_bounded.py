"""Typed SQL byte gates for the supervision metadata union, including TextField."""
from django.db import connection, models
from django.db.models import BigIntegerField, Case, F, Q, Sum, Value, When, Window
from django.db.models.expressions import RawSQL

from . import second_review_contract as c


class Budget:
    def __init__(self, canonical=8 * c.MIB, stored=32 * c.MIB):
        self.canonical_limit, self.stored_limit = canonical, stored
        self.canonical = self.stored = 0

    def rows(self, query, limits, *, union=None):
        """Preflight AND gate each hydrating SELECT. No deferred field reads."""
        limits = dict(limits)
        payload_fields = tuple(limits)
        deferred, is_defer = query.query.deferred_loading
        integer_fields = []
        for field in query.model._meta.fields:
            selected = field.primary_key or (
                (field.name not in deferred and field.attname not in deferred)
                if is_defer
                else field.name in deferred or field.attname in deferred
            )
            scalar = field.target_field if isinstance(field, models.ForeignKey) else field
            name = field.attname
            if (
                isinstance(scalar, (models.CharField, models.DateTimeField, models.UUIDField, models.IntegerField))
                and selected
                and name not in limits
            ):
                # SQLite does not enforce varchar(n). Bound every actually
                # selected string too; an assignee .only() stays name-only.
                maximum = (
                    4 * scalar.max_length
                    if isinstance(scalar, models.CharField)
                    else 32
                    if isinstance(scalar, models.IntegerField)
                    else 128
                )
                limits[name] = (maximum + 2, maximum)
            if selected and isinstance(scalar, models.IntegerField):
                integer_fields.append(name)
        table = connection.ops.quote_name(query.model._meta.db_table)
        measure = 'OCTET_LENGTH(CAST({} AS TEXT))' if connection.vendor == 'postgresql' else 'LENGTH(CAST({} AS BLOB))'
        query = query.select_related(None)
        expressions = []
        for field in limits:
            col = table + '.' + connection.ops.quote_name(query.model._meta.get_field(field).column)
            sql = 'COALESCE(' + measure.format(col) + ',0)'
            expressions.append(sql)
            query = query.annotate(**{f'_sr_size_{field}': RawSQL(sql, [], output_field=BigIntegerField())})
        if connection.vendor == 'sqlite' and integer_fields:
            for name in integer_fields:
                column = table + '.' + connection.ops.quote_name(query.model._meta.get_field(name).column)
                query = query.annotate(
                    **{
                        f'_sr_integer_{name}': RawSQL(
                            f"CASE WHEN {column} IS NULL OR typeof({column})='integer' THEN 1 ELSE 0 END",
                            [],
                            output_field=BigIntegerField(),
                        )
                    }
                )
        query = query.annotate(_sr_row=RawSQL('+'.join(expressions), [], output_field=BigIntegerField()))
        payload = '+'.join(expressions[: len(payload_fields)])
        query = query.annotate(_sr_payload=RawSQL(payload, [], output_field=BigIntegerField()))
        query = query.annotate(_sr_union=Window(Sum(F('_sr_row'))))
        remaining = self.stored_limit - self.stored
        row_stored = union[1] if union else remaining
        sizes = list(query.values_list(*(f'_sr_size_{f}' for f in limits)))
        if (
            sum(sum(s) for s in sizes) > remaining
            or any(sum(s[: len(payload_fields)]) > row_stored for s in sizes)
            or any(type(n) is not int or not 0 <= n <= limits[f][1] for s in sizes for f, n in zip(limits, s))
        ):
            c.fail('TOO_LARGE', 422)
        gate = Q(_sr_union__lte=remaining, _sr_payload__lte=row_stored)
        if connection.vendor == 'sqlite' and integer_fields:
            for name in integer_fields:
                gate &= Q(**{f'_sr_integer_{name}': 1})
            if any(
                n != 1
                for flags in query.values_list(*(f'_sr_integer_{name}' for name in integer_fields))
                for n in flags
            ):
                c.fail()
        for f in limits:
            gate &= Q(**{f'_sr_size_{f}__lte': limits[f][1]})
        for f in limits:
            field = query.model._meta.get_field(f)
            if field.primary_key:
                continue
            output = field.target_field if isinstance(field, models.ForeignKey) else field
            query = query.annotate(
                **{f'_sr_value_{f}': Case(When(gate, then=F(f)), default=Value(None), output_field=output)}
            )
        # Deferring every field in an existing only() silently clears Django's
        # restriction. Retain a literal PK-only base projection and materialize
        # every other selected scalar/payload exclusively through its CASE.
        # These model PKs are native integers or new insert-guarded frozen UUIDs.
        query = query.only('pk')
        result = []
        for row in query.iterator(chunk_size=1):
            if connection.vendor == 'sqlite' and any(getattr(row, f'_sr_integer_{f}') != 1 for f in integer_fields):
                c.fail()
            if row._sr_union > remaining or row._sr_payload > row_stored:
                c.fail('TOO_LARGE', 422)
            canonical_row = canonical_payload = 0
            for f, (canonical, stored) in limits.items():
                n = getattr(row, f'_sr_size_{f}')
                if not 0 <= n <= stored:
                    c.fail('TOO_LARGE', 422)
                field = query.model._meta.get_field(f)
                value = row.pk if field.primary_key else getattr(row, f'_sr_value_{f}')
                scalar = field.target_field if isinstance(field, models.ForeignKey) else field
                serializable = (
                    str(value)
                    if value is not None and isinstance(scalar, (models.DateTimeField, models.UUIDField))
                    else value
                )
                measured = c.bounded(serializable, canonical)
                canonical_row += measured
                if f in payload_fields:
                    canonical_payload += measured
                setattr(row, f, value)
            if union and canonical_payload > union[0]:
                c.fail('TOO_LARGE', 422)
            self.canonical += canonical_row
            self.stored += row._sr_row
            if self.canonical > self.canonical_limit or self.stored > self.stored_limit:
                c.fail('TOO_LARGE', 422)
            result.append(row)
        return result

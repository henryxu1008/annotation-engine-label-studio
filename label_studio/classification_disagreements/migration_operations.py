"""Small compatibility wrapper around Django's online PostgreSQL index operation."""

from django.db import migrations


class AddIndexOnline(migrations.AddIndex):
    def _already_applied(self, model, schema_editor):
        """Resume a non-atomic migration without trusting a different index."""
        db = schema_editor.connection
        if db.vendor == 'postgresql':
            with db.cursor() as cursor:
                cursor.execute(
                    """SELECT i.indisvalid, i.indisready, i.indisunique,
                    i.indpred IS NULL, i.indexprs IS NULL, a.amname,
                    ARRAY(SELECT pg_get_indexdef(i.indexrelid, s, true)
                          FROM generate_series(1, i.indnkeyatts) s),
                    NOT EXISTS (SELECT 1 FROM unnest(i.indoption) AS o WHERE o <> 0),
                    i.indnatts = i.indnkeyatts
                    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                    JOIN pg_am a ON a.oid = c.relam
                    WHERE i.indrelid = to_regclass(%s) AND c.relname = %s""",
                    [model._meta.db_table, self.index.name],
                )
                found = cursor.fetchone()
            if found is None:
                return False
            valid, ready, unique, unfiltered, simple, method, columns, ascending, no_include = found
            if (
                unique
                or not unfiltered
                or not simple
                or method != 'btree'
                or columns != list(self.index.fields)
                or not ascending
                or not no_include
            ):
                raise RuntimeError('Existing index does not match the requested blind read index.')
            if valid and ready:
                return True
            # PostgreSQL can leave an invalid index after an interrupted
            # concurrent build. Drop only a verified matching index.
            schema_editor.remove_index(model, self.index, concurrently=True)
            return False
        if db.vendor == 'sqlite':
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = %s AND name = %s",
                    [model._meta.db_table, self.index.name],
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                existing = db.introspection.get_constraints(cursor, model._meta.db_table).get(self.index.name)
                cursor.execute(f'PRAGMA index_list({db.ops.quote_name(model._meta.db_table)})')
                listed = next((item for item in cursor.fetchall() if item[1] == self.index.name), None)
                cursor.execute(f'PRAGMA index_xinfo({db.ops.quote_name(self.index.name)})')
                key_columns = [(item[2], item[3], item[4]) for item in cursor.fetchall() if item[5]]
            if (
                not existing
                or not existing['index']
                or existing['unique']
                or existing['columns'] != list(self.index.fields)
                or not listed
                or len(listed) < 5
                or listed[4]
                or key_columns != [(name, 0, 'BINARY') for name in self.index.fields]
            ):
                raise RuntimeError('Existing index does not match the requested blind read index.')
            return True
        return False

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        if self._already_applied(model, schema_editor):
            return
        if schema_editor.connection.vendor == 'postgresql':
            from django.contrib.postgres.operations import AddIndexConcurrently

            return AddIndexConcurrently(self.model_name, self.index).database_forwards(
                app_label,
                schema_editor,
                from_state,
                to_state,
            )
        return super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'postgresql':
            from django.contrib.postgres.operations import AddIndexConcurrently

            return AddIndexConcurrently(self.model_name, self.index).database_backwards(
                app_label,
                schema_editor,
                from_state,
                to_state,
            )
        return super().database_backwards(app_label, schema_editor, from_state, to_state)

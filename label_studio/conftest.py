"""
Root conftest for the label_studio package.

Ensures CurrentContext is cleared after every test to prevent leakage across tests.
With Postgres, test transactions are rolled back so User rows are removed, but
thread-local CurrentContext can still hold a stale User instance. The next test
then triggers FSM (e.g. on project save), which uses that user as triggered_by_id,
causing IntegrityError because the user row no longer exists.
"""

import pytest


@pytest.fixture(scope='session', autouse=True)
def flush_sqlite_capture_identity_for_tests():
    """Allow Django to reset only its isolated in-memory test database."""
    from django.core.management.commands.flush import Command
    from django.db import connections

    original = Command.handle

    def handle(command, *args, **options):
        db = connections[options.get('database', 'default')]
        name = str(db.settings_dict['NAME'])
        if db.vendor != 'sqlite' or not (
            name == ':memory:' or (name.startswith('file:memorydb_') and 'mode=memory' in name)
        ):
            return original(command, *args, **options)
        with db.cursor() as cursor:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = %s",
                           ['blind_capture_identity_delete'])
            row = cursor.fetchone()
            if row:
                cursor.execute('DROP TRIGGER blind_capture_identity_delete')
        try:
            return original(command, *args, **options)
        finally:
            if row:
                with db.cursor() as cursor:
                    cursor.execute(row[0])

    Command.handle = handle
    try:
        yield
    finally:
        Command.handle = original


@pytest.fixture(autouse=True)
def clear_current_context_after_test():
    """Clear thread-local CurrentContext after each test to prevent FK violations on Postgres."""
    yield
    from core.current_request import CurrentContext

    CurrentContext.clear()

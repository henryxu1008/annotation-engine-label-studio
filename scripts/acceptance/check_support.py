"""Standard-library-only CI/local runner; never starts the isolated acceptance lifecycle."""
import asyncio  # noqa: F401 -- preload stdlib before installing the socket guard
import importlib.abc
import json
import socket
import ssl  # noqa: F401 -- SSLSocket must be defined against the original socket class
import sys
import unittest
import unittest.mock  # noqa: F401 -- imports asyncio in some supported Python versions
from pathlib import Path


class ProductImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'django', 'label_studio', 'access_control', 'core', 'data_export', 'review_batches', 'projects',
            'tasks', 'users', 'organizations', 'classification_disagreements', 'psycopg', 'redis', 'rq',
        }:
            raise AssertionError('Product/service imports are forbidden in offline support verification')


def deny_network(*args, **kwargs):
    raise AssertionError('Network access is forbidden in offline support verification')


def install_guards():
    class DeniedSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            deny_network()

    sys.meta_path.insert(0, ProductImportGuard())
    socket.socket = DeniedSocket
    socket.create_connection = deny_network
    socket.getaddrinfo = deny_network
    socket.socketpair = deny_network


def main():
    install_guards()
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert 'django' not in sys.modules and 'psycopg' not in sys.modules and 'redis' not in sys.modules
    print(json.dumps({'mode': 'OFFLINE_SUPPORT_ONLY', 'tests': result.testsRun,
                      'passed': result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
                      'skipped': len(result.skipped), 'failed': len(result.failures) + len(result.errors),
                      'product_imports': 'BLOCKED', 'network': 'BLOCKED',
                      'services': 'NOT_STARTED', 'execution_matrix_72': 'ALL_NOT_RUN'}), flush=True)
    if not result.wasSuccessful():
        return 1
    from ls84 import main as plan

    return plan(['plan'])


if __name__ == '__main__':
    raise SystemExit(main())

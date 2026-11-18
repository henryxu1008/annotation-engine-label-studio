"""Synthetic legacy S3 client used only inside the disposable network fixture."""
import ipaddress
import os
import socket
import threading
import time
from pathlib import Path

if os.environ.get('ISSUE86_DISPOSABLE_NETWORK_TEST') != '1':
    raise RuntimeError('The disposable network fixture must be explicitly enabled.')

ready = Path('/tmp/issue86-ready')
resume = Path('/tmp/issue86-resume')
result = Path('/tmp/issue86-result')
try:
    import django

    django.setup()
    import io_storages.s3.models as legacy_s3
    from botocore.exceptions import (
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
    )
    from io_storages.s3.models import S3ExportStorage
    from urllib3.util import Timeout

    if not legacy_s3.__file__.startswith('/legacy/'):
        raise RuntimeError('Legacy source was not loaded.')
    host = os.environ['ISSUE86_OBJECT_HOST']
    address = ipaddress.ip_address(host)
    if address.version != 4 or not address.is_private:
        raise ValueError('The fixture requires a private IPv4 address.')
    storage = S3ExportStorage(
        bucket='issue86-network-fixture', s3_endpoint=f'http://{host}:9000', region_name='us-east-1',
        aws_access_key_id='issue86oldworker', aws_secret_access_key='issue86oldworkerpassword',
    )
    client = storage.get_client()
    # Bound the fixture's failure observation without changing product code.
    client._endpoint.http_session._timeout = Timeout(connect=2, read=2)
    client.head_bucket(Bucket=storage.bucket)

    # The second legacy S3 client submits one synthetic PUT to the intermediary.
    # Its outcome is deliberately not inferred if the network is disconnected.
    put_storage = S3ExportStorage(
        bucket=storage.bucket, s3_endpoint=f'http://{host}:9100', region_name='us-east-1',
        aws_access_key_id='issue86oldworker', aws_secret_access_key='issue86oldworkerpassword',
    )
    put_client = put_storage.get_client()

    def submitted_put():
        try:
            put_client.put_object(Bucket=storage.bucket, Key='issue86/inflight', Body=b'synthetic-network-inflight')
        except Exception:
            pass  # The provider outcome is inspected independently by the observer.

    threading.Thread(target=submitted_put, daemon=True).start()
    ready.write_text('READY', encoding='utf-8')
    deadline = time.monotonic() + 120
    while not resume.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not resume.exists():
        raise TimeoutError('The fixture was not resumed.')
    try:
        with socket.create_connection((host, 9000), timeout=3):
            result.write_text('UNEXPECTED_NEW_CONNECTION', encoding='utf-8')
            raise SystemExit(1)
    except OSError:
        pass

    # Keep the old SDK client and its connection pool: disconnecting a network
    # may leave an existing request or socket without an observable terminal
    # result. A bounded timeout is explicitly UNKNOWN, never a drain proof.
    outcome = []

    def existing_client_request():
        try:
            client.head_bucket(Bucket=storage.bucket)
        except (ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError) as exc:
            outcome.append('BLOCKED_' + type(exc).__name__)
        except Exception as exc:
            outcome.append('ERROR_' + type(exc).__name__)
        else:
            outcome.append('SUCCEEDED')

    worker = threading.Thread(target=existing_client_request, daemon=True)
    worker.start()
    worker.join(timeout=8)
    client_result = outcome[0] if outcome else 'UNKNOWN'
    result.write_text('NEW_CONNECTION_BLOCKED;CLIENT_' + client_result, encoding='utf-8')
except BaseException as exc:
    result.write_text('ERROR:' + type(exc).__name__, encoding='utf-8')
    raise

"""Exercise the actual SDK's retry behavior for admitted annotation PUTs."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from botocore.exceptions import ClientError, ConnectionClosedError
from django.test import SimpleTestCase
from io_storages.s3.models import S3ExportStorage, clients_cache
from io_storages.s3.utils import get_client_and_resource


class S3SingleAttemptTests(SimpleTestCase):
    def test_single_attempt_overrides_environment_for_503_and_lost_response(self):
        for failure in ('503', 'lost_response'):
            with self.subTest(failure=failure):
                requests = []

                class Handler(BaseHTTPRequestHandler):
                    protocol_version = 'HTTP/1.1'

                    def do_PUT(self):
                        requests.append(self.rfile.read(int(self.headers['Content-Length'])))
                        if failure == '503':
                            self.send_response(503)
                            self.send_header('Content-Length', '0')
                            self.send_header('Connection', 'close')
                            self.end_headers()
                        else:
                            self.close_connection = True

                    def log_message(self, *args):
                        pass

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with patch.dict(
                        os.environ,
                        {'AWS_MAX_ATTEMPTS': '7', 'AWS_RETRY_MODE': 'standard', 'NO_PROXY': '127.0.0.1,localhost'},
                    ):
                        _, resource = get_client_and_resource(
                            'synthetic-access',
                            'synthetic-secret',
                            region_name='us-east-1',
                            s3_endpoint=f'http://127.0.0.1:{server.server_port}',
                            single_attempt=True,
                        )
                        self.assertEqual(resource.meta.client.meta.config.retries['total_max_attempts'], 1)
                        with self.assertRaises((ClientError, ConnectionClosedError)):
                            resource.Object('synthetic-bucket', 'key').put(Body=b'synthetic')
                    self.assertEqual(requests, [b'synthetic'])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=10)

    def test_normal_and_single_attempt_clients_never_share_cache_entry(self):
        storage = S3ExportStorage(
            bucket='bucket', aws_access_key_id='synthetic-key', aws_secret_access_key='synthetic-secret'
        )
        with patch.dict(clients_cache, {}, clear=True), patch(
            'io_storages.s3.models.get_client_and_resource', side_effect=[('normal', 1), ('single', 2)]
        ) as create:
            self.assertEqual(storage.get_client_and_resource(), ('normal', 1))
            self.assertEqual(storage.get_client_and_resource(single_attempt=True), ('single', 2))
            self.assertEqual(storage.get_client_and_resource(), ('normal', 1))
            self.assertEqual(storage.get_client_and_resource(single_attempt=True), ('single', 2))
            self.assertEqual(create.call_count, 2)

    def test_s3_session_creation_does_not_log_credentials_or_tokens(self):
        with patch('io_storages.s3.utils.logger') as logger:
            get_client_and_resource(
                'synthetic-access-key',
                'synthetic-secret-key',
                'synthetic-session-token',
                region_name='us-east-1',
                single_attempt=True,
            )
        logged = str(logger.mock_calls)
        for secret in ('synthetic-access-key', 'synthetic-secret-key', 'synthetic-session-token'):
            self.assertNotIn(secret, logged)

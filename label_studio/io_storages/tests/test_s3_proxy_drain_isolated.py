"""A disconnected old process is not proof that a buffered request was drained.

Runs only against the dedicated loopback MinIO fixture. The proxy models a
separate intermediary which has accepted a complete signed request. It is not
a model of AWS IAM, VPC enforcement, or every proxy's buffering behavior.
"""
import os
import socket
import subprocess
import sys
import threading
import unittest
import uuid

import boto3
from django.test import SimpleTestCase


@unittest.skipUnless(os.environ.get('ISSUE86_DISPOSABLE_S3_TEST') == '1', 'requires disposable S3 fixture')
class BufferedRequestDrainTests(SimpleTestCase):
    def test_killed_old_process_does_not_cancel_request_buffered_by_proxy(self):
        client = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket, key = 'issue86-' + uuid.uuid4().hex, 'synthetic/buffered-put'
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(20)
        captured, release = threading.Event(), threading.Event()
        errors = []

        def intermediary():
            try:
                with listener.accept()[0] as old_socket:
                    old_socket.settimeout(20)
                    request = bytearray()
                    while b'\r\n\r\n' not in request:
                        chunk = old_socket.recv(8192)
                        if not chunk or len(request) > 65536:
                            raise ValueError('Incomplete request headers')
                        request.extend(chunk)
                    head, body = bytes(request).split(b'\r\n\r\n', 1)
                    headers = {}
                    for line in head.split(b'\r\n')[1:]:
                        name, value = line.split(b':', 1)
                        headers[name.lower()] = value.strip()
                    length = int(headers[b'content-length'])
                    if length > 1024:
                        raise ValueError('Unexpected fixture payload length')
                    if headers.get(b'expect', b'').lower() == b'100-continue':
                        old_socket.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')
                    while len(body) < length:
                        chunk = old_socket.recv(length - len(body))
                        if not chunk:
                            raise ValueError('Incomplete request body')
                        body += chunk
                    if len(body) != length:
                        raise ValueError('Unexpected request body length')
                    captured.set()
                    if not release.wait(20):
                        raise TimeoutError('Proxy release was not signalled')
                    # Forward the original signed request after the old client
                    # has died; a proxy can own this request independently.
                    with socket.create_connection(('127.0.0.1', 9000), timeout=10) as backend:
                        backend.settimeout(10)
                        backend.sendall(head + b'\r\n\r\n' + body)
                        response = bytearray()
                        while b'HTTP/1.1 200 OK' not in response:
                            part = backend.recv(8192)
                            if not part or len(response) > 65536:
                                raise ValueError('Provider did not confirm the buffered PUT')
                            response.extend(part)
            except BaseException as exc:
                errors.append(exc)
                captured.set()
            finally:
                listener.close()

        worker = """
import boto3, os
from botocore.config import Config
client = boto3.client('s3', endpoint_url='http://127.0.0.1:' + os.environ['ISSUE86_PROXY_PORT'],
    region_name='us-east-1', aws_access_key_id='issue86fixture', aws_secret_access_key='issue86fixturepassword',
    config=Config(retries={'max_attempts': 0}, connect_timeout=5, read_timeout=30, proxies={}))
client.put_object(Bucket=os.environ['ISSUE86_BUCKET'], Key='synthetic/buffered-put', Body=b'synthetic')
"""
        proxy = threading.Thread(target=intermediary, daemon=True)
        proxy.start()
        environment = os.environ.copy()
        environment.update(ISSUE86_PROXY_PORT=str(listener.getsockname()[1]), ISSUE86_BUCKET=bucket)
        process = subprocess.Popen(
            [sys.executable, '-c', worker], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        try:
            self.assertTrue(captured.wait(20), 'Old request did not reach the intermediary')
            if errors:
                raise errors[0]
            process.kill()
            process.wait(timeout=10)
            release.set()
            proxy.join(timeout=20)
            self.assertFalse(proxy.is_alive(), 'Intermediary did not terminate')
            if errors:
                raise errors[0]
            result = client.get_object(Bucket=bucket, Key=key)
            self.assertEqual(result['Body'].read(), b'synthetic')
            self.assertTrue(result.get('VersionId'))
        finally:
            release.set()
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            listener.close()
            proxy.join(timeout=20)

"""One-shot synthetic PUT intermediary for the disposable network experiment."""
import os
import socket
import time
from pathlib import Path

if os.environ.get('ISSUE86_DISPOSABLE_NETWORK_TEST') != '1':
    raise RuntimeError('The disposable network fixture must be explicitly enabled.')

captured = Path('/tmp/issue86-proxy-captured')
listening = Path('/tmp/issue86-proxy-listening')
release = Path('/tmp/issue86-proxy-release')
result = Path('/tmp/issue86-proxy-result')
try:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('0.0.0.0', 9100))
        listener.listen(1)
        listener.settimeout(120)
        listening.write_text('LISTENING', encoding='utf-8')
        with listener.accept()[0] as incoming:
            incoming.settimeout(20)
            request = bytearray()
            while b'\r\n\r\n' not in request:
                data = incoming.recv(8192)
                if not data or len(request) > 65536:
                    raise ValueError('Incomplete fixture request headers.')
                request.extend(data)
            head, body = bytes(request).split(b'\r\n\r\n', 1)
            if not head.startswith(b'PUT /issue86-network-fixture/issue86/inflight '):
                raise ValueError('Unexpected fixture request target.')
            headers = {}
            for line in head.split(b'\r\n')[1:]:
                name, value = line.split(b':', 1)
                headers[name.lower()] = value.strip()
            length = int(headers[b'content-length'])
            if not 0 < length <= 1024:
                raise ValueError('Unexpected fixture payload length.')
            if headers.get(b'expect', b'').lower() == b'100-continue':
                incoming.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')
            while len(body) < length:
                data = incoming.recv(length - len(body))
                if not data:
                    raise ValueError('Incomplete fixture request body.')
                body += data
            if body != b'synthetic-network-inflight':
                raise ValueError('Unexpected fixture request body.')
            captured.write_text('CAPTURED', encoding='utf-8')
            deadline = time.monotonic() + 120
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not release.exists():
                raise TimeoutError('The fixture proxy was not released.')
            with socket.create_connection(('127.0.0.1', 9000), timeout=10) as backend:
                backend.settimeout(10)
                backend.sendall(head + b'\r\n\r\n' + body)
                response = bytearray()
                while b'HTTP/1.1 200 OK' not in response:
                    data = backend.recv(8192)
                    if not data or len(response) > 65536:
                        raise ValueError('The fixture provider did not confirm the PUT.')
                    response.extend(data)
            # The response belongs to the proxy's observation; the old client
            # has been disconnected and does not receive this confirmation.
            result.write_text('PROVIDER_CONFIRMED', encoding='utf-8')
except BaseException as exc:
    result.write_text('ERROR:' + type(exc).__name__, encoding='utf-8')
    raise

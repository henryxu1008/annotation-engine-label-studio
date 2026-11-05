"""Test-entrypoint WSGI wrapper. Not installed in product settings or URL routing."""
import hashlib
import hmac
import json
from urllib.parse import parse_qs

import ls84_contract as c

CONTEXT = None
SECRET = None
PRODUCT = None
ATTESTATION_PATH = '/__ls84_support__/attestation/'


def proof(secret, payload):
    return hmac.new(secret.encode(), ('ls84-web-attestation/v1:' + c.hashed(payload)).encode(), hashlib.sha256).hexdigest()


def configure(receipt, secret):
    global CONTEXT, SECRET
    CONTEXT, SECRET = dict(receipt), secret


def application(environ, start_response):
    global PRODUCT
    c.require(CONTEXT is not None and SECRET is not None, 'EXPLICIT_WEB_BRIDGE_REQUIRED')
    try:
        c.require_live_authorization(CONTEXT)
    except c.SupportError:
        start_response('503 Service Unavailable', [('Content-Type', 'application/json'), ('Cache-Control', 'no-store')])
        return [b'{"status":"AUTHORIZATION_EXPIRED"}']
    if environ.get('PATH_INFO') == ATTESTATION_PATH:
        try:
            query = parse_qs(environ.get('QUERY_STRING', ''), strict_parsing=True)
        except ValueError:
            query = {}
        if (environ.get('REQUEST_METHOD') != 'GET' or set(query) != {'challenge'}
                or len(query['challenge']) != 1 or not c.uuid(query['challenge'][0])):
            start_response('400 Bad Request', [('Content-Type', 'application/json'), ('Cache-Control', 'no-store')])
            return [b'{"status":"invalid challenge"}']
        payload = {'challenge': query['challenge'][0], 'run_id': CONTEXT['run_id'], 'pid': CONTEXT['pid'],
                   'manifest_sha256': CONTEXT['manifest_sha256'], 'fixture_sha256': CONTEXT['fixture_sha256'],
                   'source_digest': CONTEXT['source_digest'], 'configuration_sha256': CONTEXT['configuration_sha256'],
                   'authorization_sha256': CONTEXT['authorization_sha256'],
                   'authorization_expires_at': CONTEXT['authorization_expires_at']}
        raw = json.dumps(dict(payload, proof=proof(SECRET, payload))).encode()
        start_response('200 OK', [('Content-Type', 'application/json'), ('Cache-Control', 'no-store'),
                                  ('Content-Length', str(len(raw)))])
        return [raw]
    if PRODUCT is None:
        from django.core.wsgi import get_wsgi_application

        PRODUCT = get_wsgi_application()
    return PRODUCT(environ, start_response)

"""Render the repository Nginx entrypoint for isolated synthetic CI only."""

import argparse
import os
import re
from pathlib import Path


def render(source, root, directory, *, control=False):
    """Keep production directives; change only CI paths, loopback and upstream."""
    if len(re.findall(r'large_client_header_buffers\s+4\s+32k\s*;', source)) != 1:
        raise ValueError('Expected the reviewed production request-line budget')
    replacements = {
        '/label-studio/': root.as_posix().rstrip('/') + '/',
        '/tmp/': directory.as_posix().rstrip('/') + '/',
        'include resolv.conf;': 'resolver 127.0.0.1;',
        'listen      8085;': f'listen      127.0.0.1:{8086 if control else 8085};',
        'proxy_pass http://localhost:8000/;': 'proxy_pass http://127.0.0.1:8080/;',
    }
    for old, new in replacements.items():
        if old not in source:
            raise ValueError('Production Nginx layout changed; review CI adaptation')
        source = source.replace(old, new)
    if control:
        source = source.replace('large_client_header_buffers 4 32k;', 'large_client_header_buffers 4 8k;')
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--control', action='store_true')
    args = parser.parse_args()
    if os.name != 'posix' or os.environ.get('TEST_ENVIRONMENT') != '1':
        parser.error('Only isolated POSIX synthetic CI is supported')
    root = Path(__file__).resolve().parents[1]
    directory = Path('/tmp/ner-nginx-control' if args.control else '/tmp/ner-nginx')
    directory.mkdir(parents=True, exist_ok=True)
    config = render((root / 'deploy/default.conf').read_text(), root, directory, control=args.control)
    (directory / 'nginx.conf').write_text(config)
    print('Prepared isolated production' + (' 8KiB control' if args.control else '') + ' Nginx config')


if __name__ == '__main__':
    main()

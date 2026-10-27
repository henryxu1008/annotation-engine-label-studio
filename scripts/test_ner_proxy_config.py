"""Pure config contracts; real request-line enforcement runs in isolated CI."""

import ast
import re
import unittest
from pathlib import Path, PurePosixPath

from prepare_ner_proxy import render

ROOT = Path(__file__).resolve().parents[1]


class TestNerProxyConfig(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / 'deploy/default.conf').read_text()

    def test_production_request_line_fits_the_signed_receipt_and_framing(self):
        nodes = ast.parse((ROOT / 'label_studio/classification_disagreements/ner.py').read_text()).body
        maximum = next(ast.literal_eval(n.value) for n in nodes if isinstance(n, ast.Assign)
                       and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'MAX_TOKEN')
        match = re.search(r'large_client_header_buffers\s+4\s+(\d+)k\s*;', self.source)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match[1]) * 1024, maximum + 2048)
        self.assertIn('buffer-size = 65535', (ROOT / 'deploy/uwsgi.ini').read_text())

    def test_ci_adapts_production_instead_of_replacing_its_header_policy(self):
        config = render(self.source, PurePosixPath('/work/fork'), PurePosixPath('/tmp/ner-nginx'))
        self.assertIn('large_client_header_buffers 4 32k;', config)
        self.assertIn('listen      127.0.0.1:8085;', config)
        self.assertIn('pid /tmp/ner-nginx/nginx.pid;', config)
        self.assertIn('proxy_pass http://127.0.0.1:8080/;', config)
        self.assertIn('alias /work/fork/web/dist/apps/labelstudio/;', config)
        self.assertNotIn('/label-studio/', config)

    def test_negative_control_retains_the_old_eight_kibibyte_boundary(self):
        config = render(self.source, PurePosixPath('/work/fork'), PurePosixPath('/tmp/ner-nginx-control'), control=True)
        self.assertIn('large_client_header_buffers 4 8k;', config)
        self.assertIn('listen      127.0.0.1:8086;', config)
        self.assertIn('pid /tmp/ner-nginx-control/nginx.pid;', config)

    def test_unknown_production_layout_does_not_silently_use_a_fake_proxy(self):
        with self.assertRaises(ValueError):
            render(self.source.replace('large_client_header_buffers', 'removed_header_policy'),
                   PurePosixPath('/work/fork'), PurePosixPath('/tmp/ner-nginx'))


if __name__ == '__main__':
    unittest.main()

"""Public worker-pipe bounds; no browser, desktop or installed core is started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkerTransportTests(unittest.TestCase):
    def test_full_plaintext_bound_survives_json_escaping_before_policy_rejection(self):
        script = Path(__file__).with_name('browser_worker.py')
        for text in ('\n' * 65536, '\x01' * 65536, '🦀' * 16384):
            with self.subTest(encoded_bytes=len(text.encode())), tempfile.TemporaryDirectory() as home:
                request = {'identity': 'owned-pipe', 'method': 'browser.clipboard', 'params': {
                    'workspace': 'owned-pipe', 'generation': 'no-controller', 'target': 'no-page',
                    'operation': 'paste', 'text': text}}
                data = json.dumps(request, separators=(',', ':')).encode() + b'\n'
                result = subprocess.run([sys.executable, '-B', '-u', str(script), home, '/not-a-browser'],
                    input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                    env=dict(os.environ, HOME=home, XDG_RUNTIME_DIR=home, PYTHONDONTWRITEBYTECODE='1'))
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                # No runtime was started: the public policy rejects the unknown
                # workspace. A silent EOF instead means the pipe truncated text.
                reply = json.loads(result.stdout)
                self.assertEqual(reply['error']['kind'], 'not_running')


if __name__ == '__main__':
    unittest.main()

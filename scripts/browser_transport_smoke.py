#!/usr/bin/env python3
"""Two actual framed native viewers against an isolated feature core + Chrome.

No ordinary account endpoints or displays. Synthetic HTTP fixture, no credentials.
"""
import base64
import http.server
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time

BINARY = Path(__file__).resolve().parents[1] / 'target/debug/herdr-core'
root = Path(tempfile.mkdtemp(prefix='hc-bt.', dir='/var/tmp'))
root.chmod(0o700)
print(root, flush=True)
env = {k: v for k, v in os.environ.items() if not k.startswith('HC_')}
for key in ['XDG_RUNTIME_DIR', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'HOME', 'TMPDIR']:
    path = root / key.lower()
    path.mkdir(mode=0o700)
    env[key] = str(path)
env.update(HC_SESSION='g', HC_SOCKET=str(root / 'c.sock'), HC_BROWSER_CHROME='/opt/google/chrome/chrome')
log = (root / 'core.log').open('wb')
core = subprocess.Popen([str(BINARY), 'serve'], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)


class Viewer:
    def __init__(self, negotiate=True):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(25)
        self.sock.connect(str(root / 'c.sock'))
        self.file = self.sock.makefile('rb')
        self.counter = 0
        self.sock.sendall(b'{"jsonrpc":"2.0","id":0,"method":"hello","params":{"generation":1,"framed":true}}\n')
        hello = json.loads(self.file.readline())
        assert hello.get('result', {}).get('framed'), hello
        if negotiate:
            result = self.call('browser.negotiate', {'revision': 2, 'client': 'herdr-native'})
            assert 'devtools_stream' in result['capabilities'], result

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            chunk = self.file.read(size - len(data))
            if not chunk:
                raise EOFError('core framed connection closed')
            data.extend(chunk)
        return bytes(data)

    def call(self, method, params, error=None):
        self.counter += 1
        payload = json.dumps({'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params}).encode()
        self.sock.sendall(struct.pack('<IBI', 0, 0, len(payload)) + payload)
        while True:
            channel, kind, size = struct.unpack('<IBI', self.exact(9))
            assert size <= 16 * 1024 * 1024
            data = self.exact(size)
            if channel or kind:
                continue
            reply = json.loads(data)
            if reply.get('id') != self.counter:
                continue
            if error:
                assert reply.get('error', {}).get('data', {}).get('kind') == error, reply
                return reply['error']
            assert 'result' in reply, reply
            return reply['result']

    def close(self):
        self.file.close()
        self.sock.close()


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/state':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(observed).encode())
            return
        if self.path.startswith('/record?'):
            from urllib.parse import parse_qs, urlsplit
            observed.append(parse_qs(urlsplit(self.path).query))
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'''<!doctype html><input autofocus id=draft><div style="height:2000px">fixture</div>
<script>window.marker=Math.random().toString();setInterval(()=>fetch('/record?draft='+encodeURIComponent(draft.value)+'&marker='+marker),200);</script>''')
    def log_message(self, *args):
        pass


observed = []
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
viewers = []
report = {}
try:
    deadline = time.monotonic() + 5
    while not (root / 'c.sock').exists():
        if time.monotonic() > deadline or core.poll() is not None:
            raise RuntimeError('isolated core startup failed; see core.log')
        threading.Event().wait(.01)
    unadmitted = Viewer(False)
    viewers.append(unadmitted)
    unadmitted.call('browser.ensure', {'workspace': 'test'}, error='viewer_required')
    unadmitted.call('browser.negotiate', {'revision': 1, 'client': 'herdr-native'}, error='unsupported_version')
    one, two = Viewer(), Viewer()
    viewers += [one, two]
    p = {'workspace': '550e8400-e29b-41d4-a716-446655440000'}
    ensured = one.call('browser.ensure', p)
    p.update(one.call('browser.acquire', p))
    target = one.call('browser.tabs', p)['selected']
    page = dict(p, target=target)
    surface = dict(p, surface={'kind': 'page', 'target': target})
    two.call('browser.acquire', {'workspace': p['workspace']}, error='owned')
    two.call('browser.tabs', p, error='not_owner')
    two.call('browser.frame', surface, error='not_owner')
    two.call('browser.acquire', {'workspace': p['workspace'], 'force': True}, error='invalid_params')
    one.call('browser.navigate', dict(page, url='http://127.0.0.1:' + str(server.server_port) + '/'))
    one.call('browser.viewport', dict(surface, width=900, height=700, scale=1))
    deadline = time.monotonic() + 5
    while not observed and time.monotonic() < deadline:
        threading.Event().wait(.02)
    assert observed
    marker = observed[-1]['marker']
    one.call('browser.input', dict(surface, event={'type': 'text', 'text': 'unsent-native-draft'}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and observed[-1].get('draft') != ['unsent-native-draft']:
        threading.Event().wait(.02)
    assert observed[-1]['draft'] == ['unsent-native-draft'], observed[-1]
    latencies = []
    ack = 0
    for index in range(10):
        start = time.monotonic()
        image = one.call('browser.frame', dict(surface, ack=ack))
        latencies.append(time.monotonic() - start)
        ack = image['sequence']
        one.call('browser.heartbeat', p)
    (root / 'page.png').write_bytes(base64.b64decode(image['event']['data']))
    one.call('browser.frame', dict(surface, ack=0), error='invalid_params')
    one.call('browser.devtools.open', page)
    tools = dict(p, surface={'kind': 'devtools', 'target': target})
    one.call('browser.viewport', dict(tools, width=900, height=500, scale=1))
    tool_image = one.call('browser.frame', tools)
    (root / 'devtools.png').write_bytes(base64.b64decode(tool_image['event']['data']))
    new = two.call('browser.acquire', {'workspace': p['workspace'], 'force': True, 'confirmed': True})
    one.call('browser.input', dict(surface, event={'type': 'text', 'text': 'BAD'}), error='not_owner')
    one.call('browser.frame', tools, error='not_owner')
    one.call('browser.devtools.open', page, error='not_owner')
    other = dict(p, generation=new['generation'])
    two.call('browser.frame', dict(other, surface={'kind': 'devtools', 'target': target}), error='not_found')
    two.call('browser.release', other)
    one.close()
    viewers.remove(one)
    three = Viewer()
    viewers.append(three)
    live = three.call('browser.ensure', {'workspace': p['workspace']})
    assert live['instance'] == ensured['instance'] and live['pid'] == ensured['pid'] and live['recovery'] == 'live', (live, ensured)
    third = dict(p, **three.call('browser.acquire', {'workspace': p['workspace']}))
    tabs = three.call('browser.tabs', third)
    assert target in [t['id'] for t in tabs['tabs']], tabs
    assert observed[-1]['marker'] == marker and observed[-1]['draft'] == ['unsent-native-draft'], observed[-1]
    # Real connection EOF (without release) must relinquish ownership, not kill.
    three.close()
    viewers.remove(three)
    deadline = time.monotonic() + 3
    while True:
        try:
            reacquired = two.call('browser.acquire', {'workspace': p['workspace']})
            break
        except AssertionError as error:
            if time.monotonic() >= deadline:
                raise
    final = dict(p, **reacquired)
    assert two.call('browser.ensure', {'workspace': p['workspace']})['pid'] == ensured['pid']
    stopped = two.call('browser.stop', dict(final, confirmed=True))
    assert stopped['state'] == 'stopped'
    report = {'result': 'PASS', 'scope': 'real core framed transport + Chrome, not native GPUI gate',
              'admission': 'PASS', 'uuid_profile': 'PASS', 'exclusive_takeover_stale_input_devtools': 'PASS',
              'same_pid_target_draft_memory_reconnect': 'PASS', 'disconnect_no_release': 'PASS',
              'frame_ack': 'PASS', 'confirmed_stop': 'PASS', 'capture_ms': [round(x*1000, 2) for x in latencies],
              'binary': str(BINARY), 'ensured': ensured}
    print(json.dumps(report, indent=2), flush=True)
finally:
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    for viewer in viewers:
        viewer.close()
    server.shutdown()
    server.server_close()
    core.terminate()
    try:
        core.wait(timeout=25)
    except subprocess.TimeoutExpired:
        core.kill()
        core.wait()
    log.close()

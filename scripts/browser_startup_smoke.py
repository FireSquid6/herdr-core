#!/usr/bin/env python3
"""Real native startup/select/DevTools order, recording transient capture errors.

Navigation acceptance is not completion; retry capture for a bounded three seconds
just as the native viewer polls. No activation or Page-domain patch is applied.
"""
import base64
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from browser_worker import Worker

root = Path(tempfile.mkdtemp(prefix='hc-start.', dir='/var/tmp'))
root.chmod(0o700)
print(root, flush=True)


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h1>startup fixture</h1><input autofocus>')
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
w = Worker(root, '/opt/google/chrome/chrome')
report = []
try:
    for index in range(3):
        p = {'workspace': 'startup_' + str(index)}
        def call(method, more=None):
            try:
                result = w.dispatch('viewer', method, dict(p, **(more or {})))
                report.append({'run': index, 'method': method, 'result': 'PASS'})
                return result
            except Exception as error:
                report.append({'run': index, 'method': method, 'error': str(error)})
                raise
        def capture(surface, ack, phase):
            started = time.monotonic()
            deadline = started + 3
            errors = []
            while True:
                try:
                    frame = call('browser.frame', {'surface': surface, 'ack': ack})
                    report.append({'run': index, 'phase': phase, 'transient_errors': errors,
                                   'elapsed_ms': round((time.monotonic()-started)*1000, 2)})
                    return frame
                except Exception as error:
                    errors.append(str(error))
                    if time.monotonic() >= deadline:
                        raise
                    threading.Event().wait(.1)
        call('browser.ensure')
        p.update(call('browser.acquire'))
        target = call('browser.tabs')['selected']
        surface = {'kind': 'page', 'target': target}
        first = capture(surface, 0, 'initial-blank-before-viewport')
        call('browser.viewport', {'surface': surface, 'width': 1090, 'height': 554, 'scale': 1})
        second = capture(surface, first['sequence'], 'blank-after-viewport')
        call('browser.navigate', {'target': target, 'url': 'http://127.0.0.1:' + str(server.server_port) + '/browser-fixture.html'})
        third = capture(surface, second['sequence'], 'immediate-navigation')
        (root / f'page-{index}.png').write_bytes(base64.b64decode(third['event']['data']))
        if index == 0:
            tab = call('browser.tab.create')['id']
            call('browser.tab.select', {'target': tab})
            other = {'kind': 'page', 'target': tab}
            call('browser.viewport', {'surface': other, 'width': 900, 'height': 600, 'scale': 1})
            capture(other, 0, 'select-create-without-activate')
            call('browser.tab.select', {'target': target})
            fourth = capture(surface, third['sequence'], 'reselect-original-without-activate')
            call('browser.devtools.open', {'target': target})
            fifth = capture(surface, fourth['sequence'], 'page-after-inspect-helper-close')
            tools = {'kind': 'devtools', 'target': target}
            call('browser.viewport', {'surface': tools, 'width': 900, 'height': 450, 'scale': 1})
            tool_frame = capture(tools, 0, 'frontend-after-undock')
            call('browser.tab.select', {'target': tab})
            call('browser.tab.select', {'target': target})
            call('browser.devtools.open', {'target': target})  # existing frontend, NOT a reset
            try:
                call('browser.frame', {'surface': tools, 'ack': 0})
                raise AssertionError('reset ACK unexpectedly accepted for retained frontend')
            except Exception as error:
                assert getattr(error, 'kind', None) == 'invalid_params', error
            capture(tools, tool_frame['sequence'], 'reopen-retained-frontend-requires-retained-ack')
            call('browser.devtools.close', {'target': target})
            capture(surface, fifth['sequence'], 'page-after-frontend-close')
            call('browser.tab.close', {'target': tab})
        call('browser.stop', {'confirmed': True})
    print(json.dumps(report, indent=2), flush=True)
finally:
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    server.shutdown()
    server.server_close()
    for b in w.browsers.values():
        try:
            b.cdp.call('Browser.close')
            os.waitpid(b.pid, 0)
            b.expiry_stop.set()
            b.cdp.close()
            b.cleanup_runtime()
        except Exception as error:
            print('cleanup', str(error), flush=True)

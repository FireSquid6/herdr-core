#!/usr/bin/env python3
"""Real isolated profile/tab/opener, permissions, sandbox, external-URI baseline."""
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from browser_worker import Worker
from browser_probe import sandbox_enabled

root = Path(tempfile.mkdtemp(prefix='hc-bs.', dir='/var/tmp'))
root.chmod(0o700)
print(root, flush=True)


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'''<!doctype html><h1>profile fixture</h1>
<button id=popup onclick="window.open('/popup','native-popup')">Open popup</button>
<a id=external href="hcdeny://synthetic-test">external protocol</a><script>window.liveMarker=918273;</script>''')
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:' + str(server.server_port) + '/'
w = Worker(root / 'state/herdr-core/profile-gate/browsers', '/opt/google/chrome/chrome')
report = {}


def evaluate(b, target, expression):
    result = b.cdp.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True}, b.session(target))
    assert 'exceptionDetails' not in result, result
    return result.get('result', {}).get('value')


def ready(b, target):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if evaluate(b, target, '!!document.querySelector("#popup")'):
            return
    raise AssertionError('fixture did not load')


try:
    p = {'workspace': '550e8400-e29b-41d4-a716-446655440000'}
    w.dispatch('one', 'browser.ensure', p)
    p.update(w.dispatch('one', 'browser.acquire', p))
    b = w.browsers[p['workspace']]
    first = w.dispatch('one', 'browser.tabs', p)['selected']
    w.dispatch('one', 'browser.navigate', dict(p, target=first, url=url))
    ready(b, first)
    evaluate(b, first, "document.cookie='gate=one; path=/';localStorage.setItem('gate','one')")
    second = w.dispatch('one', 'browser.tab.create', dict(p, url=url))['id']
    ready(b, second)
    shared = evaluate(b, second, '({cookie:document.cookie,local:localStorage.getItem("gate")})')
    assert shared['cookie'] == 'gate=one' and shared['local'] == 'one', shared
    isolated = {'workspace': '550e8400-e29b-41d4-a716-446655440001'}
    w.dispatch('one', 'browser.ensure', isolated)
    isolated.update(w.dispatch('one', 'browser.acquire', isolated))
    other = w.browsers[isolated['workspace']]
    other_target = w.dispatch('one', 'browser.tabs', isolated)['selected']
    w.dispatch('one', 'browser.navigate', dict(isolated, target=other_target, url=url))
    ready(other, other_target)
    separate = evaluate(other, other_target, '({cookie:document.cookie,local:localStorage.getItem("gate")})')
    assert separate['cookie'] == '' and separate['local'] is None, separate
    permissions = evaluate(b, first, '''Promise.all(['geolocation','camera','microphone','notifications','clipboard-read','clipboard-write'].map(async name=>({name,state:(await navigator.permissions.query({name})).state})))''')
    assert all(p['state'] == 'denied' for p in permissions), permissions
    # Actual trusted pointer click creates a genuine popup target with opener.
    def click(selector):
        rect = evaluate(b, first, f'(()=>{{let r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()')
        for action, buttons in [('down', 1), ('up', 0)]:
            w.dispatch('one', 'browser.input', dict(p, surface={'kind':'page','target':first}, event=dict(type='pointer',action=action,button='left',buttons=buttons,click_count=1,modifiers=0,**rect)))
    click('#popup')
    deadline = time.monotonic() + 5
    popup = None
    while time.monotonic() < deadline:
        tabs = w.dispatch('one', 'browser.tabs', p)['tabs']
        popup = next((t for t in tabs if t.get('opener') == first), None)
        if popup:
            break
    assert popup, tabs
    ready(b, popup['id'])
    popup = next(t for t in w.dispatch('one', 'browser.tabs', p)['tabs'] if t['id'] == popup['id'])
    assert evaluate(b, popup['id'], 'window.opener.liveMarker') == 918273
    # Synthetic external handler writes only inside THIS private test root. No
    # real desktop program or user config is installed/touched.
    marker = root / 'EXTERNAL_HANDLER_RAN'
    handler = root / 'synthetic-handler'
    handler.write_text('#!/bin/sh\nprintf attempted > ' + str(marker) + '\n')
    handler.chmod(0o700)
    apps = b.root / 'xdg_data_home/applications'
    apps.mkdir(parents=True, exist_ok=True)
    (apps / 'hcdeny.desktop').write_text('[Desktop Entry]\nType=Application\nName=Synthetic Test\nExec=' + str(handler) + ' %u\nMimeType=x-scheme-handler/hcdeny;\n')
    (b.root / 'xdg_config_home/mimeapps.list').write_text('[Default Applications]\nx-scheme-handler/hcdeny=hcdeny.desktop\n')
    click('#external')
    # Bounded Chrome roundtrips let pending navigation/handler work complete.
    until = time.monotonic() + 1
    while time.monotonic() < until:
        b.cdp.call('Target.getTargets')
    assert not marker.exists(), 'external OS handler executed'
    proc_env = Path(f'/proc/{b.pid}/environ').read_bytes().split(b'\0')
    assert not any(value.startswith(b'DBUS_SESSION_BUS_ADDRESS=') for value in proc_env)
    assert ('PATH=' + str(b.root / 'external-denied')).encode() in proc_env
    assert not list((b.root / 'external-denied').iterdir())
    # Prove actual worker launch sandbox, not merely flags/probe substitution.
    sandbox = b.cdp.call('Target.createTarget', {'url':'chrome://sandbox'})['targetId']
    deadline = time.monotonic() + 5
    text = ''
    while time.monotonic() < deadline:
        text = evaluate(b, sandbox, 'document.body?.innerText') or ''
        if sandbox_enabled(text):
            break
    assert sandbox_enabled(text), text
    (root / 'sandbox.txt').write_text(text)
    b.cdp.call('Target.closeTarget', {'targetId':sandbox})
    report = {'result':'PASS','scope':'real Chrome worker, not native GPUI',
              'same_workspace_shared':shared,'second_workspace_isolated':separate,
              'permissions':permissions,'popup':popup,'opener_memory':918273,
              'external_handler_executed':marker.exists(),'external_helpers':'private empty PATH; no desktop bus',
              'sandbox':'PASS','uid':os.geteuid(),'profile_mode':oct((b.root/'profile').stat().st_mode & 0o777)}
    print(json.dumps(report,indent=2),flush=True)
finally:
    server.shutdown()
    server.server_close()
    cleanup = []
    for b in w.browsers.values():
        try:
            b.cdp.call('Browser.close')
            os.waitpid(b.pid, 0)
            b.expiry_stop.set()
            b.cdp.close()
            cleanup.append({'pid':b.pid,'runtime_removed':b.cleanup_runtime()})
        except Exception as error:
            cleanup.append({'pid':b.pid,'error':str(error)})
    report['cleanup'] = cleanup
    (root/'report.json').write_text(json.dumps(report,indent=2))

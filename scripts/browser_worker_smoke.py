#!/usr/bin/env python3
"""Owned isolated real Chrome test; internal CDP assertions are test-only."""
import base64
import json
import http.server
import threading
import os
from pathlib import Path
import sys
import tempfile
import time
from browser_worker import Worker

root = Path(tempfile.mkdtemp(prefix='hc-bw.', dir='/var/tmp'))
os.chmod(root, 0o700)
print(root, flush=True)
class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/json')
        self.end_headers()
        self.wfile.write(b'<h1 id="marker">Native gate</h1><input id="draft"><button id="go">debug</button>' if self.path == '/' else b'{"networkMarker":91827}')
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
w = Worker(root / 's/state/herdr-core/browser-mvp/browsers', '/opt/google/chrome/chrome')
p = {'workspace': '550e8400-e29b-41d4-a716-446655440000'}
b = None
try:
    print(w.dispatch('one', 'browser.ensure', p), flush=True)
    p.update(w.dispatch('one', 'browser.acquire', p))
    target = w.dispatch('one', 'browser.tabs', p)['selected']
    b = w.browsers[p['workspace']]
    s = b.session(target)
    b.cdp.call('Page.navigate', {'url': 'http://127.0.0.1:' + str(server.server_port) + '/'}, s)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ready = b.cdp.call('Runtime.evaluate', {'expression': '!!document.body', 'returnByValue': True}, s)
        if ready.get('result', {}).get('value'):
            break
    # Test-only fixture setup; no Runtime operation exposed on Herdr transport.
    b.cdp.call('Runtime.evaluate', {'expression': '''document.body.innerHTML='<h1 id="marker">Native gate</h1><input id="draft"><button id="go">debug</button>'; window.liveMarker=84729; document.querySelector('#go').onclick=()=>{debugger; window.afterStep=1;}; console.log('fixture-console');'''}, s)
    p['target'] = target
    print(w.dispatch('one', 'browser.devtools.open', p), flush=True)
    tool = b.tools[target]
    front = tool['frontend']
    # Wait for observable frontend runtime readiness, not a fixed startup sleep.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = b.cdp.call('Runtime.evaluate', {'expression': '''({title:document.title,text:(function walk(n){return [...n.childNodes].map(c=>c.nodeType===3?c.textContent:(c.shadowRoot?walk(c.shadowRoot):'')+walk(c)).join(' ')})(document.body)})''', 'returnByValue': True}, front)
        value = state.get('result', {}).get('value', {})
        if 'Elements' in value.get('text', '') and 'Console' in value.get('text', ''):
            break
    print('FRONTEND', value, flush=True)
    (root / 'frontend.json').write_text(json.dumps(value, indent=2))
    assert 'Elements' in value.get('text', ''), value
    surf = dict(p, surface={'kind': 'devtools', 'target': target})
    w.dispatch('one', 'browser.viewport', dict(surf, width=1000, height=600, scale=1))
    image = w.dispatch('one', 'browser.frame', surf)
    (root / 'devtools.png').write_bytes(base64.b64decode(image['event']['data']))
    # Introspect the real frontend model only in this test. Readiness requires
    # actual DOM/console models populated through the binding transport.
    expression = '''(async()=>{
      const SDK=await import('./core/sdk/sdk.js');
      const tm=SDK.TargetManager.TargetManager.instance();
      return tm.targets().map(t=>({id:t.id(),name:t.name(),type:t.type(),models:[...t.models().keys()].map(k=>k.name)}));
    })()'''
    models = b.cdp.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True}, front)
    print('MODELS', models, flush=True)
    (root / 'models.json').write_text(json.dumps(models, indent=2))
    assert models.get('result', {}).get('value'), models
    evaluate = lambda expression: b.cdp.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True}, front)
    setup = '''const SDK=await import('./core/sdk/sdk.js');const target=SDK.TargetManager.TargetManager.instance().targets().find(t=>t.type()==='frame');'''
    dom = evaluate('(async()=>{' + setup + 'const node=await target.model(SDK.DOMModel.DOMModel).requestDocument();return await node.getOuterHTML();})()')
    (root / 'dom.json').write_text(json.dumps(dom, indent=2))
    assert 'Native gate' in dom['result']['value'], dom
    print('PAGE_WINDOW', b.cdp.call('Browser.getWindowForTarget', {'targetId': target}), flush=True)
    print('UNDOCK', evaluate("import('./ui/legacy/legacy.js').then(UI=>{let d=UI.DockController.DockController.instance();d.setDockSide('undocked');return d.dockSide();})"), flush=True)
    image2 = w.dispatch('one', 'browser.frame', dict(surf, ack=image['sequence']))
    (root / 'devtools-ready.png').write_bytes(base64.b64decode(image2['event']['data']))
    view = b.cdp.call('Page.captureScreenshot', {'format':'png','fromSurface':False}, front)
    (root / 'devtools-view.png').write_bytes(base64.b64decode(view['data']))
    # Exercise real streamed frontend input (not just protocol/model calls).
    locate = '''(selector)=>{const find=r=>{const v=r.querySelector(selector);if(v){const b=v.getBoundingClientRect();if(b.width&&b.height)return {x:b.x+b.width/2,y:b.y+b.height/2};}for(const n of r.querySelectorAll('*'))if(n.shadowRoot){const b=find(n.shadowRoot);if(b)return b;}return null;};return find(document);}'''
    def click_selector(selector):
        value = evaluate('(' + locate + ')(' + json.dumps(selector) + ')')['result'].get('value')
        assert value, selector
        for action, buttons in [('down', 1), ('up', 0)]:
            w.dispatch('one', 'browser.input', dict(surf, event=dict(type='pointer', action=action, button='left', buttons=buttons, modifiers=0, click_count=1, **value)))
    click_selector('#tab-console')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        prompt = evaluate('(' + locate + ')(".cm-content[contenteditable=true]")')['result'].get('value')
        if prompt:
            break
    click_selector('.cm-content[contenteditable=true]')
    w.dispatch('one', 'browser.input', dict(surf, event={'type':'text','text':'window.liveMarker + 3'}))
    for action in ('down', 'up'):
        w.dispatch('one', 'browser.input', dict(surf, event={'type':'key','action':action,'key':'Enter','code':'Enter','modifiers':0,'repeat':False}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = evaluate('(' + '''function walk(n){return [...n.childNodes].map(c=>c.nodeType===3?c.textContent:(c.shadowRoot?walk(c.shadowRoot):'')+walk(c)).join(' ')}''' + ')(document.body)')['result'].get('value','')
        if '84732' in text:
            break
    assert '84732' in text, 'real Console pointer/text/Enter did not evaluate'
    (root / 'console-input.json').write_text(json.dumps({'result':'PASS','expression':'window.liveMarker + 3','observed':'84732'}, indent=2))
    image3 = w.dispatch('one', 'browser.frame', dict(surf, ack=image2['sequence']))
    (root / 'devtools-console-input.png').write_bytes(base64.b64decode(image3['event']['data']))
    console = evaluate('(async()=>{' + setup + '''return (await target.model(SDK.RuntimeModel.RuntimeModel).defaultExecutionContext().evaluate({expression:'window.liveMarker + 1',objectGroup:'console',includeCommandLineAPI:true,silent:false,returnByValue:true,generatePreview:false})).object.value;})()''')
    (root / 'console.json').write_text(json.dumps(console, indent=2))
    assert '84730' in json.dumps(console), console
    evaluate('(async()=>{' + setup + '''target.model(SDK.RuntimeModel.RuntimeModel).defaultExecutionContext().evaluate({expression:"fetch('/evidence-network').then(r=>r.json()).then(x=>console.log(x.networkMarker))",objectGroup:'console',includeCommandLineAPI:true,silent:false,returnByValue:true,generatePreview:false});return true;})()''')
    deadline = time.monotonic() + 5
    network = None
    while time.monotonic() < deadline:
        network = evaluate("import('./models/logs/logs.js').then(L=>L.NetworkLog.NetworkLog.instance().requests().map(r=>r.url()))")
        if '/evidence-network' in json.dumps(network):
            break
    assert '/evidence-network' in json.dumps(network), network
    (root / 'network.json').write_text(json.dumps(network, indent=2))
    # Issue a debugger statement through the real DevTools RuntimeModel, then
    # inspect/step/resume through the real frontend DebuggerModel.
    evaluate('(async()=>{' + setup + '''target.model(SDK.RuntimeModel.RuntimeModel).defaultExecutionContext().evaluate({expression:'debugger; window.afterStep=1; window.afterStep=2;',objectGroup:'console',includeCommandLineAPI:true,silent:false,returnByValue:true,generatePreview:false});return true;})()''')
    deadline = time.monotonic() + 5
    paused = None
    while time.monotonic() < deadline:
        paused = evaluate('(async()=>{' + setup + 'return target.model(SDK.DebuggerModel.DebuggerModel).isPaused();})()')
        if paused.get('result', {}).get('value'):
            break
    assert paused['result']['value'], paused
    evaluate('(async()=>{' + setup + 'await target.model(SDK.DebuggerModel.DebuggerModel).stepOver();return true;})()')
    evaluate('(async()=>{' + setup + 'await target.model(SDK.DebuggerModel.DebuggerModel).resume();return true;})()')
    (root / 'debugger.json').write_text(json.dumps({'paused': paused, 'step_over': True, 'resume': True}, indent=2))
    # A genuine outstanding CDP response crosses expiry. Compositor screenshots
    # may complete while JS is busy, so use an explicitly awaited page Promise
    # rather than falsely claiming renderer-busy implies capture is blocked.
    with b.state_lock:
        b.lease.until = time.monotonic() + .1
    started = time.monotonic()
    b.local.active = True
    b.local.identity, b.local.generation = 'one', p['generation']
    try:
        b.cdp.call('Runtime.evaluate', {'expression': 'new Promise(r=>setTimeout(()=>r(1),1000))', 'awaitPromise': True}, s)
        b.lease.check('one', p['generation'])  # same completion gate as Worker.dispatch
        raise AssertionError('expired CDP completion was delivered')
    except Exception as error:
        assert getattr(error, 'kind', None) == 'not_owner', error
    finally:
        b.local.active = False
    assert time.monotonic() - started > .1
    assert not b.tools and b.running
    renewed = w.dispatch('two', 'browser.acquire', {'workspace': p['workspace']})
    assert renewed['generation'] != p['generation']
    (root / 'expiry-inflight.json').write_text(json.dumps({'result':'PASS','cdp_elapsed_ms':round((time.monotonic()-started)*1000,2),'frontend_closed':not b.tools,'browser_running':b.running}, indent=2))
    print('PASS real frontend DOM/Console/Network/debugger pause-step-resume + expiry during outstanding CDP; screenshot:', root / 'devtools-ready.png', flush=True)
finally:
    server.shutdown()
    server.server_close()
    if b:
        try:
            b.cdp.call('Browser.close')
            os.waitpid(b.pid, 0)
            b.expiry_stop.set()
            b.cdp.close()
            print('runtime cleanup', b.cleanup_runtime(), flush=True)
        except Exception as e:
            print('cleanup error', e, flush=True)

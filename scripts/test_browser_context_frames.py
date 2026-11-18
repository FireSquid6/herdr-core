"""Owned native frame acceptance gaps: failures are intentionally NOT xfail/skip.

Run with HC_BROWSER_CONTEXT_FRAMES_NATIVE_TEST=1, scratch HC_SESSION/HOME/XDG,
and HC_BROWSER_CONTEXT_FRAMES_EVIDENCE=/absolute/owned/evidence-directory.
Only Worker.dispatch drives context/edit/input. Direct CDP is fixture setup,
readonly lifecycle/geometry diagnostics and permission-denial assertions, never
source selected-text extraction or a clipboard-result fallback.
"""
import http.server
import json
import os
from pathlib import Path
import signal
import shutil
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from browser_worker import Fault, Worker
from browser_probe import private_processes, sandbox_enabled

ROOT_WITNESS = 'ROOT-DECOY π — must never be child-copy'
A_WITNESS = 'CHILD-A π\nowned first frame'
B_WITNESS = 'CHILD-B 🦀\nowned sibling frame'
NEW_WITNESS = 'REPLACEMENT-CHILD — never stale-copy'


@unittest.skipUnless(os.environ.get('HC_BROWSER_CONTEXT_FRAMES_NATIVE_TEST') == '1',
                     'explicit isolated native frame acceptance opt-in')
class NativeContextFrames(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0, 'normal user required')
        self.assertTrue(os.environ.get('HC_SESSION') not in (None, '', 'default'), 'scratch HC_SESSION required')
        evidence = os.environ.get('HC_BROWSER_CONTEXT_FRAMES_EVIDENCE')
        self.assertTrue(evidence, 'explicit owned evidence directory required')
        self.evidence = Path(evidence).resolve(strict=True)
        self.assertEqual(self.evidence.stat().st_uid, os.geteuid())
        self.name = self._testMethodName
        self.report = {'test': self.name, 'scope': 'owned headless frame acceptance, NOT OS/GPUI',
                       'expected': {}, 'steps': [], 'events': [], 'result': 'INCOMPLETE'}
        self.event_lock = threading.Lock()
        self.gate_requested = threading.Event()
        self.release_gate = threading.Event()
        self.gate_on_command = False
        self.root = Path(tempfile.mkdtemp(prefix='hc-frame-', dir='/var/tmp'))
        self.worker = Worker(self.root, '/opt/google/chrome/chrome')
        self.p = {'workspace': 'owned'}
        self.browser = None
        self.helper_ready = False
        self.server = None
        self.watchdog = None
        # Clean up even if setUp fails midway.
        self.addCleanup(self.finish)
        self.call('browser.ensure')
        self.browser = self.worker.browsers['owned']
        self.report.update(pid=self.browser.pid, version=self.browser.version,
                           source_profile_isolation='fresh owned workspace/profile; no imported user data')
        def deadline():
            self.report['watchdog_fired'] = True
            if self.browser.running:
                try:
                    os.killpg(self.browser.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.watchdog = threading.Timer(35, deadline)
        self.watchdog.daemon = True
        self.watchdog.start()
        self.p.update(self.call('browser.acquire'))
        self.target = self.call('browser.tabs')['selected']
        self.surface = {'kind': 'page', 'target': self.target}
        self.session = self.browser.session(self.target)
        self.browser.cdp.call('Page.navigate', {'url': 'chrome://sandbox'}, self.session)
        self.wait(lambda: sandbox_enabled(self.evaluate('document.body?.innerText') or ''), 'enabled sandbox')
        self.report['sandbox'] = self.evaluate('document.body.innerText')
        self.browser.cdp.call('Page.navigate', {'url':'about:blank'}, self.session)
        self.wait(lambda:self.evaluate('location.href==="about:blank"'), 'owned fixture return from sandbox diagnostic')
        self.start_server()
        self.call('browser.viewport', surface=self.surface, width=1000, height=800, scale=1)

    def call(self, method, **extra):
        started = time.monotonic()
        try:
            result = self.worker.dispatch('viewer', method, dict(self.p, **extra))
            self.report['steps'].append({'method': method, 'elapsed_ms': round((time.monotonic()-started)*1000, 2),
                                         'result': result})
            return result
        except Fault as error:
            self.report['steps'].append({'method': method, 'elapsed_ms': round((time.monotonic()-started)*1000, 2),
                                         'fault': {'kind': error.kind, 'message': str(error)}})
            raise

    def evaluate(self, expression):
        reply = self.browser.cdp.call('Runtime.evaluate', {'expression': expression,
                    'returnByValue': True, 'awaitPromise': True}, self.session)
        if 'exceptionDetails' in reply:
            raise AssertionError('owned fixture script failed: '+str(reply['exceptionDetails']))
        return reply.get('result', {}).get('value')

    def wait(self, predicate, label, seconds=4):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('bounded wait failed: '+label)

    def start_server(self):
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == '/event':
                    event = {k:v[0] for k,v in query.items()}
                    with fixture.event_lock:
                        fixture.report['events'].append({'at':time.monotonic(), **event})
                    if (fixture.gate_on_command and event.get('phase') == 'command-a'
                            and event.get('gateArmed') == 'true'):
                        fixture.release_gate.set()
                    body = 'ok'
                elif parsed.path == '/root':
                    cross = query.get('cross', ['0'])[0] == '1'
                    divert = query.get('divert', ['0'])[0] == '1'
                    port = fixture.server.server_port
                    a_host = 'localhost' if cross else '127.0.0.1'
                    body = '''<!doctype html><meta charset=utf-8><style>
body{margin:0}textarea{position:absolute;left:20px;top:20px;width:600px;height:90px}
iframe{position:absolute;left:20px;width:360px;height:180px;border:0}#a{top:160px}#b{top:380px}
</style><textarea id=rootdecoy></textarea><textarea id=rootother style="left:650px;width:250px"></textarea>
<iframe id=a src="http://HOST:PORT/child?name=a&divert=DIVERT"></iframe>
<iframe id=b src="http://127.0.0.1:PORT/child?name=b"></iframe>
<a id=rootlink href='/owned-link' style="position:absolute;left:20px;top:720px">Owned link</a>
<script>
rootdecoy.value=ROOT;rootother.value='OTHER-ELEMENT must not escape';window.nativeCommands=0;
window.addEventListener('keydown',e=>{if(e.key==='F24'){
 nativeCommands++;
 if(window.divertOnCommand==='element'){rootother.focus();rootother.select();}
 if(window.divertOnCommand==='frame'){b.contentWindow.editor.focus();b.contentWindow.editor.select();}
}});
window.addEventListener('contextmenu',e=>{
 if(window.divertMenuElement && e.target===rootdecoy){e.preventDefault();rootdecoy.select();rootother.focus();rootother.select();}
});
window.addEventListener('message',e=>{
 if(e.source===a.contentWindow && e.data==='focus-sibling')b.contentWindow.postMessage('focus-select','*');
});
</script>'''.replace('HOST',a_host).replace('PORT',str(port)).replace('DIVERT','1' if divert else '0').replace('ROOT',json.dumps(ROOT_WITNESS))
                elif parsed.path in ('/child', '/gated'):
                    name = query.get('name',['a-new' if parsed.path == '/gated' else 'a'])[0]
                    if parsed.path == '/gated':
                        fixture.gate_requested.set()
                        if not fixture.release_gate.wait(2):
                            with fixture.event_lock:
                                fixture.report['events'].append({'phase':'gate-timeout-release','at':time.monotonic()})
                    witness = A_WITNESS if name == 'a' else B_WITNESS if name == 'b' else NEW_WITNESS
                    divert = query.get('divert',['0'])[0] == '1'
                    body = '''<!doctype html><meta charset=utf-8>
<style>body{margin:0}textarea{position:absolute;left:20px;top:20px;width:260px;height:95px}</style>
<textarea id=editor></textarea><script>
const name=NAME;editor.value=WITNESS;let gateArmed=false;window.nativeCommands=0;
function event(phase,extra={}){fetch('/event?'+new URLSearchParams({phase,...extra}));}
async function permissions(){
 const readState=(await navigator.permissions.query({name:'clipboard-read'})).state;
 const writeState=(await navigator.permissions.query({name:'clipboard-write'})).state;
 let read,write;try{await navigator.clipboard.readText();read='ALLOWED'}catch(e){read=e.name}
 try{await navigator.clipboard.writeText('owned-denial-attempt');write='ALLOWED'}catch(e){write=e.name}
 event('permissions-'+name,{readState,writeState,read,write});
}
window.addEventListener('contextmenu',e=>{
 e.preventDefault();event('contextmenu-'+name,{trusted:e.isTrusted});
 if(DIVERT){editor.select();parent.postMessage('focus-sibling','*');}
});
window.addEventListener('keydown',e=>{
 if(e.key==='F24'){nativeCommands++;event('command-'+name,{trusted:e.isTrusted,gateArmed});}
});
window.addEventListener('message',e=>{
 if(e.data==='focus-select'){editor.focus();editor.select();event('focused-'+name);}
 if(e.data==='permissions')permissions();
 if(e.data==='navigate-gated'){gateArmed=true;location.href='/gated?name=a-new';}
});
event('loaded-'+name);permissions();
</script>'''.replace('NAME',json.dumps(name)).replace('WITNESS',json.dumps(witness)).replace('DIVERT','true' if divert else 'false')
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                try:
                    self.wfile.write(body.encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self,*args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def has_event(self, phase):
        with self.event_lock:
            return any(e.get('phase') == phase for e in self.report['events'])

    def load(self, cross=False, divert=False):
        url = f'http://127.0.0.1:{self.server.server_port}/root?cross={int(cross)}&divert={int(divert)}'
        self.call('browser.navigate',target=self.target,url=url)
        self.wait(lambda:self.has_event('loaded-a') and self.has_event('loaded-b'), 'both owned frames loaded')
        self.wait(lambda:self.has_event('permissions-a') and self.has_event('permissions-b'), 'frame permission receipts')
        self.assert_permissions()
        self.focus_root()
        # Real top-level Copy bootstraps the native helper, never an OS clipboard.
        copied = self.call('browser.clipboard',target=self.target,operation='copy')
        self.assertEqual(copied,{'kind':'copied','text':ROOT_WITNESS})
        self.helper_ready = True
        self.private_boundary('before')
        self.report['initial_lifecycle'] = self.lifecycle()

    def focus_root(self):
        self.evaluate('rootdecoy.value='+json.dumps(ROOT_WITNESS)+';rootdecoy.focus();rootdecoy.select();true')

    def point(self, child):
        # Only geometry, no field value/selected text. Point lies inside text area.
        return self.evaluate('(()=>{const r=document.getElementById('+json.dumps(child)+').getBoundingClientRect();return {x:r.x+32,y:r.y+30}})()')

    def right_click(self, child):
        point = self.point(child)
        for action,buttons in [('down',2),('up',0)]:
            result = self.call('browser.input',surface=self.surface,event={
                'type':'pointer','action':action,'button':'right','buttons':buttons,
                'click_count':1,'modifiers':0,**point})
            if result.get('pending'):
                self.wait(lambda:self.call('browser.heartbeat')['input_pending']==0,'native pointer ACK')
        self.wait(lambda:self.has_event('contextmenu-'+child),'actual child contextmenu event')
        return point

    def context(self, point):
        try:
            return self.call('browser.context',surface=self.surface,**point)
        except Fault:
            self.report['failure_lifecycle'] = self.lifecycle(point)
            raise

    def action(self, menu, action):
        return self.call('browser.context_action',target=self.target,token=menu['token'],action=action)

    def copy_child(self, child, divert=False):
        self.focus_root()
        point = self.right_click(child)
        if divert:
            self.wait(lambda:self.has_event('focused-b'),'fixture contextmenu diverted focus to sibling')
        menu = self.context(point)
        self.report['steps'].append({'phase':'hit-document','child':child,'document':menu['document']})
        if not menu['editable']:
            self.report['classification'] = 'HIT_METADATA_NOT_CHILD_EDITOR'
            self.report['failure_lifecycle'] = self.lifecycle(point)
        hit_editable = menu['editable']
        self.action(menu,'select_all')
        menu = self.context(point)  # fresh one-shot token for Copy
        copied = self.action(menu,'copy')
        self.report['observed_native_copy'] = copied
        self.report['focus_diagnostics'] = self.evaluate('({rootActive:document.activeElement?.id})')
        self.assertTrue(hit_editable, 'hit metadata must describe child editor')
        return copied

    def lifecycle(self, point=None):
        result = {}
        for label,method,params,session in [
            ('frames','Page.getFrameTree',{},self.session),
            ('targets','Target.getTargets',{},None),
            *([('hit','DOM.getNodeForLocation',{k:int(v) for k,v in point.items()},self.session)] if point else [])]:
            try:
                result[label] = self.browser.cdp.call(method,params,session)
            except Fault as error:
                result[label] = {'fault':{'kind':error.kind,'message':str(error)}}
        if point and isinstance(result.get('hit'),dict) and result['hit'].get('backendNodeId'):
            try:
                node = self.browser.cdp.call('DOM.describeNode',
                    {'backendNodeId':result['hit']['backendNodeId'],'depth':0},self.session)['node']
                result['hit_node_metadata'] = {k:node[k] for k in
                    ('nodeName','localName','frameId','backendNodeId') if k in node}
            except Fault as error:
                result['hit_node_metadata'] = {'fault':{'kind':error.kind,'message':str(error)}}
        return result

    def private_boundary(self, phase):
        public = self.call('browser.tabs')['tabs']
        infos = self.browser.cdp.call('Target.getTargets')['targetInfos']
        source = next(t for t in infos if t['targetId']==self.target)
        private = [t for t in infos if t['type']=='page' and
                   t.get('browserContextId') != source.get('browserContextId')]
        self.assertEqual(len(private),1,'one separately owned native helper context')
        helper = private[0]['targetId']
        self.assertNotIn(helper,[t['id'] for t in public])
        for method,extra in [
            ('browser.context',{'surface':{'kind':'page','target':helper},'x':1,'y':1}),
            ('browser.input',{'surface':{'kind':'page','target':helper},'event':{'type':'text','text':'denied'}}),
            ('browser.navigate',{'target':helper,'url':'about:blank'}),
            ('browser.tab.close',{'target':helper}),
            ('browser.clipboard',{'target':helper,'operation':'copy'})]:
            with self.assertRaises(Fault):
                self.call(method,**extra)
        self.report['private_boundary_'+phase] = {'helper':helper,'context':private[0].get('browserContextId'),'rejected':True}

    def assert_permissions(self, refresh=False):
        if refresh:
            with self.event_lock:
                old_count = sum(e.get('phase','').startswith('permissions-') for e in self.report['events'])
            self.evaluate("a.contentWindow.postMessage('permissions','*');b.contentWindow.postMessage('permissions','*');true")
            def fresh_receipts():
                with self.event_lock:
                    return sum(e.get('phase','').startswith('permissions-') for e in self.report['events']) >= old_count+2
            self.wait(fresh_receipts,'fresh child permission denial receipts')
        root = self.evaluate('''(async()=>{
 const readState=(await navigator.permissions.query({name:'clipboard-read'})).state;
 const writeState=(await navigator.permissions.query({name:'clipboard-write'})).state;
 let read,write;try{await navigator.clipboard.readText();read='ALLOWED'}catch(e){read=e.name}
 try{await navigator.clipboard.writeText('owned-denial-attempt');write='ALLOWED'}catch(e){write=e.name}
 return {readState,writeState,read,write,legacy:document.execCommand('paste')};})()''')
        self.report['root_permissions'] = root
        self.assertEqual(root,{'readState':'denied','writeState':'denied','read':'NotAllowedError','write':'NotAllowedError','legacy':False})
        with self.event_lock:
            receipts = [e for e in self.report['events'] if e.get('phase','').startswith('permissions-')]
        self.assertGreaterEqual(len(receipts),2)
        for receipt in receipts:
            for key,expected in [('readState','denied'),('writeState','denied'),('read','NotAllowedError'),('write','NotAllowedError')]:
                self.assertEqual(receipt[key],expected,receipt)

    def test_same_origin_two_children_after_real_right_click(self):
        self.report['expected'] = {'a':A_WITNESS,'b':B_WITNESS}
        self.load()
        passed = []
        for child,witness in [('a',A_WITNESS),('b',B_WITNESS)]:
            with self.subTest(child=child):
                copied = self.copy_child(child)
                expected = {'kind':'copied','text':witness}
                passed.append(copied==expected)
                self.assertEqual(copied,expected,'must not copy root decoy or sibling')
        self.report['result'] = 'PASS' if len(passed)==2 and all(passed) else 'FAIL'

    def test_contextmenu_sibling_focus_cannot_redirect_copy(self):
        self.report['expected'] = {'copy':A_WITNESS,'forbidden':[ROOT_WITNESS,B_WITNESS]}
        self.load(divert=True)
        copied = self.copy_child('a',divert=True)
        self.report['classification'] = ('WRONG_FOCUSED_FRAME_EXPORT' if copied.get('text')==B_WITNESS
            else 'SAFE_REJECTED_FUNCTIONAL_ROUTING_UNRESOLVED' if copied.get('kind')=='failed' else 'inspect native result')
        self.assertEqual(copied,{'kind':'copied','text':A_WITNESS},'hit child A must not export focused sibling B')
        self.report['result'] = 'PASS'

    def focus_receipt(self):
        return self.evaluate('({active:document.activeElement.id,root:nativeCommands,a:a.contentWindow.nativeCommands,b:b.contentWindow.nativeCommands})')

    def test_safety_rejects_sibling_focus_without_source_command(self):
        self.load(divert=True)
        point = self.right_click('a')
        self.wait(lambda:self.has_event('focused-b'),'owned sibling focus diversion')
        passed = []
        for action in ('select_all','copy'):
            with self.subTest(action=action):
                menu = self.context(point)
                before = self.focus_receipt()
                try:
                    result = self.action(menu,action)
                except Fault as error:
                    result = {'fault':error.kind}
                after = self.focus_receipt()
                self.report['steps'].append({'safety_action':action,'before':before,'after':after,'result':result})
                passed.append((result.get('kind')=='failed' or result.get('fault')=='stale_context') and 'text' not in result and before==after)
                self.assertTrue(result.get('kind')=='failed' or result.get('fault')=='stale_context',result)
                self.assertNotIn('text',result)
                self.assertEqual(before,after,'rejection must not dispatch source native commands or move focus')
        self.focus_root()
        self.assertEqual(self.call('browser.clipboard',target=self.target,operation='copy'),{'kind':'copied','text':ROOT_WITNESS})
        self.report['result']='PASS' if len(passed)==2 and all(passed) else 'FAIL'
        # A link need not belong to the focused child: only edit actions bind focus.
        link = self.context({'x':32,'y':730})
        self.assertEqual(self.action(link,'copy_link'),{'kind':'link',
            'text':f'http://127.0.0.1:{self.server.server_port}/owned-link'})

    def test_safety_same_frame_hit_and_focused_element_must_match(self):
        self.load()
        self.focus_root()
        self.evaluate('window.divertMenuElement=true;true')
        point = {'x':32,'y':30}
        for action,buttons in [('down',2),('up',0)]:
            self.call('browser.input',surface=self.surface,event={
                'type':'pointer','action':action,'button':'right','buttons':buttons,
                'click_count':1,'modifiers':0,**point})
        self.assertEqual(self.evaluate('document.activeElement.id'),'rootother')
        menu = self.context(point)
        before = self.focus_receipt()
        result = self.action(menu,'copy')
        after = self.focus_receipt()
        self.report['same_frame_focus']={'before':before,'after':after,'result':result}
        self.assertEqual(result.get('kind'),'failed',result)
        self.assertNotIn('text',result)
        self.assertEqual(before,after)
        self.report['result']='PASS'

    def test_safety_ordinary_copy_discards_changed_frame_and_element_focus(self):
        self.load()
        passed = []
        for diversion in ('element','frame'):
            with self.subTest(diversion=diversion):
                self.focus_root()
                self.evaluate('window.divertOnCommand='+json.dumps(diversion)+';true')
                before = self.focus_receipt()
                result = self.call('browser.clipboard',target=self.target,operation='copy')
                after = self.focus_receipt()
                self.evaluate('window.divertOnCommand=null;true')
                self.report['steps'].append({'safety_diversion':diversion,'before':before,'after':after,'result':result})
                passed.append(result.get('kind')=='failed' and 'text' not in result)
                self.assertEqual(result.get('kind'),'failed',result)
                self.assertNotIn('text',result)
        self.focus_root()
        self.assertEqual(self.call('browser.clipboard',target=self.target,operation='copy'),{'kind':'copied','text':ROOT_WITNESS})
        self.report['result']='PASS' if len(passed)==2 and all(passed) else 'FAIL'

    def test_safety_ordinary_focused_child_navigation_sample(self):
        self.load()
        point = self.right_click('a')
        self.action(self.context(point),'select_all')
        self.gate_on_command=True
        self.evaluate("a.contentWindow.postMessage('navigate-gated','*');true")
        self.assertTrue(self.gate_requested.wait(2))
        started=time.monotonic()
        result=self.call('browser.clipboard',target=self.target,operation='copy')
        self.report['ordinary_child_navigation']={'started':started,'returned':time.monotonic(),
            'result':result,'lifecycle_after':self.lifecycle(point),
            'limitation':'one bounded scheduling sample; not an atomic document/focus fence'}
        if result.get('kind')=='copied':
            self.assertEqual(result['text'],A_WITNESS,'never export replacement or sibling data')
        else:
            self.assertEqual(result.get('kind'),'failed',result)
            self.assertNotIn('text',result)
            self.assertNotIn('poisoned',result.get('message',''))
        self.release_gate.set()
        self.report['result']='PASS'

    def test_safety_oopif_owner_is_explicitly_unsupported(self):
        self.load(cross=True)
        point = self.right_click('a')
        with self.assertRaisesRegex(Fault,'unsupported|out.of.process|iframe') as rejected:
            self.context(point)
        self.assertEqual(rejected.exception.kind,'unsupported_capability')
        self.report['result']='PASS'

    def test_cross_site_oopif_after_real_right_click(self):
        self.report['expected'] = {'copy':A_WITNESS,'required':'localhost child is genuinely out-of-process'}
        self.load(cross=True)
        infos = self.report['initial_lifecycle']['targets']['targetInfos']
        oopif = [t for t in infos if t['type']=='iframe' and urlsplit(t['url']).hostname=='localhost']
        self.report['oopif_targets'] = oopif
        self.assertTrue(oopif,'cross-origin alone is not proof of OOPIF coverage')
        try:
            copied = self.copy_child('a')
        except Fault as error:
            self.report['classification'] = ('FRAME_API_UNSUPPORTED_FAIL_CLOSED'
                if error.kind=='unsupported_capability' else 'OOPIF_PARENT_NODE_METADATA_ACTION_REJECTED')
            self.fail(f'required OOPIF acceptance failed: {error.kind}: {error}')
        self.assertEqual(copied,{'kind':'copied','text':A_WITNESS})
        self.report['result'] = 'PASS'

    def test_child_navigation_token_and_execution_gates(self):
        self.report['expected'] = {'stale_token':'reject','timed_copy':'old witness before commit OR fail closed; never replacement/sibling'}
        self.load()
        point = self.right_click('a')
        self.action(self.context(point),'select_all')
        stale = self.context(point)
        self.evaluate("a.contentWindow.postMessage('navigate-gated','*');true")
        self.assertTrue(self.gate_requested.wait(2),'owned HTTP navigation gate reached')
        self.release_gate.set()
        self.wait(lambda:self.has_event('loaded-a-new'),'new child committed')
        try:
            rejected = self.action(stale,'copy')
        except Fault as error:
            rejected = {'fault':error.kind}
        self.assertTrue(rejected.get('kind')=='failed' or rejected.get('fault')=='stale_context',rejected)
        self.assertNotIn('text',rejected)
        # Reload only the child via owned page setup; root loader remains intact.
        self.gate_requested.clear()
        self.release_gate.clear()
        self.evaluate('a.src='+json.dumps(f'http://127.0.0.1:{self.server.server_port}/child?name=a')+';true')
        self.wait(lambda:self.evaluate('a.contentDocument?.querySelector("#editor")!==null && a.contentWindow.location.pathname==="/child"'),'fresh original child')
        point = self.right_click('a')
        self.action(self.context(point),'select_all')
        token = self.context(point)
        self.gate_on_command = True
        self.evaluate("a.contentWindow.postMessage('navigate-gated','*');true")
        self.assertTrue(self.gate_requested.wait(2))
        started = time.monotonic()
        try:
            result = self.action(token,'copy')
        except Fault as error:
            result = {'fault':{'kind':error.kind,'message':str(error)}}
        self.report['navigation_race'] = {'started':started,'returned':time.monotonic(),'result':result,
                                         'lifecycle_after':self.lifecycle(point),
                                         'limitation':'bounded scheduling sample, not an atomic navigation/ClipboardHost fence'}
        if result.get('kind') == 'copied':
            self.assertEqual(result['text'],A_WITNESS,'must never export a different child document/widget')
        else:
            self.assertTrue(result.get('kind') in ('failed','no-update-observed') or
                            result.get('fault',{}).get('kind') in ('stale_context','not_owner','stale_generation'),result)
        self.release_gate.set()
        self.report['result'] = 'PASS'

    def finish(self):
        self.release_gate.set()
        cleanup = {}
        b = self.browser
        if b is not None:
            try:
                if self.helper_ready:
                    self.private_boundary('after')
                    self.assert_permissions(refresh=True)
            except Exception as error:
                cleanup['boundary_or_permission_error'] = str(error)
                self.report['result'] = 'FAIL'
            try:
                self.call('browser.stop',confirmed=True)
                cleanup['normal_stop'] = True
            except Exception as error:
                cleanup['normal_stop_error'] = str(error)
                try:
                    os.killpg(b.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    pid,status = os.waitpid(b.pid,0)
                    b.running=False
                    cleanup.update(fallback_reaped_pid=pid,exit_code=os.waitstatus_to_exitcode(status))
                except ChildProcessError:
                    cleanup['already_reaped'] = True
                b.expiry_stop.set()
                b.cdp.close()
                cleanup['fallback_runtime_removed'] = b.cleanup_runtime()
            cleanup.update(owned_pid=b.pid,owned_pid_reaped=not b.running,runtime_removed=not b.runtime_root.exists())
            cleanup['remaining_owned_pids'] = private_processes(b.runtime_root)
            if not cleanup['owned_pid_reaped'] or not cleanup['runtime_removed'] or cleanup['remaining_owned_pids']:
                self.report['result'] = 'FAIL'
        if self.watchdog:
            self.watchdog.cancel()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.report['cleanup'] = cleanup
        if self.report['result']=='INCOMPLETE':
            self.report['result']='FAIL'
        if b is None or (cleanup['owned_pid_reaped'] and not cleanup['remaining_owned_pids']):
            shutil.rmtree(self.root)
        else:
            self.report['retained_private_profile_root'] = str(self.root)
        (self.evidence/(self.name+'.json')).write_text(json.dumps(self.report,ensure_ascii=False,indent=2))
        self.assertNotIn('boundary_or_permission_error',cleanup,cleanup)
        if b is not None:
            self.assertTrue(cleanup['owned_pid_reaped'],cleanup)
            self.assertTrue(cleanup['runtime_removed'],cleanup)
            self.assertEqual(cleanup['remaining_owned_pids'],[],cleanup)


if __name__ == '__main__':
    unittest.main()

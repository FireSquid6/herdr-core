"""Public worker context-menu contract; native tests require explicit opt-in."""
import os
import threading
import time
import unittest
from unittest.mock import patch
from browser_worker import Browser, Fault, Lease, Worker


class ContextProtocol(unittest.TestCase):
    def setUp(self):
        self.worker = Worker('/unused-owned-root', '/unused-chrome')
        b = self.browser = Browser.__new__(Browser)
        b.running = True
        b.instance = 'instance'
        b.cdp = type('NoIO', (), {'closed': False})()
        b.lease = Lease()
        b.state_lock = threading.RLock()
        b.local = threading.local()
        b.tabs = lambda: {'tabs':[{'id':'source'}]}
        self.worker.browsers['owned'] = b
        self.p = {'workspace':'owned', 'generation':b.lease.acquire('viewer',False,False)}

    def test_invalid_actions_tokens_and_private_targets_cannot_start_native_io(self):
        for extra in [{'action':'arbitrary','token':'unknown','target':'source'},
                      {'action':'copy_link','token':'unknown','target':'source'},
                      {'action':'paste','token':'unknown','target':'source','text':None},
                      {'action':'paste','token':'unknown','target':'source','text':'x'*65537},
                      {'action':'copy','token':'unknown','target':'private-helper'},
                      {'action':'copy','token':'unknown','target':'source','generation':'stale'}]:
            with self.subTest(action=extra['action']), self.assertRaises(Fault):
                self.worker.dispatch('viewer','browser.context_action',dict(self.p,**extra))
        for x in [float('nan'), -1, 4097, True]:
            with self.assertRaises(Fault):
                self.worker.dispatch('viewer','browser.context',dict(self.p,surface={'kind':'page','target':'source'},x=x,y=1))
        with self.assertRaises(Fault):
            self.worker.dispatch('viewer','browser.context',dict(self.p,surface={'kind':'page','target':'private-helper'},x=1,y=1))

    def metadata_backend(self):
        b = self.browser
        b.sizes, b.sessions = {}, {'source':'session'}
        self.calls, self.native, self.loader = [], [], 'child-document'
        def call(method, params=None, session=None):
            self.calls.append(method)
            if method == 'Page.getFrameTree':
                return {'frameTree':{'frame':{'id':'main','loaderId':'main-document'},
                    'childFrames':[{'frame':{'id':'child','loaderId':self.loader}}]}}
            if method == 'DOM.getNodeForLocation':
                return {'frameId':'child','backendNodeId':7}
            if method == 'Page.createIsolatedWorld':
                self.assertNotIn('grantUniversalAccess', params)
                self.assertNotIn('grantUniveralAccess', params)
                return {'executionContextId':3 if params['frameId']=='main' else 4}
            if method == 'Runtime.evaluate':
                return {'result':{'objectId':'focus-root' if params['contextId']==3 else 'focus-child'}}
            if method == 'DOM.describeNode':
                if params.get('objectId')=='focus-root':
                    return {'node':{'nodeName':'IFRAME','frameId':'child','backendNodeId':2}}
                return {'node':{'nodeName':'DIV','backendNodeId':7}}
            if method == 'DOM.resolveNode':
                self.assertEqual(params['executionContextId'],4)
                return {'object':{'objectId':'isolated-node'}}
            if method == 'Runtime.callFunctionOn':
                return {'result':{'value':{'_focus_matches_hit':True,'link':'http://owned.invalid/link','editable':False,
                    'password':False,'read_only':False,'disabled':False,'selection_present':False,
                    'can_copy':False,'can_cut':False,'can_paste':False}}}
            if method in ('Runtime.releaseObject','Emulation.setDeviceMetricsOverride','Page.navigate'):
                return {}
            if method == 'Target.getTargetInfo':
                return {'targetInfo':{'targetId':'source','browserContextId':'owned-profile'}}
            if method == 'Target.createTarget':
                self.assertEqual(params,{'url':'http://owned.invalid/link','background':True,'browserContextId':'owned-profile'})
                return {'targetId':'new-target'}
            if method == 'Input.dispatchKeyEvent':
                self.native.append(params)
                return {}
            self.fail('unexpected native side effect: '+method)
        b.cdp.call = call
        b.input_command = lambda *args: {'accepted':True}

    def issue(self):
        return self.worker.dispatch('viewer','browser.context',dict(self.p,
            surface={'kind':'page','target':'source'},x=1,y=1))

    def action(self, menu, **extra):
        params=dict(self.p,target='source',token=menu['token'],action='copy_link')
        params.update(extra)
        return self.worker.dispatch('viewer','browser.context_action',params)

    def test_legacy_new_tab_keeps_its_exact_acknowledgement_shape(self):
        self.metadata_backend()
        reply=self.action(self.issue(),action='new_tab')
        self.assertEqual(reply,{'kind':'action-dispatched','action':'new_tab','target':'new-target','opener_preserved':False})

    def test_one_shot_foreign_wrong_target_expired_and_wrong_action_have_no_side_effects(self):
        self.metadata_backend()
        menu = self.issue()
        for identity, extra in [('foreign',{}),('viewer',{'target':'other'}),('viewer',{'action':'bogus'})]:
            before = len(self.calls)
            params = dict(self.p,target='source',token=menu['token'],action='copy_link')
            params.update(extra)
            with self.assertRaises(Fault):
                self.worker.dispatch(identity,'browser.context_action',params)
            self.assertEqual(len(self.calls),before)
        self.assertEqual(self.action(menu),{'kind':'link','text':'http://owned.invalid/link'})
        before = len(self.calls)
        with self.assertRaises(Fault):
            self.action(menu)
        self.assertEqual(len(self.calls),before)
        menu = self.issue()
        before = len(self.calls)
        with patch('browser_context.time.monotonic', return_value=time.monotonic()+11):
            with self.assertRaises(Fault):
                self.action(menu)
        self.assertEqual(len(self.calls),before)

    def test_child_document_change_and_capacity_discard_capabilities(self):
        self.metadata_backend()
        menu = self.issue()
        self.loader = 'new-child-document'
        with self.assertRaisesRegex(Fault,'frame document changed'):
            self.action(menu)
        self.assertEqual(self.native,[])
        menus = [self.issue() for _ in range(17)]
        before = len(self.calls)
        with self.assertRaises(Fault):
            self.action(menus[0])
        self.assertEqual(len(self.calls),before)
        self.assertEqual(self.action(menus[-1])['kind'],'link')

    def test_mouseup_move_keyup_preserve_menu_but_meaningful_input_invalidates(self):
        self.metadata_backend()
        for event, stale in [
            ({'type':'pointer','action':'up','button':'right','x':1,'y':1},False),
            ({'type':'pointer','action':'move','button':'none','x':1,'y':1},False),
            ({'type':'key','action':'up','key':'F24'},False),
            ({'type':'pointer','action':'down','button':'right','x':1,'y':1},True),
            ({'type':'key','action':'down','key':'F24'},True),
            ({'type':'wheel','x':1,'y':1,'dx':0,'dy':1},True)]:
            menu = self.issue()
            self.worker.dispatch('viewer','browser.input',dict(self.p,
                surface={'kind':'page','target':'source'},event=event))
            if stale:
                before = len(self.calls)
                with self.assertRaises(Fault):
                    self.action(menu)
                self.assertEqual(len(self.calls),before)
            else:
                self.assertEqual(self.action(menu)['kind'],'link')

    def test_controller_replacement_rejects_old_menu_without_native_io(self):
        self.metadata_backend()
        b = self.browser
        b.tools, b.sequences, b.input_receipts, b.input_errors = {}, {}, {}, []
        menu = self.issue()
        fresh = self.worker.dispatch('replacement','browser.acquire',
            {'workspace':'owned','force':True,'confirmed':True})
        before = len(self.calls)
        with self.assertRaises(Fault):
            self.worker.dispatch('replacement','browser.context_action',dict(self.p,**fresh,
                target='source',token=menu['token'],action='copy_link'))
        self.assertEqual(len(self.calls),before)

    def test_untyped_or_unbounded_metadata_is_not_published(self):
        self.metadata_backend()
        original = self.browser.cdp.call
        for invalid in [{'can_copy':1},{'link':'x'*16385},{'value':'not-metadata'}]:
            def malformed(method, params=None, session=None):
                result = original(method,params,session)
                if method == 'Runtime.callFunctionOn':
                    result['result']['value'].update(invalid)
                return result
            self.browser.cdp.call = malformed
            with self.assertRaises(Fault):
                self.issue()
        self.assertEqual(self.native,[])

    def test_resize_navigation_and_selection_invalidation(self):
        self.metadata_backend()
        b = self.browser
        b.sizes['source'] = (100,100,1)
        menu = self.issue()
        self.worker.dispatch('viewer','browser.viewport',dict(self.p,
            surface={'kind':'page','target':'source'},width=100,height=100,scale=1))
        self.assertEqual(self.action(menu)['kind'],'link')
        for method, extra in [('browser.viewport',{'surface':{'kind':'page','target':'source'},'width':101,'height':100}),
                              ('browser.navigate',{'target':'source','url':'http://owned.invalid/new'}),
                              ('browser.tab.select',{'target':'source'})]:
            menu = self.issue()
            self.worker.dispatch('viewer',method,dict(self.p,**extra))
            with self.assertRaises(Fault):
                self.action(menu)


@unittest.skipUnless(os.environ.get('HC_BROWSER_CONTEXT_NATIVE_TEST')=='1','explicit isolated native opt-in')
class NativeContext(unittest.TestCase):
    def test_readonly_isolated_metadata_copy_link_and_native_copy(self):
        import http.server
        import json
        from pathlib import Path
        import tempfile
        from browser_probe import sandbox_enabled
        self.assertNotEqual(os.geteuid(),0)
        with tempfile.TemporaryDirectory(prefix='hc-context-',dir='/var/tmp') as directory:
            worker = Worker(Path(directory),'/opt/google/chrome/chrome')
            p = {'workspace':'owned'}
            worker.dispatch('viewer','browser.ensure',p)
            b = worker.browsers['owned']
            server = None
            sandbox = False
            try:
                p.update(worker.dispatch('viewer','browser.acquire',p))
                target = worker.dispatch('viewer','browser.tabs',p)['selected']
                session = b.session(target)
                def evaluate(expression):
                    reply = b.cdp.call('Runtime.evaluate',{'expression':expression,'returnByValue':True},session)
                    self.assertNotIn('exceptionDetails',reply)
                    return reply.get('result',{}).get('value')
                b.cdp.call('Page.navigate',{'url':'chrome://sandbox'},session)
                deadline = time.monotonic()+4
                while time.monotonic()<deadline:
                    if sandbox_enabled(evaluate('document.body?.innerText') or ''):
                        sandbox=True
                        break
                    time.sleep(.03)
                self.assertTrue(sandbox)
                class Fixture(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header('Content-Type','text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write('''<!doctype html><style>input,textarea,a,div{display:block;margin:15px}</style>
<a id=link href='/destination'>Owned link</a><textarea id=editor></textarea>
<input id=readonly readonly value=readonly><input id=password type=password value=secret>
<input id=disabled disabled value=disabled><div id=plain>Plain</div>'''.encode())
                    def log_message(self,*args):
                        pass
                server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture)
                threading.Thread(target=server.serve_forever,daemon=True).start()
                url = 'http://127.0.0.1:'+str(server.server_port)+'/'
                b.cdp.call('Page.navigate',{'url':url},session)
                deadline = time.monotonic()+4
                while not evaluate('!!document.querySelector("#editor")'):
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.03)
                evaluate("Object.defineProperty(HTMLAnchorElement.prototype,'href',{get(){return 'https://spoof.invalid/'}});true")
                def menu(element):
                    point = evaluate('(()=>{const r=document.querySelector('+json.dumps('#'+element)+').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()')
                    return worker.dispatch('viewer','browser.context',dict(p,surface={'kind':'page','target':target},**point))
                link = menu('link')
                self.assertEqual(link['link'],url+'destination')
                result = worker.dispatch('viewer','browser.context_action',dict(p,target=target,token=link['token'],action='copy_link'))
                self.assertEqual(result,{'kind':'link','text':url+'destination'})
                with self.assertRaises(Fault):
                    worker.dispatch('viewer','browser.context_action',dict(p,target=target,token=link['token'],action='copy_link'))
                self.assertFalse(menu('plain')['editable'])
                readonly = menu('readonly')
                self.assertTrue(readonly['read_only'])
                self.assertFalse(readonly['can_cut'])
                self.assertFalse(readonly['can_paste'])
                password = menu('password')
                self.assertTrue(password['password'])
                self.assertFalse(password['can_copy'])
                self.assertFalse(password['can_cut'])
                self.assertFalse(menu('disabled')['can_paste'])
                evaluate("editor.value='Native context π\\n第二行';editor.focus();editor.setSelectionRange(0,0)")
                selection_menu = menu('editor')
                self.assertEqual(worker.dispatch('viewer','browser.context_action',dict(p,target=target,
                    token=selection_menu['token'],action='select_all'))['kind'],'action-dispatched')
                self.assertTrue(evaluate('editor.selectionStart!==editor.selectionEnd'))
                selected = menu('editor')
                self.assertTrue(selected['selection_present'])
                self.assertTrue(selected['can_copy'])
                self.assertNotIn('text',selected)
                self.assertNotIn('_focus_matches_hit',selected)
                copied = worker.dispatch('viewer','browser.context_action',dict(p,target=target,token=selected['token'],action='copy'))
                self.assertEqual(copied,{'kind':'copied','text':'Native context π\n第二行'})
                with self.assertRaises(Fault):
                    worker.dispatch('viewer','browser.context',dict(p,
                        surface={'kind':'page','target':b.edit.target},x=1,y=1))
                # Invalidate through a public action after real helper seed ACK,
                # before source Copy. The guard must fail healthy, not poison.
                selected = menu('editor')
                original = b.cdp.call
                source_copies = []
                def invalidate_seed(method, params=None, session=None, **kwargs):
                    reply = original(method,params,session,**kwargs)
                    if method == 'Input.dispatchKeyEvent' and (params or {}).get('commands') == ['Copy']:
                        if session == b.edit.session:
                            worker.dispatch('viewer','browser.tab.select',dict(p,target=target))
                        elif session == b.sessions[target]:
                            source_copies.append(True)
                    return reply
                b.cdp.call = invalidate_seed
                try:
                    stale = worker.dispatch('viewer','browser.context_action',dict(p,target=target,token=selected['token'],action='copy'))
                finally:
                    b.cdp.call = original
                self.assertEqual(stale['kind'],'failed')
                self.assertNotIn('text',stale)
                self.assertFalse(b.edit.poisoned)
                self.assertEqual(source_copies,[])
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                worker.dispatch('viewer','browser.stop',dict(p,confirmed=True))
                self.assertFalse(b.runtime_root.exists())
                if os.environ.get('HC_BROWSER_CONTEXT_EVIDENCE'):
                    Path(os.environ['HC_BROWSER_CONTEXT_EVIDENCE']).write_text(json.dumps({
                        'pid':b.pid,'version':b.version,'sandbox':sandbox,'owned_pid_reaped':not b.running,
                        'runtime_removed_after_descendant_check':not b.runtime_root.exists(),
                        'scope':'owned top-level metadata/native editing, not OS/GPUI or child-frame acceptance'},indent=2))


if __name__ == '__main__':
    unittest.main()

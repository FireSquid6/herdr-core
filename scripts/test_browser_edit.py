"""Private browser protocol tests; no core, desktop or Chrome by default."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from browser_worker import Browser, Fault, Lease, Worker


class ClipboardProtocol(unittest.TestCase):
    def setUp(self):
        self.worker = Worker('/unused-owned-test-root', '/unused-chrome')
        self.browser = Browser.__new__(Browser)
        b = self.browser
        b.running = True
        b.cdp = type('NoNativeIO', (), {'closed': False})()
        b.lease = Lease()
        b.state_lock = threading.RLock()
        b.local = threading.local()
        b.instance = 'owned-instance'
        b.tabs = lambda: {'tabs': [{'id': 'source'}]}
        self.worker.browsers['owned'] = b
        self.params = {'workspace': 'owned', 'generation': b.lease.acquire('viewer', False, False),
                       'target': 'source', 'operation': 'copy'}

    def test_invalid_requests_and_stale_lease_cannot_start_native_io(self):
        for update in [{'operation': 'bogus'}, {'operation': 'paste'},
                       {'operation': 'paste', 'text': None},
                       {'operation': 'paste', 'text': 'π'*32769},
                       {'operation': 'copy', 'text': 'unexpected'},
                       {'target': 'private-helper'}, {'generation': 'stale'}]:
            with self.subTest(update=update), self.assertRaises(Fault):
                self.worker.dispatch('viewer', 'browser.clipboard', dict(self.params, **update))
        self.assertFalse(hasattr(self.browser, 'edit'))

    def test_transport_failure_poison_rejects_later_edits_without_native_io(self):
        calls = []
        def call(method, params=None, session=None):
            calls.append(method)
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'main', 'loaderId': 'document'}}}
            raise Fault('browser_failed', 'Chromium pipe closed; owned simulated transport failure')
        self.browser.sessions = {'source': 'source-session'}
        self.browser.cdp.call = call
        first = self.worker.dispatch('viewer', 'browser.clipboard', self.params)
        self.assertEqual(first['kind'], 'failed')
        self.assertIn('poisoned', first['message'])
        count = len(calls)
        second = self.worker.dispatch('viewer', 'browser.clipboard', self.params)
        self.assertEqual(second['kind'], 'failed')
        self.assertEqual(len(calls), count)
        self.assertNotIn('Input.dispatchKeyEvent', calls)

    def test_unsupported_native_focus_is_healthy_and_cannot_start_helper(self):
        calls = []
        def call(method, params=None, session=None):
            calls.append(method)
            if method == 'Page.getFrameTree':
                return {'frameTree':{'frame':{'id':'main','loaderId':'document'}}}
            raise Fault('browser_failed', 'owned unsupported native frame inspection')
        self.browser.sessions = {'source':'session'}
        self.browser.cdp.call = call
        for _ in range(2):
            before = len(calls)
            result = self.worker.dispatch('viewer','browser.clipboard',self.params)
            self.assertEqual(result['kind'],'failed')
            self.assertNotIn('text',result)
            self.assertIn('No source edit command',result['message'])
            self.assertNotIn('poisoned',result['message'])
            self.assertGreater(len(calls),before,'known unsupported focus must not poison future inspection')
        self.assertNotIn('Target.createBrowserContext',calls)
        self.assertNotIn('Input.dispatchKeyEvent',calls)

    def test_lost_helper_creation_ack_cannot_publish_its_private_context(self):
        b = self.browser
        b.sessions, b.tools, b.internal_targets = {'source': 'session'}, {}, set()
        b.selected, b.revision = 'source', 0
        b.tabs = Browser.tabs.__get__(b, Browser)
        created = False
        def call(method, params=None, session=None):
            nonlocal created
            if method == 'Target.getTargets':
                targets = [{'targetId':'source', 'type':'page', 'url':'about:blank', 'title':'source'}]
                if created:
                    targets.append({'targetId':'unacknowledged-helper', 'type':'page',
                                    'url':'about:blank', 'title':'', 'browserContextId':'private-context'})
                return {'targetInfos':targets}
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id':'main', 'loaderId':'document'}}}
            if method == 'Page.createIsolatedWorld':
                return {'executionContextId':3}
            if method == 'Runtime.evaluate':
                return {'result':{'objectId':'native-focus'}}
            if method == 'DOM.describeNode':
                return {'node':{'nodeName':'TEXTAREA','backendNodeId':7}}
            if method == 'Runtime.releaseObject':
                return {}
            if method == 'Target.createBrowserContext':
                return {'browserContextId':'private-context'}
            if method == 'Target.createTarget':
                created = True
                raise Fault('browser_failed', 'owned simulated lost create ACK')
            self.fail('unexpected native action: '+method)
        b.cdp.call = call
        self.assertEqual(self.worker.dispatch('viewer', 'browser.clipboard', self.params)['kind'], 'failed')
        self.assertTrue(created, 'exercise actual lost helper creation ACK, not an earlier focus failure')
        tabs = self.worker.dispatch('viewer', 'browser.tabs', self.params)
        self.assertEqual([t['id'] for t in tabs['tabs']], ['source'])
        with self.assertRaises(Fault):
            self.worker.dispatch('viewer', 'browser.tab.close', dict(self.params, target='unacknowledged-helper'))


@unittest.skipUnless(os.environ.get('HC_BROWSER_EDIT_NATIVE_TEST') == '1', 'explicit isolated native opt-in')
class NativeClipboard(unittest.TestCase):
    def test_public_copy_paste_and_private_target_boundary(self):
        # No hc invocation. The worker creates private HOME/runtime/profile and
        # starts only its own sandboxed Chrome; source reads are fixture oracles.
        from browser_probe import sandbox_enabled
        import http.server
        import time
        if os.geteuid() == 0:
            self.fail('normal user required')
        with tempfile.TemporaryDirectory(prefix='hc-edit-test-', dir='/var/tmp') as directory:
            worker = Worker(Path(directory), '/opt/google/chrome/chrome')
            p = {'workspace': 'owned'}
            worker.dispatch('viewer', 'browser.ensure', p)
            b = worker.browsers['owned']
            owner = 'viewer'
            server = None
            sandbox_proved = False
            try:
                p.update(worker.dispatch('viewer', 'browser.acquire', p))
                target = worker.dispatch('viewer', 'browser.tabs', p)['selected']
                session = b.session(target)
                def evaluate(expression):
                    reply = b.cdp.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session)
                    self.assertNotIn('exceptionDetails', reply)
                    return reply.get('result', {}).get('value')
                b.cdp.call('Page.navigate', {'url': 'chrome://sandbox'}, session)
                deadline = time.monotonic()+4
                while time.monotonic()<deadline:
                    if sandbox_enabled(evaluate('document.body?.innerText') or ''):
                        break
                    time.sleep(.03)
                else:
                    self.fail('sandbox not proved')
                sandbox_proved = True
                class Fixture(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write(b'<!doctype html><textarea id=e></textarea>')
                    def log_message(self, *args):
                        pass
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                url = 'http://127.0.0.1:' + str(server.server_port) + '/'
                b.cdp.call('Page.navigate', {'url': url}, session)
                deadline = time.monotonic()+4
                while not evaluate('location.href === '+repr(url)+' && !!document.querySelector("#e")'):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.03)
                def denied():
                    self.assertEqual(evaluate('''(async()=>{
 const state=(await navigator.permissions.query({name:'clipboard-read'})).state;
 let read;try{await navigator.clipboard.readText();read='allowed'}catch(e){read=e.name}
 return {state,read,legacy:document.execCommand('paste')};})()'''),
                                     {'state': 'denied', 'read': 'NotAllowedError', 'legacy': False})
                denied()
                evaluate("document.body.innerHTML='<textarea id=e></textarea>';window.events=[];document.addEventListener('paste',x=>events.push(x.isTrusted));e.value='Owned π\\n第二行';e.focus();e.select()")
                request = dict(p, target=target)
                copied = worker.dispatch('viewer', 'browser.clipboard', dict(request, operation='copy'))
                self.assertEqual(copied, {'kind': 'copied', 'text': 'Owned π\n第二行'})
                hidden = b.edit.target
                self.assertNotIn(hidden, [t['id'] for t in worker.dispatch('viewer', 'browser.tabs', p)['tabs']])
                for method, extra in [('browser.navigate', {'target': hidden, 'url': 'about:blank'}),
                                      ('browser.tab.close', {'target': hidden}),
                                      ('browser.input', {'surface': {'kind': 'page', 'target': hidden}, 'event': {}})]:
                    with self.assertRaises(Fault):
                        worker.dispatch('viewer', method, dict(p, **extra))
                evaluate("e.value='';e.focus()")
                result = worker.dispatch('viewer', 'browser.clipboard', dict(request, operation='paste', text='Fresh 🦀\n'))
                self.assertEqual(result['kind'], 'paste-dispatched')
                self.assertEqual(evaluate('e.value'), 'Fresh 🦀\n')
                self.assertEqual(evaluate('events'), [True])
                denied()
                # Canceled native keydown yields an honest bounded no-update;
                # it must leave the healthy mechanism reusable.
                evaluate("window.cancelCopy=x=>{if(x.key==='F24')x.preventDefault()};document.addEventListener('keydown',cancelCopy);e.select()")
                canceled = worker.dispatch('viewer', 'browser.clipboard', dict(request, operation='copy'))
                self.assertEqual(canceled, {'kind': 'no-update-observed'})
                evaluate("document.removeEventListener('keydown',cancelCopy);e.value='cut π';e.focus();e.select()")
                cut = worker.dispatch('viewer', 'browser.clipboard', dict(request, operation='cut'))
                self.assertEqual(cut, {'kind': 'copied', 'text': 'cut π'})
                self.assertEqual(evaluate('e.value'), '')
                empty = worker.dispatch('viewer', 'browser.clipboard', dict(request, operation='paste', text=''))
                self.assertEqual(empty, {'kind': 'paste-dispatched'})
                self.assertFalse(b.edit.poisoned)
                # Force takeover through the public dispatcher after a real Cut
                # ACK: no text may escape and Cut must never be retried.
                evaluate("e.value='ownership-race';e.focus();e.select()")
                original = b.cdp.call
                cut_dispatches = []
                def takeover(method, params=None, session=None, **kwargs):
                    nonlocal owner
                    reply = original(method, params, session, **kwargs)
                    if method == 'Input.dispatchKeyEvent' and session == b.sessions[target] and (params or {}).get('commands') == ['Cut']:
                        cut_dispatches.append(True)
                        owner = 'replacement'
                        p.update(worker.dispatch(owner, 'browser.acquire', {'workspace':'owned', 'force':True, 'confirmed':True}))
                    return reply
                b.cdp.call = takeover
                with self.assertRaisesRegex(Fault, 'do not automatically retry or undo'):
                    worker.dispatch('viewer', 'browser.clipboard', dict(request, operation='cut'))
                b.cdp.call = original
                self.assertEqual(len(cut_dispatches), 1)
                self.assertEqual(evaluate('e.value'), '')
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                worker.dispatch(owner, 'browser.stop', dict(p, confirmed=True))
                self.assertFalse(b.runtime_root.exists(), 'owned Chrome runtime cleanup failed')
                evidence = os.environ.get('HC_BROWSER_EDIT_EVIDENCE')
                if evidence:
                    Path(evidence).write_text(json.dumps({
                        'scope': 'owned worker native editing, not OS/GPUI acceptance',
                        'pid': b.pid, 'version': b.version, 'sandbox': 'PASS' if sandbox_proved else 'NOT PROVED',
                        'owned_pid_reaped': not b.running,
                        'private_runtime_removed_after_descendant_check': not b.runtime_root.exists(),
                        'core_started': False, 'clipboard_permission_grants': False,
                    }, indent=2))


if __name__ == '__main__':
    unittest.main()

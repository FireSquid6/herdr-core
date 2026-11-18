"""Owned custom-shadow focus safety, not shadow routing or OS acceptance.

Opt in with BOTH HC_BROWSER_SHADOW_FOCUS_NATIVE_TEST=1 and the existing
HC_BROWSER_CONTEXT_FRAMES_NATIVE_TEST=1/scratch evidence envelope. The reused
fixture owns Chrome, sandbox verification, private helper denial and cleanup.
"""
import json
import os
import unittest

import test_browser_context_frames as frames
from browser_focus import FocusUnavailable, native_focus
from browser_worker import Fault


@unittest.skipUnless(os.environ.get('HC_BROWSER_SHADOW_FOCUS_NATIVE_TEST')=='1',
                     'explicit isolated native shadow-focus opt-in')
class NativeShadowFocus(frames.NativeContextFrames):
    def shadow_setup(self, mode):
        self.load()
        # Fixture-only DOM writes, never a production transport. Keeping the
        # closed-root reference is normal creator behavior; shadowRoot is null.
        old_events = sum(e.get('phase') in ('loaded-a','loaded-b') for e in self.report['events'])
        self.evaluate('''(()=>{
 window.a=document.getElementById('a');window.b=document.getElementById('b');
 const host=document.createElement('div');host.id='shadowhost';
 host.style='position:absolute;left:20px;top:160px;width:400px;height:400px';
 document.body.append(host);window.fixtureShadow=host.attachShadow({mode:MODE});
 const style=document.createElement('style');style.textContent='iframe{position:absolute;left:0;width:360px;height:180px;border:0}#a{top:0}#b{top:220px}';
 fixtureShadow.append(style,a,b);return true;
 })()'''.replace('MODE',json.dumps(mode)))
        def fresh_frames():
            with self.event_lock:
                return sum(e.get('phase') in ('loaded-a','loaded-b') for e in self.report['events']) >= old_events+2
        self.wait(fresh_frames,'shadow iframe browsing contexts reloaded')
        self.wait(lambda:self.evaluate('!!a.contentDocument?.querySelector("#editor") && !!b.contentDocument?.querySelector("#editor")'),
                  'owned shadow children ready')
        self.report['shadow_mode'] = mode
        self.report['closed_getter_is_null'] = self.evaluate('shadowhost.shadowRoot===null')

    def point(self, child):
        return self.evaluate('(()=>{const r=window['+json.dumps(child)+'].getBoundingClientRect();return {x:r.x+32,y:r.y+30}})()')

    def describe_focus(self):
        # Native metadata diagnostic, never a selected-text/value oracle.
        world=self.browser.cdp.call('Page.createIsolatedWorld',{
            'frameId':self.browser.cdp.call('Page.getFrameTree',session=self.session)['frameTree']['frame']['id'],
            'worldName':'herdr-shadow-fixture-diagnostic'},self.session)
        obj=self.browser.cdp.call('Runtime.evaluate',{'expression':'document.activeElement',
            'contextId':world['executionContextId']},self.session)['result']['objectId']
        try:
            receipts={}
            for depth in (0,1):
                node=self.browser.cdp.call('DOM.describeNode',{'objectId':obj,'depth':depth,'pierce':True},self.session)['node']
                receipts[str(depth)]={k:node[k] for k in ('nodeName','backendNodeId','frameId') if k in node}
                receipts[str(depth)]['shadowRoots']=[{k:r[k] for k in ('nodeName','backendNodeId','shadowRootType') if k in r}
                                                    for r in node.get('shadowRoots',[])]
            return receipts
        finally:
            self.browser.cdp.call('Runtime.releaseObject',{'objectId':obj},self.session)

    def commands(self):
        return self.evaluate('({root:nativeCommands,a:a.contentWindow.nativeCommands,b:b.contentWindow.nativeCommands,active:document.activeElement.id})')

    def test_shadow_open_rejects_ordinary_copy(self):
        self.check_shadow('open')

    def test_shadow_closed_rejects_ordinary_copy(self):
        self.check_shadow('closed')

    def check_shadow(self, mode):
        self.shadow_setup(mode)
        stamps=[]
        descriptors=[]
        # Diagnose the old identity collision, but acceptance uses public API.
        for name in ('a','b'):
            self.evaluate(name+'.contentWindow.editor.focus();'+name+'.contentWindow.editor.select();true')
            descriptors.append(self.describe_focus())
            try:
                stamps.append({'stamp':native_focus(self.browser,self.session)._asdict()})
            except FocusUnavailable as error:
                stamps.append({'unsupported':str(error)})
        self.report['native_descriptors']=descriptors
        self.report['focus_diagnostics']=stamps
        self.report['initial_stamp_collision'] = (stamps[0].get('stamp') is not None and stamps[0]==stamps[1])
        for descriptor in descriptors:
            self.assertIn(mode,[r.get('shadowRootType') for r in descriptor['0']['shadowRoots']],
                          'depth-0 native descriptor must expose actual custom shadow root')
        if mode=='closed':
            self.assertTrue(self.report['closed_getter_is_null'])
        # Native right-button input, with a fixture contextmenu callback moving
        # A's retained selection/focus to B under the same opaque custom host.
        self.evaluate("a.contentWindow.document.addEventListener('contextmenu',e=>{e.preventDefault();a.contentWindow.editor.select();b.contentWindow.editor.focus();b.contentWindow.editor.select()});true")
        point=self.right_click('a')
        self.assertEqual(self.evaluate('document.activeElement.id'),'shadowhost')
        before=self.commands()
        result=self.call('browser.clipboard',target=self.target,operation='copy')
        after=self.commands()
        self.report['ordinary_copy']={'before':before,'after':after,'result':result,
                                     'forbidden_witness':frames.B_WITNESS}
        # Keep observing both paths even when the old implementation is unsafe.
        try:
            menu=self.context(point)
            context_result=self.action(menu,'copy')
        except Fault as error:
            context_result={'fault':error.kind,'message':str(error)}
        self.report['context_copy']=context_result
        self.assertEqual(result.get('kind'),'failed',result)
        self.assertNotIn('text',result)
        self.assertIn('shadow',result.get('message','').lower())
        self.assertNotIn('poisoned',result.get('message',''))
        self.assertEqual(before,after,'opaque custom shadow focus must fail before source Copy')
        self.assertTrue(context_result.get('kind')=='failed' or
                        context_result.get('fault') in ('unsupported_capability','stale_context'),context_result)
        self.assertNotIn('text',context_result)
        # Ordinary user-agent shadow roots must remain usable after rejection.
        self.focus_root()
        self.report['ordinary_textarea_descriptor']=self.describe_focus()
        self.assertEqual(self.call('browser.clipboard',target=self.target,operation='copy'),
                         {'kind':'copied','text':frames.ROOT_WITNESS})
        self.evaluate("window.divertOnCommand='frame';true")
        self.focus_root()
        before=self.commands()
        shifted=self.call('browser.clipboard',target=self.target,operation='copy')
        after=self.commands()
        self.report['post_command_shadow_focus']={'before':before,'after':after,'result':shifted}
        self.evaluate('window.divertOnCommand=null;true')
        self.assertEqual(shifted.get('kind'),'failed',shifted)
        self.assertNotIn('text',shifted)
        self.assertIn('Native editing may already have occurred',shifted.get('message',''))
        self.focus_root()
        self.assertEqual(self.call('browser.clipboard',target=self.target,operation='copy'),
                         {'kind':'copied','text':frames.ROOT_WITNESS})
        self.report['result']='PASS'


def load_tests(loader, tests, pattern):
    # Reuse lifecycle helpers, not inherited frame acceptance test cases.
    return unittest.TestSuite(NativeShadowFocus(name) for name in
        ('test_shadow_open_rejects_ordinary_copy','test_shadow_closed_rejects_ordinary_copy'))


if __name__=='__main__':
    unittest.main()

"""Owned headless input diagnostic; no OS clipboard, core service or GUI.

Requires HC_BROWSER_INPUT_AFTER_EDIT_NATIVE_TEST=1 plus the existing frame-test
opt-in, scratch HC_SESSION/HOME/XDG and HC_BROWSER_CONTEXT_FRAMES_EVIDENCE.
Source CDP JS only sets up an owned fixture/reads event counters, never supplies
clipboard result text. Public Worker.dispatch drives input, edit and context.
"""
import os
import time
import unittest

import test_browser_context_frames as frames
from browser_focus import FocusUnavailable, native_focus
from browser_worker import Fault


@unittest.skipUnless(os.environ.get('HC_BROWSER_INPUT_AFTER_EDIT_NATIVE_TEST')=='1',
                     'explicit isolated native input-after-edit opt-in')
class NativeInputAfterEdit(frames.NativeContextFrames):
    def snapshot(self, label):
        # Readonly native target/frame/focus diagnostics, not production hooks.
        try:
            focus={'stamp':native_focus(self.browser,self.session)._asdict()}
        except FocusUnavailable as error:
            focus={'unsupported':str(error)}
        value={'label':label,'at':time.monotonic(),'events':self.events(),
               'focus':focus,'lifecycle':self.lifecycle(),
               'page':self.evaluate('({active:document.activeElement.id,hasFocus:document.hasFocus(),visibility:document.visibilityState})')}
        self.report.setdefault('input_snapshots',[]).append(value)
        return value

    def events(self):
        return self.evaluate('({mouse:window.inputMouse,context:window.inputContext,keys:window.inputKeys,journal:window.inputJournal})')

    def input_ack(self,event):
        result=self.call('browser.input',surface=self.surface,event=event)
        self.assertIs(result.get('accepted'),True,result)
        if result.get('pending'):
            receipt=result['receipt']
            def drained():
                status=self.call('browser.heartbeat')
                self.assertFalse(status['input_errors'],status)
                return status['input_pending']==0
            self.wait(drained,'actual native input ACK / receipt drain')
            self.report['steps'].append({'receipt_drained':receipt})
        return result

    def click(self,button='left'):
        for action,buttons in [('down',2 if button=='right' else 1),('up',0)]:
            self.input_ack({'type':'pointer','action':action,'button':button,
                'buttons':buttons,'click_count':1,'modifiers':0,'x':32,'y':30})

    def ctrl_a(self):
        for action in ('down','up'):
            self.input_ack({'type':'key','action':action,'key':'a','code':'KeyA','modifiers':2})

    def test_input_before_and_after_native_copy(self):
        # Deliberately do NOT use fixture.load(): it bootstraps the private helper
        # before measuring the initial pointer path.
        self.call('browser.navigate',target=self.target,
            url=f'http://127.0.0.1:{self.server.server_port}/root?cross=0&divert=0')
        self.wait(lambda:self.has_event('loaded-a') and self.has_event('loaded-b'),'owned source/children loaded')
        self.wait(lambda:self.has_event('permissions-a') and self.has_event('permissions-b'),'permission baseline')
        self.assert_permissions()
        self.evaluate('''(()=>{
 window.inputMouse=0;window.inputContext=0;window.inputKeys=0;window.inputJournal=[];
 document.addEventListener('mousedown',e=>{inputMouse++;inputJournal.push({type:e.type,trusted:e.isTrusted,button:e.button,target:e.target.id,x:e.clientX,y:e.clientY})},true);
 document.addEventListener('contextmenu',e=>{inputContext++;inputJournal.push({type:e.type,trusted:e.isTrusted,target:e.target.id});e.preventDefault()},true);
 document.addEventListener('keydown',e=>{if(e.ctrlKey && e.key.toLowerCase()==='a'){inputKeys++;inputJournal.push({type:e.type,trusted:e.isTrusted,key:e.key,ctrl:e.ctrlKey,target:e.target.id})}},true);
 return true;
 })()''')
        self.focus_root()  # Owned fixture setup, not a production workaround.
        first=self.snapshot('before-any-edit-helper')
        source_info=next(t for t in first['lifecycle']['targets']['targetInfos'] if t['targetId']==self.target)
        self.assertFalse([t for t in first['lifecycle']['targets']['targetInfos']
            if t['type']=='page' and t.get('browserContextId')!=source_info.get('browserContextId')],
            'initial measurement must precede private helper creation')
        self.click()
        self.assertEqual(self.events()['mouse'],1,'initial public mouse must reach source')
        self.ctrl_a()
        self.assertEqual(self.events()['keys'],1,'initial public Ctrl+A must reach source')
        self.snapshot('before-copy')
        copied=self.call('browser.clipboard',target=self.target,operation='copy')
        self.assertEqual(copied,{'kind':'copied','text':frames.ROOT_WITNESS})
        self.helper_ready=True
        self.private_boundary('after-copy')
        self.snapshot('after-copy-before-new-input')
        # No DOM focus, activation, pointer relocation, OS reader or new viewport
        # operation is inserted between Copy and the identical pointer request.
        self.ctrl_a()
        keys_after=self.events()['keys']
        before=self.events()
        self.click()
        after=self.events()
        self.snapshot('after-identical-left-pointer')
        self.click('right')
        right=self.events()
        self.snapshot('after-right-pointer')
        try:
            context=self.call('browser.context',surface=self.surface,x=32,y=30)
        except Fault as error:
            context={'fault':{'kind':error.kind,'message':str(error)}}
        self.report['input_after_copy']={'before_mouse':before['mouse'],'after_mouse':after['mouse'],
            'after_right_mouse':right['mouse'],'right_contextmenus':right['context'],
            'keys_after_copy':keys_after,'context_result':context}
        dropped=(after['mouse']!=before['mouse']+1 or right['mouse']!=after['mouse']+1 or right['context']<1)
        if dropped:
            # Explicit diagnostic only. Never called on the straight passing path
            # and never converts the acceptance assertion to green.
            self.report['bring_to_front_experiment']={'performed':True,'before':self.events()}
            self.browser.cdp.call('Page.bringToFront',session=self.session)
            self.click()
            self.report['bring_to_front_experiment']['after']=self.events()
            self.snapshot('after-explicit-diagnostic-bring-to-front')
        else:
            self.report['bring_to_front_experiment']={'performed':False,'reason':'no core mouse drop reproduced'}
        self.assertEqual(keys_after,2,'public keyboard delivery must survive edit helper creation')
        self.assertEqual(after['mouse'],before['mouse']+1,'post-edit left pointer silently dropped')
        self.assertEqual(right['mouse'],after['mouse']+1,'post-edit right pointer silently dropped')
        self.assertEqual(right['context'],1,'actual trusted source contextmenu must fire')
        self.assertTrue(context.get('editable'),context)
        self.assertTrue(all(e['trusted'] for e in right['journal']))
        self.report['result']='PASS'


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([NativeInputAfterEdit('test_input_before_and_after_native_copy')])


if __name__=='__main__':
    unittest.main()

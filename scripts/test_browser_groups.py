"""Public worker group contract. Native fixtures require explicit scratch opt-in."""
import base64
import os
import struct
import threading
import unittest
from unittest.mock import patch
from browser_worker import Browser, Fault, Lease, Worker


class GroupProtocol(unittest.TestCase):
    def setUp(self):
        self.worker=Worker('/unused-owned-root','/unused-chrome')
        b=self.browser=Browser.__new__(Browser)
        b.running=True;b.instance='instance';b.lease=Lease();b.state_lock=threading.RLock();b.local=threading.local()
        b.sessions={};b.tools={};b.sizes={};b.sequences={};b.internal_targets=set();b.selected=None;b.revision=0
        self.infos={'boot':{'targetId':'boot','type':'page','url':'about:blank','title':''}}
        self.calls=[];self.next=0
        def call(method,params=None,session=None):
            p=params or {};self.calls.append((method,p.copy()))
            if method=='Target.getTargets':return {'targetInfos':[dict(i) for i in self.infos.values()]}
            if method=='Target.createTarget':
                self.next+=1;target='native-'+str(self.next)
                self.infos[target]={'targetId':target,'type':'page','url':p['url'],'title':''}
                return {'targetId':target}
            if method=='Target.getTargetInfo':return {'targetInfo':dict(self.infos[p['targetId']])}
            if method=='Target.attachToTarget':return {'sessionId':p['targetId']}
            if method=='Page.captureScreenshot':
                header=b'\x89PNG\r\n\x1a\n'+b'\0\0\0\rIHDR'+struct.pack('>II',1,1)+b'\x08\x06\0\0\0'+b'\0'*4
                return {'data':base64.b64encode(header).decode()}
            if method=='Page.navigate':self.infos[session]['url']=p['url'];return {}
            if method=='Target.closeTarget':return {'success':self.infos.pop(p['targetId'],None) is not None}
            self.fail('unexpected native operation: '+method)
        b.cdp=type('OwnedProtocol',(),{'closed':False})()
        b.cdp.call=call
        self.worker.browsers['ws']=b
        self.p={'workspace':'ws','generation':b.lease.acquire('viewer',False,False)}

    def call(self,method,**params):
        return self.worker.dispatch('viewer',method,dict(self.p,**params))

    def ensure(self,group='pane',**params):
        params.setdefault('visible',True)
        return self.call('browser.group.ensure',group=group,**params)

    def writes(self):
        return [c for c in self.calls if c[0] in ('Target.createTarget','Target.closeTarget','Page.navigate')]

    def test_cold_restore_is_lazy_and_warm_state_wins_without_duplicate_blank(self):
        restore={'tabs':[{'id':'one','url':'http://owned/one'},{'id':'two','url':'http://owned/two'}],'selected':'two'}
        first=self.ensure(restore=restore)
        self.assertEqual([t['id'] for t in first['tabs']],['one','two'])
        self.assertIsNone(first['tabs'][0]['target'])
        self.assertEqual(first['tabs'][1]['target'],'boot')
        self.assertEqual(self.writes(),[('Page.navigate',{'url':'http://owned/two'})])
        before=self.writes()
        warm=self.ensure(restore={'tabs':[{'id':'stale','url':'http://owned/stale'}],'selected':'stale'})
        self.assertEqual(warm['selected'],'two')
        self.assertEqual([t['id'] for t in warm['tabs']],['one','two'])
        self.assertEqual(self.writes(),before)
        selected=self.call('browser.tab.select',group='pane',tab='one')
        self.assertIsNotNone(selected['tabs'][0]['target'])
        self.assertEqual(len(self.writes()),2)

    def test_same_controller_reattach_resets_only_its_groups_frame_streams(self):
        a=self.ensure('a');b=self.ensure('b')
        sa={'kind':'page','target':a['tabs'][0]['target']}
        sb={'kind':'page','target':b['tabs'][0]['target']}
        self.assertEqual(self.call('browser.frame',group='a',surface=sa,ack=0)['sequence'],1)
        self.assertEqual(self.call('browser.frame',group='b',surface=sb,ack=0)['sequence'],1)
        generation=self.call('browser.acquire')['generation']
        self.assertEqual(generation,self.p['generation'])
        self.ensure('a',reset_frames=True)
        self.assertEqual(self.call('browser.frame',group='a',surface=sa,ack=0)['sequence'],1)
        self.assertEqual(self.call('browser.frame',group='b',surface=sb,ack=1)['sequence'],2)
        before=self.writes()
        with self.assertRaises(Fault):self.ensure('a',reset_frames='yes')
        self.assertEqual(self.writes(),before)

    def test_trusted_context_new_tab_joins_only_its_source_group_in_background(self):
        source=self.ensure()
        self.ensure('other',restore={'tabs':[],'selected':None})
        def native_context(browser,identity,method,params,navigation):
            target=browser.cdp.call('Target.createTarget',{'url':'http://owned/link','background':True})['targetId']
            return {'kind':'action-dispatched','action':'new_tab','target':target,'url':'http://owned/link','opener_preserved':False}
        with patch('browser_worker.context_dispatch',side_effect=native_context):
            result=self.call('browser.context_action',group='pane',target=source['tabs'][0]['target'],token='owned-token',action='new_tab')
        snapshot=self.call('browser.tabs',group='pane')
        self.assertEqual(len(snapshot['tabs']),2)
        self.assertEqual(snapshot['selected'],source['selected'])
        self.assertEqual(snapshot['tabs'][1]['target'],result['target'])
        self.assertEqual(snapshot['tabs'][1]['url'],'http://owned/link')
        self.assertEqual(self.call('browser.tabs',group='other')['tabs'],[])
        self.call('browser.navigate',group='pane',target=result['target'],url='http://owned/next')

    def test_full_group_rejects_context_new_tab_before_native_dispatch(self):
        tabs=[{'id':'t'+str(i),'url':'about:blank'} for i in range(64)]
        source=self.ensure(restore={'tabs':tabs,'selected':'t0'})
        before=self.writes()
        with patch('browser_worker.context_dispatch') as native_context:
            with self.assertRaises(Fault):
                self.call('browser.context_action',group='pane',target=source['tabs'][0]['target'],token='owned-token',action='new_tab')
            native_context.assert_not_called()
        self.assertEqual(self.writes(),before)

    def test_reorder_move_close_preserve_native_identity_and_reset_ack(self):
        first=self.ensure(url='http://owned/a')
        a=first['selected'];target=first['tabs'][0]['target']
        second=self.call('browser.tab.create',group='pane',url='http://owned/b')
        b=second['selected']
        reordered=self.call('browser.tab.reorder',group='pane',tab=b,before=a)
        self.assertEqual([t['id'] for t in reordered['tabs']],[b,a])
        self.assertEqual(reordered['selected'],b)
        self.ensure('destination',restore={'tabs':[],'selected':None})
        self.assertEqual(self.call('browser.frame',group='pane',surface={'kind':'page','target':target},ack=0)['sequence'],1)
        before=self.writes()
        moved=self.call('browser.tab.move',group='pane',tab=a,to_group='destination')
        self.assertEqual(moved['destination']['tabs'][0]['target'],target)
        self.assertEqual(moved['destination']['selected'],a)
        self.assertEqual(self.writes(),before)
        self.assertEqual(self.call('browser.frame',group='destination',surface={'kind':'page','target':target},ack=0)['sequence'],1)
        closed=self.call('browser.tab.close',group='destination',tab=a)
        self.assertTrue(closed['empty'])
        self.assertEqual(closed['closed'],{'id':a,'url':'http://owned/a'})
        self.assertEqual(closed['tabs'],[])
        self.assertTrue(self.browser.running)

    def test_group_membership_rejects_foreign_native_targets_before_io(self):
        self.ensure('first')
        other=self.ensure('second',url='http://owned/second')
        target=other['tabs'][0]['target']
        for method,extra in [('browser.navigate',{'target':target,'url':'http://owned/no'}),
                             ('browser.input',{'surface':{'kind':'page','target':target},'event':{}}),
                             ('browser.context',{'surface':{'kind':'page','target':target},'x':1,'y':1}),
                             ('browser.clipboard',{'target':target,'operation':'copy'})]:
            before=len(self.calls)
            with self.assertRaises(Fault):self.call(method,group='first',**extra)
            self.assertEqual(len(self.calls),before)
        before=len(self.calls)
        with self.assertRaises(Fault):self.call('browser.tab.move',group='first',tab='missing',to_group='second',to_workspace='other')
        self.assertEqual(len(self.calls),before)

    def test_restore_validation_limits_and_stale_lease_do_not_touch_native_io(self):
        for restore in [
            {'tabs':[{'id':'a','url':'file:///private'}],'selected':'a'},
            {'tabs':[{'id':'a','url':'about:blank'}]*2,'selected':'a'},
            {'tabs':[{'id':str(i),'url':'about:blank'} for i in range(65)],'selected':'0'},
            {'tabs':[{'id':'../escape','url':'about:blank'}],'selected':'../escape'},
            {'tabs':[{'id':'a','url':'about:blank'}],'selected':'missing'}]:
            before=len(self.calls)
            with self.assertRaises(Fault):self.ensure(restore=restore)
            self.assertEqual(len(self.calls),before)
        with self.assertRaises(Fault):
            self.worker.dispatch('viewer','browser.group.ensure',dict(self.p,generation='stale',group='pane',visible=True))
        with self.assertRaises(Fault):
            self.worker.dispatch('intruder','browser.group.ensure',dict(self.p,group='pane',visible=True))
        self.assertEqual(self.calls,[])

    def test_legacy_migration_and_legacy_calls_remain_actual_target_api(self):
        self.call('browser.navigate',target='boot',url='http://owned/live')
        live=self.ensure(restore={'tabs':[{'id':'saved','url':'http://owned/live'}],'selected':'saved'})
        self.assertEqual(live['tabs'][0]['target'],'boot')
        before=self.writes()
        self.ensure(restore={'tabs':[{'id':'old','url':'http://owned/old'}],'selected':'old'})
        self.assertEqual(self.writes(),before)
        legacy=self.call('browser.tabs')
        self.assertEqual(legacy['tabs'][0]['id'],'boot')
        created=self.call('browser.tab.create',url='http://owned/unassigned')
        self.assertIn('id',created)
        self.assertNotIn('group',created)
        self.assertEqual(len(self.call('browser.tabs',group='pane')['tabs']),1)

    def test_client_id_then_queued_navigation_resolves_only_loaded_named_tab(self):
        old=self.ensure(url='http://owned/old')
        old_target=old['tabs'][0]['target']
        created=self.call('browser.tab.create',group='pane',id='bt-queued',url='about:blank')
        actual=next(t['target'] for t in created['tabs'] if t['id']=='bt-queued')
        self.assertNotEqual(actual,'bt-queued')
        self.call('browser.tab.select',target=old_target)  # Legacy selected must not be followed.
        self.call('browser.navigate',group='pane',tab='bt-queued',url='http://owned/intended')
        tabs=self.call('browser.tabs',group='pane')['tabs']
        self.assertEqual(next(t['url'] for t in tabs if t['id']=='bt-queued'),'http://owned/intended')
        self.assertEqual(next(t['url'] for t in tabs if t['target']==old_target),'http://owned/old')
        self.assertEqual(self.call('browser.frame',group='pane',tab='bt-queued',ack=0)['sequence'],1)
        before=len(self.calls)
        for method,extra in [('browser.clipboard',{'operation':'copy'}),('browser.context',{'x':1,'y':1}),
                             ('browser.context_action',{'action':'copy','token':'unknown'})]:
            with self.assertRaises(Fault):self.call(method,group='pane',tab='bt-queued',**extra)
            self.assertEqual(len(self.calls),before)
        self.ensure('lazy',visible=False,restore={'tabs':[{'id':'bt-lazy','url':'http://owned/lazy'}],'selected':'bt-lazy'})
        before=len(self.calls)
        with self.assertRaises(Fault):self.call('browser.navigate',group='lazy',tab='bt-lazy',url='http://owned/no')
        self.assertEqual(len(self.calls),before)
        self.call('browser.tab.select',group='lazy',tab='bt-lazy')
        self.call('browser.navigate',group='lazy',tab='bt-lazy',url='http://owned/selected-then-navigate')
        self.assertEqual(self.call('browser.tabs',group='lazy')['tabs'][0]['url'],'http://owned/selected-then-navigate')
        before=len(self.calls)
        with self.assertRaises(Fault):self.call('browser.tab.create',group='pane',id='bt-lazy')
        self.assertEqual(len(self.calls),before)
        with self.assertRaises(Fault):self.call('browser.frame',group='pane',tab='bt-queued',surface={'kind':'page','target':old_target})
        self.assertEqual(len(self.calls),before)

    def test_pane_scoped_colon_ids_restore_create_and_move_unchanged(self):
        restored=self.ensure('pane:a',visible=False,restore={
            'tabs':[{'id':'pane-a:initial','url':'about:blank'}],'selected':'pane-a:initial'})
        self.assertEqual(restored['selected'],'pane-a:initial')
        created=self.call('browser.tab.create',group='pane:a',id='pane-a:second')
        self.assertEqual(created['selected'],'pane-a:second')
        self.ensure('pane:b',visible=False,restore={'tabs':[],'selected':None})
        moved=self.call('browser.tab.move',group='pane:a',tab='pane-a:initial',to_group='pane:b')
        self.assertEqual(moved['destination']['selected'],'pane-a:initial')
        self.assertEqual(moved['destination']['tabs'][0]['id'],'pane-a:initial')
        self.assertIsNone(moved['destination']['tabs'][0]['target'])

    def test_catalog_bounds_hidden_restore_empty_close_and_popup_deferral(self):
        entries=lambda prefix:[{'id':prefix+str(i),'url':'http://owned/'+prefix+str(i)} for i in range(64)]
        self.ensure('a',visible=False,restore={'tabs':entries('a'),'selected':'a0'})
        self.ensure('b',visible=False,restore={'tabs':entries('b'),'selected':'b0'})
        self.assertEqual(self.writes(),[])
        before=len(self.calls)
        with self.assertRaises(Fault):self.ensure('overflow',url='http://owned/no')
        self.assertEqual(len(self.calls),before)
        with self.assertRaises(Fault):self.ensure('duplicate',restore={'tabs':[{'id':'a0','url':'about:blank'}],'selected':'a0'})
        self.assertEqual(len(self.calls),before)
        closed=self.call('browser.tab.close',group='a',tab='a0')
        self.assertEqual(closed['selected'],'a1')
        self.assertEqual(self.writes(),[])
        # Different browser catalog for the independent group count bound.
        self.setUp()
        for i in range(16):self.ensure('g'+str(i),visible=False,restore={'tabs':[],'selected':None})
        before=len(self.calls)
        with self.assertRaises(Fault):self.ensure('g16')
        self.assertEqual(len(self.calls),before)
        self.setUp()
        self.call('browser.navigate',target='boot',url='http://owned/root')
        self.infos['popup']={'targetId':'popup','type':'page','url':'http://owned/popup','title':'','openerId':'boot'}
        snapshot=self.ensure()
        self.assertEqual([e['target'] for e in snapshot['tabs']],['boot'])


@unittest.skipUnless(os.environ.get('HC_BROWSER_GROUPS_NATIVE_TEST')=='1','explicit isolated native group opt-in')
class NativeGroups(unittest.TestCase):
    def test_owned_lazy_requests_shared_profile_history_move_and_cleanup(self):
        import http.server
        import json
        from pathlib import Path
        import tempfile
        import time
        from browser_probe import sandbox_enabled, private_processes
        self.assertNotEqual(os.geteuid(),0)
        self.assertTrue(os.environ.get('HC_SESSION') not in (None,'','default'))
        root=Path(tempfile.mkdtemp(prefix='hc-groups-',dir='/var/tmp'))
        worker=Worker(root,'/opt/google/chrome/chrome');p={'workspace':'owned'}
        hits={};lock=threading.Lock();server=None;b=None;watchdog=None
        report={'scope':'owned native group catalog, NOT app/GPUI acceptance','checks':{}}
        try:
            ensured=worker.dispatch('viewer','browser.ensure',p);b=worker.browsers['owned']
            report.update(uid=os.geteuid(),version=ensured['version'],instance=ensured['instance'])
            p['generation']=worker.dispatch('viewer','browser.acquire',p)['generation']
            target=worker.dispatch('viewer','browser.tabs',p)['selected'];session=b.session(target)
            def call(method,**extra):return worker.dispatch('viewer',method,dict(p,**extra))
            def evaluate(expression,tab_session=session):
                value=b.cdp.call('Runtime.evaluate',{'expression':expression,'returnByValue':True},tab_session)
                self.assertNotIn('exceptionDetails',value)
                return value.get('result',{}).get('value')
            def wait(predicate):
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.02)
                self.fail('owned fixture did not become ready')
            import signal
            def timeout():
                if b.running:
                    try:os.killpg(b.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
            watchdog=threading.Timer(30,timeout);watchdog.daemon=True;watchdog.start()
            b.cdp.call('Page.navigate',{'url':'chrome://sandbox'},session)
            wait(lambda:sandbox_enabled(evaluate('document.body?.innerText') or ''))
            report['sandbox']='PASS'
            b.cdp.call('Page.navigate',{'url':'about:blank'},session)
            wait(lambda:evaluate('location.href==="about:blank"'))
            class Fixture(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    with lock:hits[self.path]=hits.get(self.path,0)+1
                    self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
                    self.wfile.write(b'''<!doctype html><input id=form><a id=ownedlink href="/context-link" style="position:absolute;left:20px;top:80px">Owned link</a><script>
window.cookieAtLoad=document.cookie;document.cookie='group_shared=owned;path=/';
window.marker=Math.random().toString();window.counter=0;setInterval(()=>counter++,10);
</script>''')
                def log_message(self,*args):pass
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            url='http://127.0.0.1:'+str(server.server_port)
            first=call('browser.group.ensure',group='left',visible=True,restore={'tabs':[
                {'id':'live','url':url+'/one'},{'id':'lazy','url':url+'/hidden'}],'selected':'live'})
            self.assertEqual(first['tabs'][0]['target'],target)
            self.assertIsNone(first['tabs'][1]['target'])
            wait(lambda:evaluate('typeof marker==="string"'))
            with lock:self.assertEqual(hits.get('/hidden',0),0)
            warm=call('browser.group.ensure',group='left',visible=True,restore={'tabs':[
                {'id':'stale','url':url+'/stale'}],'selected':'stale'})
            self.assertEqual(warm['selected'],'live')
            with lock:self.assertEqual(hits.get('/stale',0),0)
            other=call('browser.group.ensure',group='right',visible=True,url=url+'/two')
            other_target=other['tabs'][0]['target'];other_session=b.session(other_target)
            wait(lambda:evaluate('typeof cookieAtLoad==="string"',other_session))
            self.assertIn('group_shared=owned',evaluate('cookieAtLoad',other_session))
            # Same-callback Select/New + Navigate addressing uses stable IDs,
            # never the old global selected native target.
            queued=call('browser.tab.create',group='right',id='bt-client-native',url='about:blank')
            queued_target=next(e['target'] for e in queued['tabs'] if e['id']=='bt-client-native')
            call('browser.navigate',group='right',tab='bt-client-native',url=url+'/queued')
            queued_session=b.session(queued_target)
            wait(lambda:evaluate('location.pathname==="/queued"',queued_session))
            self.assertEqual(evaluate('location.pathname'),'/one')
            call('browser.tab.close',group='right',tab='bt-client-native')
            call('browser.navigate',group='left',tab='live',url=url+'/one-next')
            wait(lambda:evaluate('location.pathname==="/one-next"'))
            call('browser.history',group='left',target=target,direction='back')
            wait(lambda:evaluate('location.pathname==="/one" && typeof marker==="string"'))
            evaluate("form.value='unsaved owned form';true")
            marker=evaluate('marker');counter=evaluate('counter')
            history=b.cdp.call('Page.getNavigationHistory',session=session)
            call('browser.tab.select',group='left',tab='lazy')
            wait(lambda:hits.get('/hidden',0)>0)
            call('browser.tab.select',group='left',tab='live')
            call('browser.viewport',group='left',surface={'kind':'page','target':target},width=800,height=600,scale=1)
            self.assertEqual(call('browser.frame',group='left',surface={'kind':'page','target':target},ack=0)['sequence'],1)
            moved=call('browser.tab.move',group='left',tab='live',to_group='right',before=other['selected'])
            self.assertEqual(moved['destination']['tabs'][0]['target'],target)
            self.assertEqual(call('browser.frame',group='right',surface={'kind':'page','target':target},ack=0)['sequence'],1)
            with self.assertRaises(Fault):call('browser.navigate',group='left',target=target,url=url+'/forbidden')
            self.assertEqual(evaluate('form.value'),'unsaved owned form')
            self.assertEqual(evaluate('marker'),marker)
            self.assertGreaterEqual(evaluate('counter'),counter)
            self.assertEqual(b.cdp.call('Page.getNavigationHistory',session=session),history)
            lazy_target=call('browser.tabs',group='left')['tabs'][0]['target']
            lazy_surface={'kind':'page','target':lazy_target}
            self.assertEqual(call('browser.frame',group='left',surface=lazy_surface,ack=0)['sequence'],1)
            call('browser.group.ensure',group='right',visible=True,reset_frames=True)
            self.assertEqual(call('browser.frame',group='right',surface={'kind':'page','target':target},ack=0)['sequence'],1)
            self.assertEqual(call('browser.frame',group='left',surface=lazy_surface,ack=1)['sequence'],2)
            self.assertEqual(evaluate('form.value'),'unsaved owned form')
            self.assertEqual(b.cdp.call('Page.getNavigationHistory',session=session),history)
            # Trusted menu-created pages must enter the moved source's current
            # group, unlike unrelated engine popups (still unsupported).
            context=call('browser.context',group='right',surface={'kind':'page','target':target},x=30,y=88)
            self.assertEqual(context['link'],url+'/context-link')
            opened=call('browser.context_action',group='right',target=target,token=context['token'],action='new_tab')
            snapshot=call('browser.tabs',group='right')
            child=next(tab for tab in snapshot['tabs'] if tab['target']==opened['target'])
            self.assertEqual(child['url'],url+'/context-link')
            self.assertEqual(snapshot['selected'],'live')
            self.assertFalse(any(tab['target']==opened['target'] for tab in call('browser.tabs',group='left')['tabs']))
            call('browser.tab.close',group='right',tab=child['id'])
            self.assertEqual(call('browser.ensure')['pid'],ensured['pid'])
            call('browser.group.visible',group='left',visible=False)
            call('browser.tab.close',group='right',tab='live')
            final=call('browser.tab.close',group='right',tab=other['selected'])
            self.assertTrue(final['empty']);self.assertTrue(b.running)
            last=call('browser.tab.close',group='left',tab='lazy')
            self.assertTrue(last['empty'])
            self.assertEqual(call('browser.status')['state'],'running')
            self.assertIn('product',b.cdp.call('Browser.getVersion'))
            self.assertEqual(call('browser.tabs')['tabs'],[])
            report['checks']={'cold_hidden_requested':False,'warm_stale_requested':False,'shared_cookie':True,
                'move_target_preserved':True,'form_marker_history_preserved':True,'frame_ack_reset':True,
                'single_pid':ensured['pid'],'client_id_queued_navigation':True,'last_close_empty_without_engine_stop':True,
                'trusted_context_new_tab_scoped_in_background':True,'reattach_scoped_ack_reset':True}
            report['result']='PASS'
        finally:
            if server:server.shutdown();server.server_close()
            if b:
                try:worker.dispatch('viewer','browser.stop',dict(p,confirmed=True))
                except Exception:
                    import signal
                    try:os.killpg(b.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    try:os.waitpid(b.pid,0);b.running=False
                    except ChildProcessError:pass
                    b.expiry_stop.set();b.cdp.close();b.cleanup_runtime()
                report['cleanup']={'pid':b.pid,'owned_pid_reaped':not b.running,'runtime_removed':not b.runtime_root.exists(),
                                   'remaining_owned_pids':private_processes(b.runtime_root)}
            if watchdog:watchdog.cancel()
            if os.environ.get('HC_BROWSER_GROUPS_EVIDENCE'):
                Path(os.environ['HC_BROWSER_GROUPS_EVIDENCE']).write_text(json.dumps(report,indent=2))
            if b:
                self.assertFalse(b.running);self.assertFalse(b.runtime_root.exists())
                self.assertEqual(report['cleanup']['remaining_owned_pids'],[])
            import shutil
            shutil.rmtree(root)


if __name__=='__main__':unittest.main()

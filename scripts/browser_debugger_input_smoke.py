#!/usr/bin/env python3
"""Measure real page-pointer pause -> frontend input step/resume broker latency.

No Runtime/SDK resume or step actions. Frontend SDK is used only to observe pause.
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

root = Path(tempfile.mkdtemp(prefix='hc-debug.', dir='/var/tmp'))
root.chmod(0o700)
print(root, flush=True)
class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type','text/html')
        self.end_headers()
        self.wfile.write(b'''<button id=debug onclick="pauseFixture()">Pause</button><script>
function pauseFixture() {
  const before = 40;
  debugger;
  window.afterStep = before + 1;
  window.afterStep += 1;
}
</script>''')
    def log_message(self,*args):pass
server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Fixture)
threading.Thread(target=server.serve_forever,daemon=True).start()
w = Worker(root,'/opt/google/chrome/chrome')
p = {'workspace':'native_debugger'}
report = {'result':'INCOMPLETE','actions':[]}
b = None
try:
    w.dispatch('viewer','browser.ensure',p)
    p.update(w.dispatch('viewer','browser.acquire',p))
    target=w.dispatch('viewer','browser.tabs',p)['selected']
    b=w.browsers[p['workspace']]
    w.dispatch('viewer','browser.navigate',dict(p,target=target,url='http://127.0.0.1:'+str(server.server_port)+'/'))
    page={'kind':'page','target':target}
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        ready=b.cdp.call('Runtime.evaluate',{'expression':'!!document.querySelector("#debug")','returnByValue':True},b.session(target))
        if ready.get('result',{}).get('value'):break
    w.dispatch('viewer','browser.viewport',dict(p,surface=page,width=1000,height=400,scale=1))
    w.dispatch('viewer','browser.devtools.open',dict(p,target=target))
    tools={'kind':'devtools','target':target}
    w.dispatch('viewer','browser.viewport',dict(p,surface=tools,width=1000,height=600,scale=1))
    front=b.tools[target]['frontend']
    def observe(expression):
        r=b.cdp.call('Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True},front)
        assert 'exceptionDetails' not in r,r
        return r.get('result',{}).get('value')
    pause_expression="""(async()=>{const SDK=await import('./core/sdk/sdk.js');const t=SDK.TargetManager.TargetManager.instance().targets().find(t=>t.type()==='frame');return t?.model(SDK.DebuggerModel.DebuggerModel).isPaused();})()"""
    def timed(method,more):
        start=time.monotonic()
        try:
            value=w.dispatch('viewer',method,dict(p,**more))
            report['actions'].append({'method':method,'event':more.get('event'),'elapsed_ms':round((time.monotonic()-start)*1000,2),'result':'PASS',**({'receipt_result':value} if method=='browser.input' else {})})
            return value
        except Exception as error:
            report['actions'].append({'method':method,'event':more.get('event'),'elapsed_ms':round((time.monotonic()-start)*1000,2),'error':str(error)})
            raise
    deadline=time.monotonic()+5
    enabled_expression="""(async()=>{const SDK=await import('./core/sdk/sdk.js');const t=SDK.TargetManager.TargetManager.instance().targets().find(t=>t.type()==='frame');return t?.model(SDK.DebuggerModel.DebuggerModel).debuggerEnabled();})()"""
    while time.monotonic()<deadline:
        if observe(enabled_expression):break
    assert observe(enabled_expression) is True,'frontend debugger model not ready'
    for event,kind in [({'type':'key','action':'down','key':'a','modifiers':16},'invalid_params'),
                       ({'type':'pointer','action':'move','button':'none','buttons':'invalid','click_count':1,'modifiers':0,'x':0,'y':0},'browser_failed')]:
        try:
            w.dispatch('viewer','browser.input',dict(p,surface=page,event=event))
            raise AssertionError('invalid input accepted')
        except Exception as error:
            assert getattr(error,'kind',None)==kind,error
            report.setdefault('normal_errors',[]).append({'kind':kind,'message':str(error)})
    rect=b.cdp.call('Runtime.evaluate',{'expression':"(()=>{let r=document.querySelector('#debug').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()",'returnByValue':True},b.session(target))['result']['value']
    for action,buttons in [('down',1),('up',0)]:
        timed('browser.input',{'surface':page,'event':dict(type='pointer',action=action,button='left',buttons=buttons,click_count=1,modifiers=0,**rect)})
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if observe(pause_expression):break
    assert observe(pause_expression) is True,'pointer did not pause actual debugger'
    frame=timed('browser.frame',{'surface':tools})
    (root/'paused.png').write_bytes(base64.b64decode(frame['event']['data']))
    # Focus actual frontend by clicking its Sources tab, then use frontend keys.
    locate="""(()=>{const find=r=>{const e=r.querySelector('#tab-sources');if(e){const b=e.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};}for(const e of r.querySelectorAll('*'))if(e.shadowRoot){const v=find(e.shadowRoot);if(v)return v;}return null;};return find(document);})()"""
    rect=observe(locate)
    assert rect
    for action,buttons in [('down',1),('up',0)]:
        timed('browser.input',{'surface':tools,'event':dict(type='pointer',action=action,button='left',buttons=buttons,click_count=1,modifiers=0,**rect)})
    def toolbar(label):
        expression="""(label=>{const find=r=>{for(const e of r.querySelectorAll('[aria-label],[title]')){const text=e.getAttribute('aria-label')||e.getAttribute('title')||'';if(text.startsWith(label)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'){const b=e.getBoundingClientRect();if(b.width&&b.height)return {x:b.x+b.width/2,y:b.y+b.height/2,label:text};}}for(const e of r.querySelectorAll('*'))if(e.shadowRoot){const b=find(e.shadowRoot);if(b)return b;}return null;};return find(document);})("""+json.dumps(label)+')'
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            value=observe(expression)
            if value:return value
        raise AssertionError('frontend toolbar not ready: '+label)
    def click_toolbar(label):
        value=toolbar(label)
        report.setdefault('frontend_controls',[]).append(value['label'])
        for action,buttons in [('down',1),('up',0)]:
            timed('browser.input',{'surface':tools,'event':dict(type='pointer',action=action,button='left',buttons=buttons,click_count=1,modifiers=0,x=value['x'],y=value['y'])})
    line_expression="""(async()=>{const SDK=await import('./core/sdk/sdk.js');const t=SDK.TargetManager.TargetManager.instance().targets().find(t=>t.type()==='frame');return t.model(SDK.DebuggerModel.DebuggerModel).debuggerPausedDetails()?.callFrames[0]?.location().lineNumber;})()"""
    before=observe(line_expression)
    click_toolbar('Step over')
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        after=observe(line_expression)
        if after is not None and after!=before:break
    assert after is not None and after!=before,(before,after)
    click_toolbar('Resume script execution')
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if observe(pause_expression) is False:break
    assert observe(pause_expression) is False,'real frontend F8 did not resume'
    value=b.cdp.call('Runtime.evaluate',{'expression':'window.afterStep','returnByValue':True},b.session(target))['result'].get('value')
    assert value==42,value
    heartbeat=w.dispatch('viewer','browser.heartbeat',p)
    assert heartbeat['input_errors']==[] and heartbeat['input_pending']==0,heartbeat
    # Repeat pause, then revoke while its input receipt is outstanding. Chrome
    # closes the old frontend; its late ACK must not belong to the replacement.
    rect=b.cdp.call('Runtime.evaluate',{'expression':"(()=>{let r=document.querySelector('#debug').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()",'returnByValue':True},b.session(target))['result']['value']
    for action,buttons in [('down',1),('up',0)]:
        replacement_pending=timed('browser.input',{'surface':page,'event':dict(type='pointer',action=action,button='left',buttons=buttons,click_count=1,modifiers=0,**rect)})
    assert replacement_pending['pending'] is True,replacement_pending
    replaced=w.dispatch('replacement','browser.acquire',dict(workspace=p['workspace'],force=True,confirmed=True))
    new_heartbeat=w.dispatch('replacement','browser.heartbeat',dict(workspace=p['workspace'],generation=replaced['generation']))
    assert new_heartbeat['input_errors']==[] and new_heartbeat['input_pending']==0,new_heartbeat
    assert not b.tools,'revoked frontend remained mapped'
    try:
        w.dispatch('viewer','browser.heartbeat',p)
        raise AssertionError('old owner heartbeat accepted')
    except Exception as error:
        assert getattr(error,'kind',None)=='not_owner',error
    report.update(result='PASS',paused_by='real page pointer',stepped_by='real frontend toolbar',resumed_by='real frontend toolbar',observed_afterStep=value,heartbeat=heartbeat,replacement_heartbeat=new_heartbeat,stale_receipt_discard='PASS',max_action_ms=max(v['elapsed_ms'] for v in report['actions']))
    print(json.dumps(report,indent=2),flush=True)
finally:
    (root/'report.json').write_text(json.dumps(report,indent=2))
    server.shutdown();server.server_close()
    if b:
        try:
            b.cdp.call('Browser.close');os.waitpid(b.pid,0);b.expiry_stop.set();b.cdp.close();b.cleanup_runtime()
        except Exception as error:print('cleanup',str(error),flush=True)

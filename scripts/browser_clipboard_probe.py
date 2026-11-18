#!/usr/bin/env python3
"""Owned headless native-editing experiment; NOT OS clipboard/GPUI acceptance.

No core, desktop connection, permission grants, source-selection extraction or
production transport. Source DOM reads below are fixture assertions only.
"""
import argparse
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time

from browser_cdp import CDP
from browser_probe import launch_args, private_processes, sandbox_enabled

FIXTURE = '''<!doctype html><meta charset=utf-8><textarea id=editor></textarea>
<script>
window.journal=[];window.mode='default';window.payload='';window.cancelKey=false;
for (const type of ['copy','cut','paste']) document.addEventListener(type,e=>{
 const item={type,trusted:e.isTrusted,types:[...e.clipboardData.types]};
 if(type==='paste') item.text=e.clipboardData.getData('text/plain');
 if(mode==='cancel') e.preventDefault();
 if(mode==='custom' && type!=='paste') {
   e.clipboardData.setData('text/plain',payload);
   e.clipboardData.setData('application/x-herdr-owned','owned-custom');e.preventDefault();
 }
 if(mode==='custom' && type==='paste') {
   e.preventDefault();editor.setRangeText('CUSTOM:'+e.clipboardData.getData('text/plain'));
 }
 item.canceled=e.defaultPrevented;journal.push(item);
});
document.addEventListener('keydown',e=>{
 journal.push({type:'keydown',trusted:e.isTrusted,key:e.key});
 if(cancelKey)e.preventDefault();
});
window.prepare=(text,m='default',p='',select=true)=>{
 editor.value=text;editor.focus();editor.setSelectionRange(0,select?text.length:0);
 journal=[];mode=m;payload=p;cancelKey=false;
};
</script>'''
HELPER = '''document.body.innerHTML='<textarea id="dummy">x</textarea>';
window.payload='';window.journal=[];
document.addEventListener('copy',e=>{
 e.clipboardData.setData('text/plain',payload);e.preventDefault();
 journal.push({type:'copy',trusted:e.isTrusted});
});
document.addEventListener('paste',e=>{
 journal.push({type:'paste',trusted:e.isTrusted,text:e.clipboardData.getData('text/plain'),types:[...e.clipboardData.types]});
 e.preventDefault();
});
window.ready=(text)=>{payload=text;journal=[];dummy.value='x';dummy.focus();dummy.select();};
'''


def run(chrome):
    if os.geteuid() == 0:
        raise PermissionError('normal user required')
    base = Path.home() / '.cache/herdr-browser-prototype'
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='core-clipboard-proof-', dir=base))
    root.chmod(0o700)
    private = root / 'private'
    private.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith('HC_')}
    for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK']:
        env.pop(key, None)
    for key in ['HOME', 'TMPDIR', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME']:
        path = private / key.lower()
        path.mkdir(mode=0o700)
        env[key] = str(path)
    helpers = private / 'helpers'
    helpers.mkdir(mode=0o700)
    env['PATH'] = str(helpers)
    # Linux Unix-socket pathname limit: alias only OUR owned TMPDIR, retaining
    # the directory fd until all Chrome children exit. All files stay under root.
    tmp_fd = os.open(private / 'tmpdir', os.O_RDONLY | os.O_DIRECTORY)
    env['TMPDIR'] = f'/proc/{os.getpid()}/fd/{tmp_fd}'
    profile = private / 'profile'
    profile.mkdir(mode=0o700)
    args = [str(chrome.resolve(strict=True)), *launch_args(profile)]
    report = {'scope': 'owned headless Chrome ONLY; not OS/Wayland/GPUI acceptance',
              'uid': os.geteuid(), 'argv': args, 'result': 'FAIL', 'checks': {},
              'ack_semantics': 'CDP response observed, not ClipboardHost write-commit barrier'}
    journal = (root / 'cdp.jsonl').open('w')
    cdp = None
    pid = None
    fds = []
    server = None

    def call(method, params=None, session=None):
        started = time.monotonic()
        try:
            value = cdp.call(method, params, session, timeout=5)
            journal.write(json.dumps({'method': method, 'session': session, 'params': params,
                                      'ack_observed': True, 'elapsed_ms': round((time.monotonic()-started)*1000, 2)})+'\n')
            journal.flush()
            return value
        except Exception as error:
            journal.write(json.dumps({'method': method, 'error': str(error)})+'\n')
            journal.flush()
            raise

    def evaluate(session, expression):
        answer = call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session)
        if 'exceptionDetails' in answer:
            raise RuntimeError(str(answer['exceptionDetails']))
        return answer.get('result', {}).get('value')

    def native(session, command):
        # Deliberately a real trusted keydown with a native edit command. This
        # cannot claim native-menu semantics independent of keydown cancellation.
        call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'F24', 'code': 'F24',
             'windowsVirtualKeyCode': 135, 'commands': [command]}, session)
        call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'F24', 'code': 'F24',
             'windowsVirtualKeyCode': 135}, session)

    try:
        incoming, write = os.pipe()
        read, outgoing = os.pipe()
        si = fcntl.fcntl(incoming, fcntl.F_DUPFD_CLOEXEC, 10)
        so = fcntl.fcntl(outgoing, fcntl.F_DUPFD_CLOEXEC, 10)
        fds = [incoming, write, read, outgoing, si, so]
        with open(os.devnull, 'rb') as null, (root / 'chromium.stderr').open('wb') as err:
            actions = [(os.POSIX_SPAWN_DUP2, a, b) for a, b in [(null.fileno(), 0), (err.fileno(), 1), (err.fileno(), 2), (si, 3), (so, 4)]]
            actions += [(os.POSIX_SPAWN_CLOSE, fd) for fd in fds if fd not in (3, 4)]
            pid = os.posix_spawn(args[0], args, env, file_actions=actions, setsid=True)
        report['pid'] = pid
        for fd in [incoming, outgoing, si, so]:
            os.close(fd)
            fds.remove(fd)
        cdp = CDP(read, write)
        fds.clear()  # CDP owns these ends.
        report['version'] = call('Browser.getVersion')
        sandbox = call('Target.createTarget', {'url': 'chrome://sandbox'})['targetId']
        ss = call('Target.attachToTarget', {'targetId': sandbox, 'flatten': True})['sessionId']
        until = time.monotonic()+4
        text = ''
        while time.monotonic() < until:
            text = evaluate(ss, 'document.body?.innerText') or ''
            if sandbox_enabled(text):
                break
            time.sleep(.05)
        (root / 'sandbox.txt').write_text(text)
        assert sandbox_enabled(text), 'enabled sandbox not proved'
        report['sandbox'] = 'PASS'
        call('Target.closeTarget', {'targetId': sandbox})
        for permission in ['geolocation', 'camera', 'microphone', 'notifications', 'clipboard-read', 'clipboard-write']:
            call('Browser.setPermission', {'permission': {'name': permission}, 'setting': 'denied'})
        call('Browser.setDownloadBehavior', {'behavior': 'deny'})

        class Fixture(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(FIXTURE.encode())
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        target = call('Target.createTarget', {'url': f'http://127.0.0.1:{server.server_port}/'})['targetId']
        source = call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        until = time.monotonic()+4
        while not evaluate(source, 'typeof prepare === "function"'):
            assert time.monotonic() < until, 'fixture load timed out'
            time.sleep(.05)
        context = call('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
        helper_target = call('Target.createTarget', {'url': 'about:blank', 'browserContextId': context})['targetId']
        helper = call('Target.attachToTarget', {'targetId': helper_target, 'flatten': True})['sessionId']
        evaluate(helper, HELPER)
        source_info = call('Target.getTargetInfo', {'targetId': target})['targetInfo']
        helper_info = call('Target.getTargetInfo', {'targetId': helper_target})['targetInfo']
        assert helper_info['browserContextId'] == context
        assert source_info.get('browserContextId') != context
        report['ownership'] = {'source_target': target, 'helper_target': helper_target, 'helper_context': context,
                               'helper_identity': 'created context and target IDs, not URL matching',
                               'different_contexts_verified': True}

        def prepare(text, mode='default', payload='', select=True):
            evaluate(source, 'prepare('+','.join(json.dumps(x) for x in [text, mode, payload, select])+')')

        def seed(text):
            assert len(text.encode()) <= 65536
            evaluate(helper, 'ready('+json.dumps(text)+')')
            native(helper, 'Copy')
            observed = read_helper(text)
            assert observed['text'] == text, 'helper seed not observed'
            return observed

        def read_helper(expected=None):
            until = time.monotonic()+1.5
            last = None
            while time.monotonic() < until:
                evaluate(helper, 'journal=[];dummy.focus();dummy.select()')
                native(helper, 'Paste')
                events = evaluate(helper, 'journal')
                last = next((event for event in events if event['type']=='paste'), None)
                if last and len(last['text'].encode()) > 65536:
                    raise RuntimeError('clipboard payload exceeds 64KiB UTF-8 probe bound')
                if last and (expected is None or last['text'] == expected):
                    return last
                time.sleep(.03)
            raise RuntimeError('NoUpdateObserved/helper paste: '+json.dumps(last))

        def observe_unchanged(expected):
            # Sample the full bounded window; absence of an update is NOT a
            # ClipboardHost commit/cancellation barrier and never justifies retry.
            deadline = time.monotonic()+.4
            count = 0
            while True:
                last = read_helper(expected)
                assert last['text'] == expected
                count += 1
                if time.monotonic() >= deadline:
                    return {'status': 'NoUpdateObserved', 'samples': count, 'last': last}
                time.sleep(.03)

        def oracle():
            return evaluate(source, '({value:editor.value,journal})')

        def denial(label):
            value = evaluate(source, '''(async()=>{
 const permission=(await navigator.permissions.query({name:'clipboard-read'})).state;
 const writePermission=(await navigator.permissions.query({name:'clipboard-write'})).state;
 const activation={active:navigator.userActivation.isActive,ever:navigator.userActivation.hasBeenActive};
 let read;try{await navigator.clipboard.readText();read='ALLOWED'}catch(e){read=e.name}
 let readItems;try{await navigator.clipboard.read();readItems='ALLOWED'}catch(e){readItems=e.name}
 journal=[];const legacy=document.execCommand('paste');
 return {permission,writePermission,activation,read,readItems,legacy,journal};})()''')
            report['checks'][label] = value
            assert value['permission']=='denied' and value['writePermission']=='denied', value
            assert value['read']!='ALLOWED' and value['readItems']!='ALLOWED' and not value['legacy'], value
            assert not any(e['type']=='paste' for e in value['journal']), value

        denial('unattended_before')
        sentinel = 'sentinel-private-clipboard'
        unicode = 'Native π 🐟\n第二行\n'
        seed(sentinel)
        prepare(unicode)
        native(source, 'Copy')
        copied = read_helper(unicode)
        report['checks']['first_copy'] = {'helper': copied, 'source_oracle': oracle()}
        assert copied['trusted']
        fresh = 'Fresh paste 🦀\nexact\n'
        seed(fresh)
        prepare('')
        native(source, 'Paste')
        pasted = oracle()
        report['checks']['first_paste'] = pasted
        assert pasted['value']==fresh and any(e['type']=='paste' and e['trusted'] for e in pasted['journal']), pasted
        report['first_vertical_slice'] = 'PASS'
        denial('activated_after_native_paste')

        for command in ['Copy', 'Cut', 'Paste']:
            for mode in ['default', 'cancel', 'custom']:
                seed(sentinel)
                initial = unicode if command != 'Paste' else 'replace-me'
                prepare(initial, mode, 'custom π\n')
                native(source, command)
                state = oracle()
                expected = (sentinel if mode=='cancel' else ('custom π\n' if mode=='custom' else unicode)) if command!='Paste' else sentinel
                clip = read_helper(expected)
                no_update = observe_unchanged(expected) if mode=='cancel' else None
                report['checks'][command+'_'+mode] = {'source_oracle': state, 'helper': clip, 'bounded_observation': no_update,
                    'no_update_interpretation': 'bounded sentinel observation, not proven cancellation' if mode=='cancel' else None}
                events = [e for e in state['journal'] if e['type']==command.lower()]
                assert events and all(e['trusted'] for e in events), state
                expected_value = initial
                if command=='Cut' and mode=='default':
                    expected_value=''
                if command=='Paste' and mode!='cancel':
                    expected_value = ('CUSTOM:' if mode=='custom' else '')+sentinel
                assert state['value']==expected_value, state
        for label, value, selected, mode, expected in [
            ('empty_custom_write', 'x', True, 'custom', ''),
            ('no_selection_no_write', 'x', False, 'default', sentinel)]:
            seed(sentinel)
            prepare(value, mode, '', selected)
            native(source, 'Copy')
            report['checks'][label] = {'helper': read_helper(expected), 'source_oracle': oracle(),
                                      'bounded_observation': observe_unchanged(expected)}
        boundary = 'π'*32768
        prepare(boundary)
        native(source, 'Copy')
        assert read_helper(boundary)['text'] == boundary
        report['checks']['utf8_64k_copy'] = {'bytes':len(boundary.encode()), 'exact': True}
        seed(boundary)
        prepare('')
        native(source, 'Paste')
        state = oracle()
        assert state['value']==boundary
        report['checks']['utf8_64k'] = {'bytes':len(boundary.encode()), 'exact': True}
        seed(sentinel)
        prepare(unicode)
        evaluate(source, 'cancelKey=true')
        native(source, 'Copy')
        blocked = oracle()
        assert not any(e['type']=='copy' for e in blocked['journal']), blocked
        report['checks']['keydown_prevent_default'] = {'source_oracle': blocked,
                                                       'bounded_observation': observe_unchanged(sentinel),
                                                       'native_menu_without_keydown': 'UNSUPPORTED by this demonstrated API'}
        denial('after_matrix')
        report['result']='PASS'
    except Exception as error:
        report['error'] = str(error)
        report['cause_if_unknown'] = 'UNDETERMINED; no fallback or permission grants attempted'
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if cdp:
            try:
                call('Browser.close')
            except Exception as error:
                report['close_error']=str(error)
        reaped = False
        if pid:
            until = time.monotonic()+3
            while time.monotonic()<until:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    report['exit_code']=os.waitstatus_to_exitcode(status)
                    reaped=True
                    break
                time.sleep(.03)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not reaped:
                _, status=os.waitpid(pid,0)
                report['exit_code']=os.waitstatus_to_exitcode(status)
                reaped=True
        if cdp:
            cdp.close()
        for fd in fds:
            os.close(fd)
        until=time.monotonic()+3
        remaining=private_processes(private)
        while remaining and time.monotonic()<until:
            time.sleep(.05)
            remaining=private_processes(private)
        report['cleanup']={'owned_pid_reaped': reaped, 'remaining_private_pids': remaining}
        os.close(tmp_fd)
        if remaining:
            report['result']='FAIL'
        journal.close()
        report['sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),root/'cdp.jsonl',root/'chromium.stderr',root/'sandbox.txt'] if p.exists()}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'directory':str(root),**report},ensure_ascii=False,indent=2))
    return 0 if report['result']=='PASS' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',type=Path,default=Path('/opt/google/chrome/chrome'))
    raise SystemExit(run(parser.parse_args().browser))

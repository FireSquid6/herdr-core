#!/usr/bin/env python3
"""Private experimental native-viewer worker. stdin is owned by core, never pages.

Chrome's CDP uses inherited pipes only. No HTTP/WS listener or arbitrary CDP RPC.
The matching bundled inspector is opened by Chrome's own chrome://inspect UI.
Its built-in host attachment uses no externally accessible debug endpoint.
"""
import base64
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time
import uuid

LIMIT = 4 * 1024 * 1024


# The feature binary prefixes the private CDP source; standalone tests import it.
if 'CDP' not in globals():
    from browser_cdp import CDP, Fault


def workspace(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise Fault('invalid_params', 'workspace must be an opaque ASCII identifier')
    return value


def navigation(url):
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 32 for c in url):
        raise Fault('invalid_params', 'invalid URL')
    if url != 'about:blank' and not re.match(r'^https?://[^/\s]+', url):
        raise Fault('invalid_params', 'only http, https and about:blank navigation supported')
    return url


def viewport(p):
    width, height, scale = p.get('width'), p.get('height'), p.get('scale', 1)
    if (type(width) is not int or type(height) is not int or not isinstance(scale, (float, int))
            or not math.isfinite(scale) or not .5 <= scale <= 3 or not 1 <= width <= 4096
            or not 1 <= height <= 4096 or width * scale > 4096 or height * scale > 4096
            or width * height * scale * scale > 16777216):
        raise Fault('invalid_params', 'viewport exceeds physical pixel limits')
    return width, height, scale


class Lease:
    def __init__(self):
        self.owner = None
        self.generation = None
        self.until = 0

    def acquire(self, identity, force, confirmed, now=None):
        now = time.monotonic() if now is None else now
        if force and not confirmed:
            raise Fault('invalid_params', 'force takeover requires explicit confirmation')
        if self.owner == identity and now < self.until:
            self.until = now + 30
            return self.generation
        if self.owner is not None and now < self.until and not (force and confirmed):
            raise Fault('owned', 'another native viewer owns this browser; confirmed takeover required')
        self.owner, self.generation, self.until = identity, uuid.uuid4().hex, now + 30
        return self.generation

    def check(self, identity, generation, now=None):
        now = time.monotonic() if now is None else now
        if self.owner != identity or now >= self.until:
            raise Fault('not_owner', 'viewer lease missing or expired; explicit acquire required')
        if self.generation != generation:
            raise Fault('stale_generation', 'discard pending work from previous controller')


class Browser:
    def __init__(self, root, executable):
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lease = Lease()
        self.state_lock = threading.RLock()
        self.local = threading.local()
        self.input_receipts, self.input_errors = {}, []
        self.sessions, self.tools, self.sizes, self.sequences = {}, {}, {}, {}
        self.selected = None
        self.revision = 0
        self.instance = uuid.uuid4().hex
        self.runtime_root = Path(tempfile.mkdtemp(prefix='hcbr.', dir='/var/tmp'))
        self.runtime_root.chmod(0o700)
        (root / 'runtime-path').write_text(str(self.runtime_root))
        env = os.environ.copy()
        for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK']:
            env.pop(key, None)
        for key in ['HOME', 'TMPDIR', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME']:
            path = (self.runtime_root if key in ('TMPDIR', 'XDG_RUNTIME_DIR') else root) / key.lower()
            path.mkdir(mode=0o700, exist_ok=True)
            env[key] = str(path)
        # No desktop bus/session, private HOME and no external helper lookup.
        # Chromium children use their absolute executable paths; external URI
        # handlers (xdg-open/gio/etc) cannot be launched through PATH.
        helpers = root / 'external-denied'
        helpers.mkdir(mode=0o700, exist_ok=True)
        env['PATH'] = str(helpers)
        profile = root / 'profile'
        profile.mkdir(mode=0o700, exist_ok=True)
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        source_in = fcntl.fcntl(input_read, fcntl.F_DUPFD_CLOEXEC, 10)
        source_out = fcntl.fcntl(output_write, fcntl.F_DUPFD_CLOEXEC, 10)
        fds = [input_read, input_write, output_read, output_write, source_in, source_out]
        with open(os.devnull, 'rb') as null, (root / 'chromium.stderr').open('ab') as err:
            actions = [(os.POSIX_SPAWN_DUP2, a, b) for a, b in [
                (null.fileno(), 0), (err.fileno(), 1), (err.fileno(), 2), (source_in, 3), (source_out, 4)]]
            actions += [(os.POSIX_SPAWN_CLOSE, fd) for fd in fds if fd not in (3, 4)]
            args = [executable, '--headless', f'--user-data-dir={profile}', '--remote-debugging-pipe',
                    '--no-first-run', '--no-default-browser-check', '--disable-background-timer-throttling', 'about:blank']
            try:
                self.pid = os.posix_spawn(executable, args, env, file_actions=actions, setsid=True)
            except Exception:
                os.close(input_write)
                os.close(output_read)
                shutil.rmtree(self.runtime_root)
                raise
            finally:
                for fd in [input_read, output_write, source_in, source_out]:
                    os.close(fd)
        self.cdp = CDP(output_read, input_write)
        try:
            self.version = self.cdp.call('Browser.getVersion')['product']
            for permission in ('geolocation', 'camera', 'microphone', 'notifications', 'clipboard-read', 'clipboard-write'):
                self.cdp.call('Browser.setPermission', {'permission': {'name': permission}, 'setting': 'denied'})
            # File export/chooser transport is not implemented in Stage A.
            self.cdp.call('Browser.setDownloadBehavior', {'behavior': 'deny'})
        except Exception:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
            self.cdp.close()
            self.cleanup_runtime()
            raise
        self.cdp.authorize = self.authorize
        self.running = True
        self.internal_targets = set()
        self.expiry_stop = threading.Event()
        threading.Thread(target=self.expire, daemon=True).start()

    def cleanup_runtime(self):
        # Never remove a directory while any Chromium child still uses it.
        needle = ('XDG_RUNTIME_DIR=' + str(self.runtime_root / 'xdg_runtime_dir')).encode()
        deadline = time.monotonic() + 2
        while True:
            live = []
            for path in Path('/proc').glob('[0-9]*/environ'):
                try:
                    if needle in path.read_bytes().split(b'\0'):
                        live.append(path.parent.name)
                except (OSError, ProcessLookupError):
                    pass
            if not live:
                shutil.rmtree(self.runtime_root)
                return True
            if time.monotonic() >= deadline:
                return False  # retain runtime-path evidence, never delete live sockets
            time.sleep(.02)

    @contextlib.contextmanager
    def authorize(self):
        # The lease lock spans authorization AND enqueue to the Chromium pipe.
        # It is released while awaiting CDP, allowing expiry during capture.
        with self.state_lock:
            if getattr(self.local, 'active', False):
                self.lease.check(self.local.identity, self.local.generation)
            yield

    @contextlib.contextmanager
    def cleanup(self):
        active = getattr(self.local, 'active', False)
        self.local.active = False
        try:
            yield
        finally:
            self.local.active = active

    def tabs(self):
        infos = self.cdp.call('Target.getTargets')['targetInfos']
        tools = {v['id'] for v in self.tools.values()}
        result = [{'id': t['targetId'], 'url': t['url'], 'title': t['title'],
                   **({'opener': t['openerId']} if t.get('openerId') else {})}
                  for t in infos if t['type'] == 'page' and t['targetId'] not in tools
                  and t['targetId'] not in self.internal_targets
                  and not t['url'].startswith(('devtools:', 'chrome:'))]
        if self.selected not in [t['id'] for t in result]:
            self.selected = result[0]['id'] if result else None
        self.revision += 1
        return {'selected': self.selected, 'tabs': result, 'revision': self.revision}

    def session(self, target):
        if target not in self.sessions:
            self.sessions[target] = self.cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        return self.sessions[target]

    def surface(self, p):
        surface = p.get('surface', {})
        target = surface.get('target')
        if target not in [t['id'] for t in self.tabs()['tabs']]:
            raise Fault('not_found', 'webpage target no longer exists')
        if surface.get('kind') == 'devtools':
            if target not in self.tools:
                raise Fault('not_found', 'DevTools is closed')
            return self.tools[target]['id']
        if surface.get('kind') != 'page':
            raise Fault('invalid_params', 'invalid surface kind')
        return target

    def expire(self):
        while not self.expiry_stop.wait(.1):
            if self.cdp.closed:
                return
            self.expire_due()

    def expire_due(self, now=None):
        with self.state_lock:
            now = time.monotonic() if now is None else now
            if self.lease.owner is not None and now >= self.lease.until:
                self.revoke()
                return True
            return False

    def open_tools(self, target):
        if target in self.tools:
            return
        # Instantiate a genuine Chrome-owned inspector, with native DevToolsHost
        # attachment. Ordinary Page.navigate(devtools://...) is not a valid way
        # to create this privileged UI (Chrome returns ERR_INVALID_URL).
        before = {t['targetId'] for t in self.cdp.call('Target.getTargets')['targetInfos']}
        helper = self.cdp.call('Target.createTarget', {'url': 'chrome://inspect/#pages'})['targetId']
        self.internal_targets.add(helper)
        helper_session = self.session(helper)
        generation = self.lease.generation
        created = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.lease.check(self.lease.owner, generation)
                ready_expression = "[...document.querySelectorAll('#pages-list .row')].some(r=>r.targetId===" + json.dumps(target) + ')'
                ready = self.cdp.call('Runtime.evaluate', {'expression': ready_expression, 'returnByValue': True}, helper_session)
                if ready.get('result', {}).get('value'):
                    break
            else:
                raise Fault('browser_failed', 'Chrome inspect UI did not initialize')
            self.cdp.call('Runtime.evaluate', {'expression': "chrome.send('inspect',['local'," + json.dumps(target) + '])'}, helper_session)
            while time.monotonic() < deadline:
                self.lease.check(self.lease.owner, generation)
                targets = self.cdp.call('Target.getTargets')['targetInfos']
                candidates = [t for t in targets if t['targetId'] not in before and t['url'].startswith('devtools://devtools/bundled/')]
                if candidates:
                    ident = candidates[0]['targetId']
                    created = ident
                    front = self.session(ident)
                    with self.state_lock:
                        self.lease.check(self.local.identity, generation)
                        self.tools[target] = {'id': ident, 'frontend': front, 'generation': generation}
                    # Undock inside headless Chrome; GPUI supplies the actual
                    # bottom/right docking. This prevents shrinking the webpage.
                    undock = "import('./ui/legacy/legacy.js').then(UI=>{let d=UI.DockController.DockController.instance();d.setDockSide('undocked');return d.dockSide();})"
                    while time.monotonic() < deadline:
                        self.lease.check(self.lease.owner, generation)
                        state = self.cdp.call('Runtime.evaluate', {'expression': undock, 'awaitPromise': True, 'returnByValue': True}, front)
                        if state.get('result', {}).get('value') == 'undocked':
                            return
                    raise Fault('browser_failed', 'DevTools frontend did not finish initializing')
            raise Fault('browser_failed', 'Chrome did not expose its DevTools frontend target')
        except Exception:
            with self.state_lock, self.cleanup():
                tool = self.tools.get(target)
                if tool and tool['generation'] == generation:
                    self.tools.pop(target)
                if created:
                    self.cdp.call('Target.closeTarget', {'targetId': created})
            raise
        finally:
            with self.cleanup():
                self.cdp.call('Target.closeTarget', {'targetId': helper})
            self.internal_targets.discard(helper)

    def revoke(self):
        with self.state_lock:
            self.local.active = False  # trusted cleanup, not a viewer command
            self.lease.owner = None
            self.lease.generation = None
            self.lease.until = 0
            self.close_frontends()

    def close_frontends(self):
        for tool in list(self.tools.values()):
            try:
                self.cdp.call('Target.closeTarget', {'targetId': tool['id']})
            except Fault:
                pass
        self.tools.clear()
        self.sequences.clear()
        self.discard_inputs()

    def discard_inputs(self):
        with self.state_lock:
            for ident in self.input_receipts:
                self.cdp.forget(ident)
            self.input_receipts.clear()
            self.input_errors.clear()

    def reap_inputs(self, now=None):
        with self.state_lock:
            now = time.monotonic() if now is None else now
            for ident, receipt in list(self.input_receipts.items()):
                kind = 'cdp_error'
                try:
                    response = receipt['answer'].get_nowait()
                except queue.Empty:
                    if now < receipt['deadline']:
                        continue
                    kind = 'timeout'
                    response = {'error': {'message': 'input ACK exceeded 30s; execution/effect unknown'}}
                self.input_receipts.pop(ident)
                self.cdp.forget(ident)
                if 'error' in response:
                    self.input_errors.append({'owner': receipt['owner'], 'generation': receipt['generation'],
                        'receipt': ident, 'kind': kind, 'message': str(response['error'])[:1024]})

    def heartbeat_receipts(self, identity, generation):
        with self.state_lock:
            self.lease.check(identity, generation)
            self.reap_inputs()
            errors = [{k: error[k] for k in ('receipt', 'kind', 'message')}
                      for error in self.input_errors
                      if error['owner'] == identity and error['generation'] == generation]
            self.input_errors.clear()
            return {'input_pending': len(self.input_receipts), 'input_errors': errors}

    def input_command(self, method, params, session):
        # Only input can await paused JS while its frontend must stay usable.
        # Record truthful pending ACKs, never silently turn timeout into success.
        with self.state_lock:
            self.lease.check(self.local.identity, self.local.generation)
            self.reap_inputs()
            if len(self.input_receipts) + len(self.input_errors) >= 64:
                raise Fault('slow_consumer', '64 unconsumed input receipts/errors; heartbeat to drain')
            ident, answer = self.cdp.begin(method, params, session, compact=True)
            self.input_receipts[ident] = {'owner': self.local.identity, 'generation': self.local.generation,
                'deadline': time.monotonic() + 30, 'answer': answer}
        try:
            response = answer.get(timeout=.05)
        except queue.Empty:
            return {'accepted': True, 'pending': True, 'receipt': ident}
        with self.state_lock:
            self.lease.check(self.local.identity, self.local.generation)
            self.input_receipts.pop(ident, None)
            self.cdp.forget(ident)
            if 'error' in response:
                raise Fault('browser_failed', str(response['error']))
        return {'accepted': True, 'pending': False, 'receipt': ident}

    def input(self, ident, event):
        session = self.session(ident)
        kind = event.get('type')
        mod = event.get('modifiers', 0)
        if type(mod) is not int or not 0 <= mod <= 15:
            raise Fault('invalid_params', 'invalid modifiers')
        if kind in ('pointer', 'wheel'):
            for key in ['x', 'y'] + (['dx', 'dy'] if kind == 'wheel' else []):
                value = event.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 100000:
                    raise Fault('invalid_params', 'invalid pointer coordinate')
            params = {'x': event['x'], 'y': event['y'], 'modifiers': mod}
            if kind == 'wheel':
                params.update(type='mouseWheel', deltaX=event['dx'], deltaY=event['dy'])
            else:
                action = {'move': 'mouseMoved', 'down': 'mousePressed', 'up': 'mouseReleased'}.get(event.get('action'))
                if action is None or event.get('button') not in ('none', 'left', 'middle', 'right'):
                    raise Fault('invalid_params', 'invalid pointer action')
                params.update(type=action, button=event['button'], buttons=event.get('buttons', 0), clickCount=event.get('click_count', 1))
            return self.input_command('Input.dispatchMouseEvent', params, session)
        elif kind == 'text':
            text = event.get('text')
            if not isinstance(text, str) or len(text.encode()) > 65536:
                raise Fault('invalid_params', 'text too large')
            return self.input_command('Input.insertText', {'text': text}, session)
        elif kind == 'key':
            key, code = event.get('key', ''), event.get('code', '')
            if any(not isinstance(v, str) or len(v.encode()) > 128 for v in [key, code]):
                raise Fault('invalid_params', 'key/code too large')
            action = {'down': 'keyDown', 'up': 'keyUp'}.get(event.get('action'))
            if action is None:
                raise Fault('invalid_params', 'invalid key action')
            vk = {'Enter': 13, 'Tab': 9, 'Backspace': 8, 'Escape': 27, 'Delete': 46,
                  'ArrowLeft': 37, 'ArrowUp': 38, 'ArrowRight': 39, 'ArrowDown': 40,
                  'Home': 36, 'End': 35, 'PageUp': 33, 'PageDown': 34,
                  'Shift': 16, 'Control': 17, 'Alt': 18, 'Meta': 91,
                  'CapsLock': 20, 'Insert': 45}.get(key, 0)
            if len(key) == 1 and ord(key) < 128:
                vk = ord(key.upper()) if key.isalpha() else ord(key)
            if re.fullmatch(r'F(?:[1-9]|1[0-9]|2[0-4])', key):
                vk = 111 + int(key[1:])
            params = {'type': action, 'key': key, 'code': code, 'modifiers': mod,
                      'autoRepeat': bool(event.get('repeat', False)), 'windowsVirtualKeyCode': vk}
            if action == 'keyDown' and key == 'Enter':
                params['text'] = '\r'
            return self.input_command('Input.dispatchKeyEvent', params, session)
        else:
            raise Fault('invalid_params', 'unsupported input event')


class Worker:
    def __init__(self, root, executable):
        self.root, self.executable = Path(root), executable
        self.browsers = {}

    def dispatch(self, identity, method, p):
        if method == 'disconnect':
            for browser in self.browsers.values():
                if browser.lease.owner == identity:
                    browser.revoke()
            return {}
        name = workspace(p.get('workspace'))
        browser = self.browsers.get(name)
        if method == 'browser.ensure':
            recovery = 'live'
            if browser is None or not browser.running or browser.cdp.closed:
                if len([b for b in self.browsers.values() if b.running]) >= 8:
                    raise Fault('browser_failed', 'experimental worker limit: 8 browsers')
                browser = Browser(self.root / name, self.executable)
                self.browsers[name] = browser
                recovery = 'new'
            return {'state': 'running', 'recovery': recovery, 'instance': browser.instance,
                    'pid': browser.pid, 'version': browser.version}
        if method == 'browser.status':
            return {'state': 'running', 'pid': browser.pid} if browser and browser.running and not browser.cdp.closed else {'state': 'stopped'}
        if browser is None or not browser.running or browser.cdp.closed:
            raise Fault('not_running', 'open the browser explicitly first')
        if method == 'browser.acquire':
            with browser.state_lock:
                old = browser.lease.generation
                generation = browser.lease.acquire(identity, p.get('force') is True, p.get('confirmed') is True)
                if old != generation:
                    browser.close_frontends()
                return {'generation': generation, 'lease_ms': 30000}
        with browser.state_lock:
            browser.lease.check(identity, p.get('generation'))
            generation = browser.lease.generation
        browser.local.active = True
        browser.local.identity, browser.local.generation = identity, generation
        try:
            result = self.owned(browser, identity, method, p)
            if method not in ('browser.release', 'browser.stop'):
                with browser.state_lock:
                    browser.lease.check(identity, generation)
            return result
        finally:
            browser.local.active = False

    def owned(self, b, identity, method, p):
        if method == 'browser.heartbeat':
            with b.state_lock:
                b.lease.check(identity, p.get('generation'))
                b.lease.until = time.monotonic() + 30
                return {'lease_ms': 30000, **b.heartbeat_receipts(identity, p.get('generation'))}
        if method == 'browser.release':
            b.revoke()
            return {'released': True}
        if method == 'browser.tabs':
            return b.tabs()
        if method == 'browser.tab.create':
            url = navigation(p.get('url', 'about:blank'))
            ident = b.cdp.call('Target.createTarget', {'url': url})['targetId']
            b.selected = ident
            return {'id': ident, 'url': url, 'title': ''}
        if method in ('browser.viewport', 'browser.frame', 'browser.input'):
            ident = b.surface(p)
            session = b.session(ident)
            if method == 'browser.viewport':
                width, height, scale = viewport(p)
                b.cdp.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height,
                           'deviceScaleFactor': scale, 'mobile': False}, session)
                b.sizes[ident] = (width, height, scale)
                return {'width': width, 'height': height, 'scale': scale}
            if method == 'browser.input':
                return b.input(ident, p.get('event', {}))
            old = b.sequences.get(ident, 0)
            if p.get('ack', 0) != old:
                raise Fault('invalid_params', 'frame requires exact previous consumed sequence ACK')
            image = b.cdp.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False}, session)
            if len(image['data']) > LIMIT - 1024:
                raise Fault('browser_failed', 'PNG exceeds 4MiB wire bound; reduce viewport')
            header = base64.b64decode(image['data'][:44])
            if header[:8] != b'\x89PNG\r\n\x1a\n':
                raise Fault('browser_failed', 'invalid PNG')
            width, height = struct.unpack('>II', header[16:24])
            if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 16777216:
                raise Fault('browser_failed', 'PNG physical bound exceeded')
            b.sequences[ident] = old + 1
            return {'generation': b.lease.generation, 'sequence': old + 1, 'event': {
                'type': 'frame', 'data': image['data'], 'width': width, 'height': height,
                'scale': b.sizes.get(ident, (0, 0, 1))[2], 'timestamp_ms': int(time.monotonic() * 1000)}}
        if method == 'browser.stop':
            if p.get('confirmed') is not True:
                raise Fault('invalid_params', 'stop requires explicit confirmation')
            b.revoke()
            b.cdp.call('Browser.close')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pid, status = os.waitpid(b.pid, os.WNOHANG)
                if pid:
                    b.running = False
                    b.expiry_stop.set()
                    b.cdp.close()
                    b.cleanup_runtime()
                    self.browsers.pop(p['workspace'], None)
                    return {'state': 'stopped'}
                time.sleep(.01)
            raise Fault('browser_failed', 'Chrome did not exit within stop deadline')
        target = p.get('target')
        if target not in [t['id'] for t in b.tabs()['tabs']]:
            raise Fault('not_found', 'page target no longer exists')
        if method == 'browser.tab.select':
            b.selected = target
            return {'selected': target}
        if method == 'browser.tab.close':
            return {'closed': b.cdp.call('Target.closeTarget', {'targetId': target})['success']}
        if method == 'browser.devtools.open':
            b.open_tools(target)
            return {'surface': {'kind': 'devtools', 'target': target}, 'docking': 'bottom'}
        if method == 'browser.devtools.close':
            tool = b.tools.pop(target, None)
            if tool:
                b.cdp.call('Target.closeTarget', {'targetId': tool['id']})
            return {'closed': tool is not None}
        session = b.session(target)
        if method == 'browser.navigate':
            b.cdp.call('Page.navigate', {'url': navigation(p.get('url'))}, session)
        elif method == 'browser.reload':
            b.cdp.call('Page.reload', session=session)
        elif method == 'browser.history':
            history = b.cdp.call('Page.getNavigationHistory', session=session)
            direction = p.get('direction')
            if direction not in ('back', 'forward'):
                raise Fault('invalid_params', 'direction must be back or forward')
            index = history['currentIndex'] + (-1 if direction == 'back' else 1)
            if not 0 <= index < len(history['entries']):
                return {'accepted': False}
            b.cdp.call('Page.navigateToHistoryEntry', {'entryId': history['entries'][index]['id']}, session)
        else:
            raise Fault('unsupported_capability', 'operation not implemented')
        return {'accepted': True}


def main():
    os.umask(0o077)
    if os.geteuid() == 0:
        raise RuntimeError('Chromium worker must not run as root')
    worker = Worker(sys.argv[1], sys.argv[2])
    while line := sys.stdin.buffer.readline(131073):
        if len(line) > 131072 or not line.endswith(b'\n'):
            break
        try:
            request = json.loads(line)
            result = worker.dispatch(request['identity'], request['method'], request.get('params', {}))
            response = {'result': result}
        except Fault as error:
            response = {'error': {'kind': error.kind, 'message': str(error)}}
        except Exception as error:
            response = {'error': {'kind': 'browser_failed', 'message': str(error)}}
        data = json.dumps(response, separators=(',', ':'))
        if len(data.encode()) > LIMIT:
            data = json.dumps({'error': {'kind': 'browser_failed', 'message': 'response exceeds bound'}})
        print(data, flush=True)
    # Core exit currently terminates its pipe browser children (no restart recovery
    # claim). Owner disconnect does not close this worker's stdin or Chromium pipe.
    for browser in worker.browsers.values():
        if browser.running:
            try:
                browser.cdp.call('Browser.close')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waited, _ = os.waitpid(browser.pid, os.WNOHANG)
                    if waited:
                        browser.running = False
                        browser.expiry_stop.set()
                        browser.cdp.close()
                        browser.cleanup_runtime()
                        break
                    time.sleep(.02)
                if browser.running:
                    # Only this worker's explicitly created Chrome process group.
                    os.killpg(browser.pid, signal.SIGKILL)
                    os.waitpid(browser.pid, 0)
                    browser.cdp.close()
                    browser.cleanup_runtime()
            except Exception:
                pass


if __name__ == '__main__':
    main()

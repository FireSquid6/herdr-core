"""Private native plaintext editing. Never reads source DOM or OS clipboard.

The hidden helper owns a separate ephemeral Chrome context. Native key commands
are cancellable keydowns, not an independently demonstrated native-menu API.
"""
import threading
import time
import uuid

if 'Fault' not in globals():
    from browser_cdp import Fault
if 'native_focus' not in globals():
    from browser_focus import native_focus, FocusUnavailable

TEXT_LIMIT = 65536
_HELPER = '''document.body.innerHTML='<textarea id="dummy">x</textarea>';
window.clipboardPayload='';window.clipboardObservation=null;
document.addEventListener('copy',e=>{
 e.clipboardData.setData('text/plain',clipboardPayload);e.preventDefault();
});
document.addEventListener('paste',e=>{
 clipboardObservation={text:e.clipboardData.getData('text/plain'),types:[...e.clipboardData.types],trusted:e.isTrusted};
 e.preventDefault();
});
'''
_PREPARE = '''function(text){this.clipboardPayload=text;this.clipboardObservation=null;
this.document.getElementById('dummy').value='x';
this.document.getElementById('dummy').focus();this.document.getElementById('dummy').select();}'''
_READ = 'function(){return this.clipboardObservation;}'


def validate_clipboard(params):
    operation = params.get('operation')
    if operation not in ('copy', 'cut', 'paste'):
        raise Fault('invalid_params', 'operation must be copy, cut or paste')
    if not isinstance(params.get('target'), str) or not params['target']:
        raise Fault('invalid_params', 'source target required')
    if operation == 'paste':
        text = params.get('text')
        if not isinstance(text, str):
            raise Fault('invalid_params', 'paste requires fresh plaintext, including an explicit empty string')
        try:
            valid = len(text.encode('utf-8')) <= TEXT_LIMIT
        except UnicodeError:
            valid = False
        if not valid:
            raise Fault('invalid_params', 'plaintext exceeds 64KiB UTF-8 or contains invalid Unicode')
    elif 'text' in params:
        raise Fault('invalid_params', 'text is only accepted for paste')


class EditFailure(Exception):
    pass


class BrowserEdit:
    def __init__(self, browser):
        self.browser = browser
        self.lock = threading.Lock()
        self.epoch = 0
        self.poisoned = False
        self.context = self.target = self.session = self.object_id = None

    def invalidate(self):
        # Never wait for IO/operation lock here: expiry must be able to revoke
        # while native ACKs are outstanding. state_lock is reentrant for revoke.
        with self.browser.state_lock:
            self.epoch += 1

    def close(self):
        b = self.browser
        self.invalidate()
        self.poisoned = True
        context, target = self.context, self.target
        if context:
            with b.cleanup():
                b.cdp.call('Target.disposeBrowserContext', {'browserContextId': context})
            if target:
                b.internal_targets.discard(target)
        # Keep private identities hidden if disposal fails or its ACK is lost.
        self.context = self.target = self.session = self.object_id = None

    def helper(self):
        if self.session:
            return
        b = self.browser
        try:
            self.context = b.cdp.call('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
            self.target = b.cdp.call('Target.createTarget', {'url': 'about:blank', 'browserContextId': self.context})['targetId']
            b.internal_targets.add(self.target)
            info = b.cdp.call('Target.getTargetInfo', {'targetId': self.target})['targetInfo']
            if info.get('browserContextId') != self.context or info.get('type') != 'page':
                raise EditFailure('private helper identity mismatch')
            self.session = b.cdp.call('Target.attachToTarget', {'targetId': self.target, 'flatten': True})['sessionId']
            reply = b.cdp.call('Runtime.evaluate', {'expression': _HELPER}, self.session)
            if 'exceptionDetails' in reply:
                raise EditFailure('private helper initialization failed')
            reply = b.cdp.call('Runtime.evaluate', {'expression': 'globalThis'}, self.session)
            self.object_id = reply['result']['objectId']
        except Exception:
            self.poisoned = True
            raise

    def function(self, function, args=()):
        reply = self.browser.cdp.call('Runtime.callFunctionOn', {
            'objectId': self.object_id, 'functionDeclaration': function,
            'arguments': [{'value': arg} for arg in args], 'returnByValue': True}, self.session)
        if 'exceptionDetails' in reply:
            raise EditFailure('private helper execution failed')
        return reply.get('result', {}).get('value')

    def native(self, session, command):
        cdp = self.browser.cdp
        # Unlike ordinary input receipts, clipboard commands wait for actual ACK.
        cdp.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'F24', 'code': 'F24',
                 'windowsVirtualKeyCode': 135, 'commands': [command]}, session)
        cdp.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'F24', 'code': 'F24',
                 'windowsVirtualKeyCode': 135}, session)

    def observe(self):
        self.function(_PREPARE, ('',))
        self.native(self.session, 'Paste')
        value = self.function(_READ)
        if (not isinstance(value, dict) or value.get('trusted') is not True
                or not isinstance(value.get('text'), str) or not isinstance(value.get('types'), list)):
            raise EditFailure('native helper paste was not observed')
        try:
            valid = len(value['text'].encode('utf-8')) <= TEXT_LIMIT
        except UnicodeError:
            valid = False
        if not valid:
            raise EditFailure('clipboard plaintext exceeds 64KiB UTF-8')
        return value

    def seed(self, text):
        self.function(_PREPARE, (text,))
        self.native(self.session, 'Copy')
        deadline = time.monotonic() + .5
        while True:
            value = self.observe()
            if value['text'] == text and ('text/plain' in value['types'] or (text == '' and not value['types'])):
                return
            if time.monotonic() >= deadline:
                raise EditFailure('fresh helper seed not observed; source command was not dispatched')
            time.sleep(.015)

    def stamp(self, session):
        return native_focus(self.browser, session)

    def run(self, identity, generation, params, guard=None):
        b = self.browser
        with self.lock:
            if self.poisoned:
                return {'kind': 'failed', 'message': 'editing is poisoned; explicitly stop and restart this browser'}
            source_dispatched = False
            try:
                target = params['target']
                session = b.session(target)
                instance, stamp = b.instance, self.stamp(session)
                with b.state_lock:
                    b.lease.check(identity, generation)
                    epoch = self.epoch
                self.helper()
                operation = params['operation']
                sentinel = 'herdr-private-sentinel-' + uuid.uuid4().hex
                self.seed(params['text'] if operation == 'paste' else sentinel)
                if guard:
                    guard()
                # A document change during helper setup must not edit its successor.
                if self.stamp(session) != stamp:
                    raise EditFailure('source focus or focused document changed before native dispatch')
                with b.state_lock:
                    b.lease.check(identity, generation)
                    if b.instance != instance or self.epoch != epoch:
                        raise EditFailure('editing capability was revoked')
                source_dispatched = True
                self.native(session, operation.capitalize())  # Cut is dispatched ONCE.
                if operation == 'paste':
                    result = {'kind': 'paste-dispatched'}
                else:
                    deadline = time.monotonic() + .5
                    while True:
                        value = self.observe()
                        types, text = value['types'], value['text']
                        if types and 'text/plain' not in types:
                            raise EditFailure('unsupported nontext clipboard projection')
                        if text != sentinel and (text or types):
                            result = {'kind': 'copied', 'text': text}
                            break
                        if time.monotonic() >= deadline:
                            # Empty/no-formats is a real empty projection, unlike
                            # an unchanged fresh sentinel. Neither proves cancel.
                            result = ({'kind': 'copied', 'text': ''} if not types and not text
                                      else {'kind': 'no-update-observed'})
                            break
                        time.sleep(.015)
                if self.stamp(session) != stamp:
                    raise EditFailure('source focus or focused document changed; pending export discarded')
                with b.state_lock:
                    b.lease.check(identity, generation)
                    if b.instance != instance or self.epoch != epoch:
                        raise EditFailure('editing capability changed; pending export discarded')
                if guard:
                    guard()
                return result
            except (EditFailure, FocusUnavailable) as error:
                # Known unsupported/changed-document outcomes disclose no text.
                result = self.failed(source_dispatched)
                result['message'] = str(error) + '. ' + result['message']
                if self.poisoned:
                    result['message'] += ' Editing is poisoned; stop/restart browser before retry.'
                return result
            except Fault as error:
                if error.kind in ('not_owner', 'stale_generation'):
                    raise
                # Ambiguous CDP loss/timeout cannot safely reuse clipboard state.
                self.poisoned = True
                result = self.failed(source_dispatched)
                result['message'] += ' Editing is poisoned; stop/restart browser before retry.'
                return result
            except Exception:
                self.poisoned = True
                result = self.failed(source_dispatched)
                result['message'] += ' Editing is poisoned; stop/restart browser before retry.'
                return result
            except BaseException:
                self.poisoned = True
                raise

    @staticmethod
    def failed(source_dispatched):
        return {'kind': 'failed', 'message': (
            'Editing did not complete. '
            + ('Native editing may already have occurred; do not automatically retry or undo Cut.'
               if source_dispatched else 'No source edit command was dispatched.'))}

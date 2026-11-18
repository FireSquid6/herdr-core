"""Trusted native menu capabilities; source inspection is readonly metadata only."""
import math
import time
import uuid
from urllib.parse import urlsplit

if 'BrowserEdit' not in globals():
    from browser_edit import BrowserEdit, EditFailure, validate_clipboard
if 'Fault' not in globals():
    from browser_cdp import Fault
if 'native_focus' not in globals():
    from browser_focus import native_focus, frame_documents, FocusUnavailable

_CONTEXT_QUERY = '''function(){
 let n=this.nodeType===1?this:this.parentElement;
 let a=n;while(a && !(a.localName==='a' && a.hasAttribute('href')))a=a.parentElement;
 const field=!!n && (n.localName==='textarea' || n.localName==='input');
 const password=field && n.localName==='input' && n.type==='password';
 const disabled=field && n.matches(':disabled'), readOnly=field && n.readOnly===true;
 const textual=field && (n.localName==='textarea' || ['text','search','url','tel','email','password'].includes(n.type));
 const editable=!!n && (textual || n.isContentEditable===true);
 const start=textual?n.selectionStart:null,end=textual?n.selectionEnd:null;
 const selection_present=(typeof start==='number' && typeof end==='number')
   ?start!==end:!!n && !n.ownerDocument.defaultView.getSelection().isCollapsed;
 const can_copy=selection_present && !password && !disabled;
 const focused=n && n.ownerDocument.activeElement;
 const _focus_matches_hit=!!n && !!focused && (n===focused || n.contains(focused) ||
   focused.isContentEditable && focused.contains(n) || !editable && focused.localName==='body' && focused.contains(n));
 return {_focus_matches_hit,link:a?a.href:null,editable,password,read_only:readOnly,disabled,selection_present,
 can_copy,can_cut:can_copy && editable && !readOnly,can_paste:editable && !disabled && !readOnly};
}'''
_ACTIONS = ('copy', 'cut', 'paste', 'copy_link', 'open_link', 'new_tab', 'select_all')
_BOOL_FIELDS = ('editable', 'password', 'read_only', 'disabled', 'selection_present', 'can_copy', 'can_cut', 'can_paste')


class StaleContext(EditFailure):
    pass


def invalidate_context(browser):
    if hasattr(browser, 'contexts'):
        browser.contexts.invalidate()


def context_event(browser, method, params):
    invalidate = method in ('browser.navigate','browser.reload','browser.history','browser.tab.select',
                            'browser.tab.create','browser.tab.close','browser.stop','browser.clipboard')
    if method == 'browser.input':
        event = params.get('event', {})
        invalidate = (event.get('type') == 'wheel' or
                      event.get('type') in ('pointer','key') and event.get('action') == 'down')
    if method == 'browser.viewport':
        surface = params.get('surface', {})
        invalidate = (surface.get('kind') == 'page' and
                      browser.sizes.get(surface.get('target')) !=
                      (params.get('width'),params.get('height'),params.get('scale',1)))
    if invalidate:
        invalidate_context(browser)


def context_dispatch(browser, identity, method, params, navigation):
    if method == 'browser.context':
        surface = params.get('surface')
        if not isinstance(surface, dict) or surface.get('kind') != 'page' or not isinstance(surface.get('target'), str):
            raise Fault('invalid_params', 'context requires a public page surface')
        for name in ('x', 'y'):
            value = params.get(name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 4096:
                raise Fault('invalid_params', 'context coordinates must be finite viewport pixels in [0,4096)')
        if surface['target'] not in [t['id'] for t in browser.tabs()['tabs']]:
            raise Fault('not_found', 'public source page not found')
    else:
        action = params.get('action')
        if action not in _ACTIONS:
            raise Fault('invalid_params', 'unsupported context action')
        if action in ('copy', 'cut', 'paste'):
            validate_clipboard(dict(params, operation=action))
        elif 'text' in params:
            raise Fault('invalid_params', 'text is only accepted for paste')
        if not isinstance(params.get('target'), str) or not isinstance(params.get('token'), str):
            raise Fault('invalid_params', 'target and one-shot token required')
        if not hasattr(browser, 'contexts'):
            raise Fault('stale_context', 'context capability is missing or expired')
    if not hasattr(browser, 'contexts'):
        browser.contexts = BrowserContexts(browser)
    contexts = browser.contexts
    if method == 'browser.context':
        return contexts.query(identity, params)
    return contexts.action(identity, params, navigation)


class BrowserContexts:
    def __init__(self, browser):
        self.browser = browser
        self.tokens = {}
        self.epoch = 0

    def invalidate(self):
        with self.browser.state_lock:
            self.epoch += 1
            self.tokens.clear()

    def documents(self, session):
        return frame_documents(self.browser, session)

    def focus(self, session):
        try:
            return native_focus(self.browser, session)
        except FocusUnavailable:
            return None  # Link actions need not target the focused widget.

    def edit_guard(self, record):
        self.check(record)
        if (record['focus'] is None or not record['focus_matches_hit']
                or record['focus'].frame != record['frame']
                or native_focus(self.browser, record['session']) != record['focus']):
            raise StaleContext('source focus does not match the menu hit or changed; edit/export rejected')

    def check(self, record):
        b = self.browser
        with b.state_lock:
            b.lease.check(record['identity'], record['generation'])
            if (record['epoch'] != self.epoch or b.instance != record['instance']
                    or time.monotonic() >= record['until']):
                raise StaleContext('context capability changed or expired')
        if record['target'] not in [t['id'] for t in b.tabs()['tabs']]:
            raise StaleContext('source page closed or no longer public')
        frames, root = self.documents(record['session'])
        if (root != record['root'] or frames.get(root) != record['root_loader']
                or frames.get(record['frame']) != record['loader']):
            raise StaleContext('source frame document changed; menu action/export discarded')
        with b.state_lock:
            b.lease.check(record['identity'], record['generation'])
            if (record['epoch'] != self.epoch or b.instance != record['instance']
                    or time.monotonic() >= record['until']):
                raise StaleContext('context capability revoked during validation')

    def query(self, identity, p):
        b = self.browser
        target = p['surface']['target']
        session = b.session(target)
        size = b.sizes.get(target, (4096,4096,1))
        if p['x'] >= size[0] or p['y'] >= size[1]:
            raise Fault('invalid_params', 'context point outside current viewport')
        with b.state_lock:
            b.lease.check(identity, p['generation'])
            epoch, instance = self.epoch, b.instance
        try:
            before, root = self.documents(session)
        except FocusUnavailable:
            raise Fault('unsupported_capability', 'native frame inspection unavailable; no fallback') from None
        if root not in before:
            raise Fault('unsupported_capability', 'main document loader identity unavailable')
        object_id = None
        try:
            hit = b.cdp.call('DOM.getNodeForLocation', {'x': int(p['x']), 'y': int(p['y'])}, session)
            frame, backend = hit.get('frameId'), hit.get('backendNodeId')
            if frame not in before or type(backend) is not int or backend <= 0:
                raise Fault('unsupported_capability', 'hit frame is unavailable in this page session')
            node = b.cdp.call('DOM.describeNode', {'backendNodeId':backend,'depth':0}, session)['node']
            if node.get('nodeName') in ('IFRAME','FRAME'):
                raise Fault('unsupported_capability', 'iframe owner hit requires unsupported child-frame routing')
            world = b.cdp.call('Page.createIsolatedWorld', {'frameId':frame, 'worldName':'herdr-readonly-context'}, session)
            resolved = b.cdp.call('DOM.resolveNode', {'backendNodeId':backend, 'executionContextId':world['executionContextId']}, session)
            object_id = resolved['object'].get('objectId')
            if not object_id:
                raise Fault('unsupported_capability', 'hit node could not be resolved in its isolated frame')
            focus_before = self.focus(session)
            answer = b.cdp.call('Runtime.callFunctionOn', {'objectId':object_id,
                'functionDeclaration':_CONTEXT_QUERY, 'returnByValue':True}, session)
            metadata = answer.get('result', {}).get('value')
            if 'exceptionDetails' in answer or not isinstance(metadata, dict):
                raise Fault('unsupported_capability', 'readonly context inspection failed')
            if (set(metadata) != {'link', '_focus_matches_hit', *_BOOL_FIELDS}
                    or any(type(metadata.get(k)) is not bool for k in (*_BOOL_FIELDS, '_focus_matches_hit'))):
                raise Fault('unsupported_capability', 'invalid context metadata')
            focus_matches_hit = metadata.pop('_focus_matches_hit')
            focus_after = self.focus(session)
            if focus_before != focus_after:
                raise StaleContext('native source focus changed during menu inspection')
            link = metadata['link']
            try:
                valid_link = link is None or (isinstance(link, str) and len(link.encode('utf-8')) <= 16384
                                              and not any(ord(c)<32 for c in link))
            except UnicodeError:
                valid_link = False
            if not valid_link:
                raise Fault('unsupported_capability', 'link metadata exceeds safe bounds')
            record = {'identity':identity, 'generation':p['generation'], 'instance':instance,
                      'epoch':epoch, 'target':target, 'session':session, 'frame':frame,
                      'loader':before[frame], 'root':root, 'root_loader':before.get(root),
                      'until':time.monotonic()+10, 'metadata':metadata,
                      'focus':focus_after, 'focus_matches_hit':focus_matches_hit}
            self.check(record)
            token = uuid.uuid4().hex
            with b.state_lock:
                b.lease.check(identity, p['generation'])
                if self.epoch != epoch:
                    raise StaleContext('context was revoked before publication')
                now = time.monotonic()
                self.tokens = {k:v for k,v in self.tokens.items() if v['until']>now}
                while len(self.tokens)>=16:
                    self.tokens.pop(next(iter(self.tokens)))
                self.tokens[token] = record
            return {'token':token, 'target':target, 'document':{'frame':frame,'loader':before[frame]}, **metadata}
        except FocusUnavailable:
            raise Fault('unsupported_capability', 'native frame inspection unavailable; no fallback') from None
        except StaleContext as error:
            raise Fault('stale_context', str(error)) from None
        except Fault as error:
            if error.kind in ('not_owner','stale_generation','unsupported_capability'):
                raise
            raise Fault('unsupported_capability', 'native hit/frame inspection unavailable; no fallback') from None
        finally:
            if object_id:
                with b.cleanup():
                    try:
                        b.cdp.call('Runtime.releaseObject', {'objectId':object_id}, session)
                    except Fault:
                        pass

    def action(self, identity, p, navigation):
        b = self.browser
        with b.state_lock:
            record = self.tokens.get(p['token'])
            if (not record or record['identity'] != identity or record['generation'] != p['generation']
                    or record['target'] != p['target'] or record['instance'] != b.instance
                    or record['until'] <= time.monotonic() or record['epoch'] != self.epoch):
                raise Fault('stale_context', 'context token is stale, foreign, expired or already consumed')
            self.tokens.pop(p['token'])
        action, metadata = p['action'], record['metadata']
        source_dispatched = False
        try:
            self.check(record)
            if action in ('copy','cut','paste'):
                if not metadata['can_'+action]:
                    raise Fault('invalid_params', 'menu metadata does not permit this edit action')
                if not hasattr(b,'edit'):
                    b.edit = BrowserEdit(b)
                self.edit_guard(record)
                return b.edit.run(identity, p['generation'], dict(p,operation=action), guard=lambda:self.edit_guard(record))
            if action == 'copy_link':
                if metadata['link'] is None:
                    raise Fault('invalid_params', 'context has no link')
                self.check(record)
                return {'kind':'link','text':metadata['link']}
            if action in ('open_link','new_tab'):
                link = metadata['link']
                try:
                    parsed = urlsplit(link) if isinstance(link,str) else None
                    valid = parsed is not None and parsed.scheme in ('http','https') and bool(parsed.hostname)
                except ValueError:
                    valid = False
                if not valid:
                    raise Fault('invalid_params', 'link action requires a safe HTTP(S) destination')
                url = navigation(link)
                self.check(record)
                if action == 'open_link':
                    b.cdp.call('Page.navigate', {'url':url}, record['session'])
                    result = {'kind':'action-dispatched','action':action}
                else:
                    info = b.cdp.call('Target.getTargetInfo', {'targetId':record['target']})['targetInfo']
                    args = {'url':url,'background':True}
                    if info.get('browserContextId'):
                        args['browserContextId'] = info['browserContextId']
                    self.check(record)
                    target = b.cdp.call('Target.createTarget', args)['targetId']
                    result = {'kind':'action-dispatched','action':action,'target':target,'opener_preserved':False}
                    if 'group' in p:result['url']=url
                self.invalidate()
                with b.state_lock:
                    b.lease.check(identity,p['generation'])
                return result
            if not hasattr(b,'edit'):
                b.edit = BrowserEdit(b)
            with b.edit.lock:
                if b.edit.poisoned:
                    return {'kind':'failed','message':'editing is poisoned; stop/restart browser'}
                try:
                    self.edit_guard(record)
                    source_dispatched = True
                    b.edit.native(record['session'],'SelectAll')
                    self.edit_guard(record)
                except Fault as error:
                    if error.kind not in ('not_owner','stale_generation'):
                        b.edit.poisoned = True
                    raise
                return {'kind':'action-dispatched','action':'select_all'}
        except (StaleContext, FocusUnavailable) as error:
            if action not in ('copy','cut','paste','select_all'):
                raise Fault('stale_context', str(error)) from None
            return {'kind':'failed','message':str(error) + ('. Native editing may already have occurred; never automatically retry/undo Cut'
                if source_dispatched else '. No source edit command was dispatched.')}

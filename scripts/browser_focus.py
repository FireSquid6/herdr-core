"""Bounded readonly native focus identity; never reads source text or values."""
from collections import namedtuple

if 'Fault' not in globals():
    from browser_cdp import Fault


class FocusUnavailable(Exception):
    pass


FocusStamp = namedtuple('FocusStamp', 'root root_loader frame loader backend chain')


def _focus_call(browser, method, params=None, session=None):
    try:
        result = browser.cdp.call(method, params, session)
        if not isinstance(result, dict):
            raise FocusUnavailable('invalid native focus inspection response')
        return result
    except Fault as error:
        if (error.kind in ('not_owner', 'stale_generation') or
                'timed out' in str(error) or 'pipe closed' in str(error)):
            raise
        raise FocusUnavailable('native frame/focus inspection unavailable; no fallback') from None


def frame_documents(browser, session):
    tree = _focus_call(browser, 'Page.getFrameTree', session=session).get('frameTree')
    if not isinstance(tree, dict):
        raise FocusUnavailable('native frame tree unavailable')
    documents, pending = {}, [(tree, 0)]
    root = None
    while pending:
        node, depth = pending.pop()
        if depth >= 32 or len(documents) >= 64 or not isinstance(node, dict):
            raise FocusUnavailable('native frame tree exceeds bounded inspection')
        frame = node.get('frame', {})
        if not isinstance(frame, dict):
            raise FocusUnavailable('native frame identity unavailable')
        frame_id, loader = frame.get('id'), frame.get('loaderId')
        if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (frame_id, loader)):
            raise FocusUnavailable('native frame loader identity unavailable')
        if frame_id in documents:
            raise FocusUnavailable('duplicate native frame identity')
        if root is None:
            root = frame_id
        documents[frame_id] = loader
        children = node.get('childFrames', [])
        if not isinstance(children, list) or len(children) + len(pending) + len(documents) > 64:
            raise FocusUnavailable('native frame tree exceeds bounded inspection')
        pending.extend((child, depth+1) for child in children)
    return documents, root


def native_focus(browser, session):
    documents, root = frame_documents(browser, session)
    frame, chain, seen = root, [], set()
    while True:
        if frame in seen or frame not in documents or len(chain) >= 32:
            raise FocusUnavailable('focused child frame unavailable in this page session')
        seen.add(frame)
        object_id = None
        try:
            world = _focus_call(browser, 'Page.createIsolatedWorld',
                {'frameId':frame, 'worldName':'herdr-native-focus'}, session)
            context = world.get('executionContextId')
            if type(context) is not int or not 0 < context <= 2147483647:
                raise FocusUnavailable('native focus isolated context unavailable')
            reply = _focus_call(browser, 'Runtime.evaluate',
                {'expression':'document.activeElement', 'contextId':context}, session)
            remote = reply.get('result')
            object_id = remote.get('objectId') if isinstance(remote,dict) else None
            if ('exceptionDetails' in reply or not isinstance(object_id,str)
                    or not 0 < len(object_id) <= 4096):
                raise FocusUnavailable('native focused element unavailable')
            node = _focus_call(browser, 'DOM.describeNode', {'objectId':object_id,'depth':0,'pierce':True}, session).get('node',{})
            if not isinstance(node, dict):
                raise FocusUnavailable('native focused element metadata unavailable')
            shadows = node.get('shadowRoots', [])
            if (not isinstance(shadows,list) or len(shadows)>64 or any(
                    not isinstance(root,dict) or root.get('shadowRootType')!='user-agent' for root in shadows)):
                raise FocusUnavailable('custom or unresolved shadow focus is unsupported')
            backend = node.get('backendNodeId')
            if type(backend) is not int or not 0 < backend <= 2147483647:
                raise FocusUnavailable('native focused backend identity unavailable')
            node_name = node.get('nodeName')
            if not isinstance(node_name,str) or not 0 < len(node_name) <= 128:
                raise FocusUnavailable('native focused node type unavailable')
            chain.append((frame, documents[frame], backend))
            if node_name not in ('IFRAME','FRAME'):
                break
            frame = node.get('frameId')
            if not isinstance(frame,str):
                raise FocusUnavailable('native iframe owner has no child frame identity')
        finally:
            if object_id:
                with browser.cleanup():
                    try:
                        browser.cdp.call('Runtime.releaseObject', {'objectId':object_id}, session)
                    except Fault:
                        pass
    after, after_root = frame_documents(browser, session)
    if after_root != root or any(after.get(f) != loader for f,loader,_ in chain):
        raise FocusUnavailable('focused frame document changed during inspection')
    return FocusStamp(root, documents[root], chain[-1][0], chain[-1][1], chain[-1][2], tuple(chain))

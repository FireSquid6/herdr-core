"""Core-owned live pane catalogs. No UI/layout, profile split or page DOM IO."""
import contextlib
import re
import uuid

if 'Fault' not in globals():
    from browser_cdp import Fault

_GROUP_TABS=('browser.tabs','browser.tab.create','browser.tab.select','browser.tab.close',
             'browser.tab.reorder','browser.tab.move')
_TARGET_METHODS=('browser.navigate','browser.reload','browser.history','browser.viewport','browser.frame',
                 'browser.input','browser.devtools.open','browser.devtools.close','browser.clipboard',
                 'browser.context','browser.context_action')
_RESOLVE_METHODS=('browser.navigate','browser.reload','browser.history','browser.viewport','browser.frame',
                  'browser.input','browser.devtools.open','browser.devtools.close')
_SURFACES=('browser.viewport','browser.frame','browser.input','browser.context')
_LEGACY_WRITES=('browser.navigate','browser.reload','browser.history','browser.tab.create','browser.tab.select',
                'browser.tab.close','browser.input','browser.clipboard','browser.context_action','browser.devtools.open')


def group_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}',value):
        raise Fault('invalid_params','group/tab identity must be opaque ASCII, 1..128 characters')
    return value


def groups_dispatch(browser, identity, method, p, navigation, invalidate_context):
    dedicated=method.startswith('browser.group.') or method in ('browser.tab.reorder','browser.tab.move')
    if 'group' not in p:
        if dedicated:
            raise Fault('invalid_params','group is required')
        if method in _LEGACY_WRITES:
            browser.legacy_touched=True
            catalog=getattr(browser,'groups',None)
            if catalog and catalog.spare:
                surface=p.get('surface')
                target=p.get('target',surface.get('target') if isinstance(surface,dict) else None)
                if target==catalog.spare:
                    catalog.spare=None  # Never later navigate a legacy-owned live blank.
        return False,None
    name=group_id(p['group'])
    if method not in _GROUP_TABS and method not in ('browser.group.ensure','browser.group.visible') and method not in _TARGET_METHODS:
        raise Fault('invalid_params','group is unsupported for this operation')
    if method=='browser.tab.move' and set(p)-{'workspace','generation','group','tab','to_group','before'}:
        raise Fault('invalid_params','move only accepts a destination group in this workspace browser')
    if not hasattr(browser,'groups'):
        if method!='browser.group.ensure':
            raise Fault('not_found','browser group not found')
        browser.groups=BrowserGroups(browser,navigation,invalidate_context)
    catalog=browser.groups
    if method in _TARGET_METHODS:
        group=catalog.get(name)
        if method=='browser.context_action' and p.get('action')=='new_tab':
            catalog.capacity(1)
            if len(group['tabs'])>=64:raise Fault('invalid_params','group tab bound exceeded')
        surface=p.get('surface')
        target=surface.get('target') if method in _SURFACES and isinstance(surface,dict) else p.get('target')
        entry=catalog.entry(group,p['tab']) if 'tab' in p else None
        if target is None:
            if method not in _RESOLVE_METHODS or entry is None:
                raise Fault('invalid_params','an exact native target is required for this owner operation')
            target=entry['target']
            if target is None:
                raise Fault('not_found','stable tab is not loaded; select/create must precede target operations')
            p=dict(p)
            if method in _SURFACES:
                if surface is not None and not isinstance(surface,dict):
                    raise Fault('invalid_params','surface must be an object')
                p['surface']=dict(surface or {})
                p['surface'].setdefault('kind','page')
                p['surface']['target']=target
            else:p['target']=target
        if not isinstance(target,str) or not any(e['target']==target for e in group['tabs']):
            raise Fault('not_found','public target does not belong to this group')
        if entry is not None and entry['target']!=target:
            raise Fault('invalid_params','stable tab and native target do not identify the same page')
        return False,p
    return True,catalog.dispatch(identity,method,p)


class BrowserGroups:
    def __init__(self,browser,navigation,invalidate_context):
        self.browser=browser;self.navigation=navigation;self.invalidate_context=invalidate_context
        self.groups={};self.spare=None;self.revision=0

    @contextlib.contextmanager
    def commit(self,identity,generation):
        with self.browser.state_lock:
            self.browser.lease.check(identity,generation)
            yield

    def get(self,name):
        if name not in self.groups:
            raise Fault('not_found','browser group not found')
        return self.groups[name]

    def ids(self):
        return {e['id'] for g in self.groups.values() for e in g['tabs']}

    def fresh(self,extra=()):
        existing=self.ids().union(e['id'] for e in extra)
        while True:
            value='bt-'+str(uuid.uuid4())
            if value not in existing:return value

    def entry(self,group,tab):
        group_id(tab)
        for e in group['tabs']:
            if e['id']==tab:return e
        raise Fault('not_found','stable browser tab not found in group')

    def changed(self,targets=()):
        self.revision+=1
        self.invalidate_context(self.browser)
        if hasattr(self.browser,'edit'):self.browser.edit.invalidate()
        for target in targets:
            if target:
                self.browser.sequences.pop(target,None)
                tool=self.browser.tools.get(target)
                if tool:self.browser.sequences.pop(tool['id'],None)

    def adjacent(self,group,index):
        tabs=group['tabs']
        group['selected']=tabs[min(index,len(tabs)-1)]['id'] if tabs else None

    def observe(self,identity,generation,infos):
        with self.commit(identity,generation):
            for group in self.groups.values():
                for index,e in reversed(list(enumerate(group['tabs']))):
                    target=e['target']
                    if target is None:continue
                    if target not in infos:
                        group['tabs'].pop(index)
                        if group['selected']==e['id']:self.adjacent(group,index)
                        self.changed((target,))
                        continue
                    info=infos[target]
                    try:url=self.navigation(info['url'])
                    except Fault:url=e['url']  # Retain last eligible observed URL.
                    title=str(info.get('title',''))[:1024]
                    if (url,title)!=(e['url'],e['title']):
                        e['url'],e['title']=url,title
                        self.revision+=1

    def context_created(self,identity,p,result):
        # Only the trusted token action's observed native create reply enters
        # the catalog. Website-created popups remain a separate policy slice.
        if p.get('action')!='new_tab' or result.get('kind')!='action-dispatched':return result
        group=self.get(p['group']);target=result.get('target')
        if not isinstance(target,str) or not target:raise Fault('browser_failed','missing created native target')
        url=self.navigation(result.get('url'))
        with self.commit(identity,p['generation']):
            if any(e['target']==target for g in self.groups.values() for e in g['tabs']):
                raise Fault('browser_failed','created native target already belongs to a group')
            self.capacity(1)
            if len(group['tabs'])>=64:raise Fault('invalid_params','group tab bound exceeded')
            group['tabs'].append({'id':self.fresh(),'target':target,'url':url,'title':''})
            self.changed((target,))
        return result

    def snapshot(self,name):
        group=self.get(name)
        return {'group':name,'selected':group['selected'],'tabs':[dict(e) for e in group['tabs']],
                'revision':self.revision}

    def restore(self,p):
        if type(p.get('reset_frames',False)) is not bool:
            raise Fault('invalid_params','reset_frames must be a boolean')
        if type(p.get('visible')) is not bool:
            raise Fault('invalid_params','visible must be a boolean')
        homepage=self.navigation(p.get('url','about:blank'))
        raw=p.get('restore')
        if raw is None:return None,homepage
        if not isinstance(raw,dict) or set(raw)-{'tabs','selected'} or not isinstance(raw.get('tabs'),list) or len(raw['tabs'])>64:
            raise Fault('invalid_params','restore requires at most 64 tabs and a selection')
        entries=[];seen=set()
        for tab in raw['tabs']:
            if not isinstance(tab,dict) or set(tab)!={'id','url'}:
                raise Fault('invalid_params','restored tabs require only id and url')
            ident=group_id(tab['id'])
            if ident in seen:raise Fault('invalid_params','duplicate restored tab identity')
            seen.add(ident)
            entries.append({'id':ident,'target':None,'url':self.navigation(tab['url']),'title':''})
        selected=raw.get('selected')
        if entries:group_id(selected)
        if (entries and selected not in seen) or (not entries and selected is not None):
            raise Fault('invalid_params','restored selection must name a restored tab')
        return {'tabs':entries,'selected':selected,'visible':p['visible']},homepage

    def capacity(self,count,new_group=False):
        if count>64 or sum(len(g['tabs']) for g in self.groups.values())+count>128 or (new_group and len(self.groups)>=16):
            raise Fault('invalid_params','browser group/catalog bound exceeded (16 groups, 64 tabs/group, 128/workspace)')

    def materialize(self,identity,generation,entry,infos,background=False):
        if entry['target'] is not None:return
        # Publish only a target ID actually observed in a native reply.
        spare=self.spare if self.spare in infos and infos[self.spare]['url']=='about:blank' else None
        if spare:
            if entry['url']!='about:blank':
                reply=self.browser.cdp.call('Page.navigate',{'url':entry['url']},self.browser.session(spare))
                if reply.get('errorText'):raise Fault('browser_failed','startup target navigation failed')
            target=spare
        else:
            args={'url':entry['url']}
            if background:args['background']=True
            target=self.browser.cdp.call('Target.createTarget',args)['targetId']
        with self.commit(identity,generation):
            if spare:self.spare=None
            entry['target']=target
            self.revision+=1
            infos[target]={'id':target,'url':entry['url'],'title':''}

    def ensure(self,identity,p,infos,restore,homepage):
        name=p['group'];generation=p['generation']
        if name in self.groups:
            group=self.groups[name]
            if p['visible'] and group['selected']:
                self.materialize(identity,generation,self.entry(group,group['selected']),infos)
            with self.commit(identity,generation):
                if group['visible']!=p['visible']:
                    group['visible']=p['visible'];self.revision+=1
            return self.snapshot(name)
        entries=restore['tabs'] if restore is not None else []
        if self.ids().intersection(e['id'] for e in entries):
            raise Fault('invalid_params','restored tab identity already belongs to another group')
        group=restore if restore is not None else {'tabs':entries,'selected':None,'visible':p['visible']}
        first=not self.groups
        startup=(first and not getattr(self.browser,'legacy_touched',False) and len(infos)==1
                 and next(iter(infos.values()))['url']=='about:blank')
        if first and not startup:
            for target,info in infos.items():
                if info.get('opener'):continue  # Engine popups stay unassigned until the opener policy slice.
                match=next((e for e in entries if e['target'] is None and e['url']==info['url']),None)
                if match is None:
                    match={'id':self.fresh(entries),'target':None,'url':self.navigation(info['url']),'title':''}
                    entries.append(match)
                match['target']=target;match['title']=str(info.get('title',''))[:1024]
                if target==self.browser.selected:group['selected']=match['id']
        if restore is None and not entries:
            entries.append({'id':self.fresh(),'target':None,'url':homepage,'title':''})
            group['selected']=entries[0]['id']
        if entries and group['selected'] is None:group['selected']=entries[0]['id']
        self.capacity(len(entries),new_group=True)
        with self.commit(identity,generation):
            if startup:self.spare=next(iter(infos))
            self.groups[name]=group
            self.changed()
        if p['visible'] and group['selected']:
            self.materialize(identity,generation,self.entry(group,group['selected']),infos)
        return self.snapshot(name)

    def dispatch(self,identity,method,p):
        name=p['group'];generation=p['generation']
        restore=homepage=None
        if method=='browser.group.ensure':
            restore,homepage=self.restore(p)
            if name not in self.groups:
                self.capacity(len(restore['tabs']) if restore is not None else 1,new_group=True)
                if restore and self.ids().intersection(e['id'] for e in restore['tabs']):
                    raise Fault('invalid_params','restored tab identity already belongs to another group')
        else:self.get(name)
        if method=='browser.group.visible' and type(p.get('visible')) is not bool:
            raise Fault('invalid_params','visible must be a boolean')
        url=self.navigation(p.get('url','about:blank')) if method=='browser.tab.create' else None
        if method=='browser.tab.create':
            if 'id' in p:
                group_id(p['id'])
                if p['id'] in self.ids():raise Fault('invalid_params','stable tab identity already belongs to a group')
            if type(p.get('background',False)) is not bool:raise Fault('invalid_params','background must be boolean')
            self.capacity(1)
            if len(self.get(name)['tabs'])>=64:raise Fault('invalid_params','group tab bound exceeded')
        if method in ('browser.tab.select','browser.tab.close','browser.tab.reorder','browser.tab.move'):
            entry=self.entry(self.get(name),p.get('tab'))
            destination=self.get(group_id(p.get('to_group'))) if method=='browser.tab.move' else self.get(name)
            before=p.get('before')
            if method in ('browser.tab.reorder','browser.tab.move') and before is not None:self.entry(destination,before)
            if method=='browser.tab.move' and destination is not self.get(name):
                if any(e['id']==entry['id'] for e in destination['tabs']):
                    raise Fault('invalid_params','stable tab identity already belongs to destination')
                if len(destination['tabs'])>=64:
                    raise Fault('invalid_params','destination group tab bound exceeded')
        infos={t['id']:t for t in self.browser.tabs()['tabs']}
        self.observe(identity,generation,infos)
        if method=='browser.group.ensure':
            result=self.ensure(identity,p,infos,restore,homepage)
            if p.get('reset_frames',False):
                # Explicit renderer reattach after its old frame pump drained.
                # No consumption is forged; other groups keep their ACK state.
                with self.commit(identity,generation):
                    for entry in self.get(name)['tabs']:
                        target=entry['target']
                        if target:
                            self.browser.sequences.pop(target,None)
                            tool=self.browser.tools.get(target)
                            if tool:self.browser.sequences.pop(tool['id'],None)
            return result
        group=self.get(name)
        if method=='browser.tabs':return self.snapshot(name)
        if method=='browser.group.visible':
            with self.commit(identity,generation):
                if group['visible']!=p['visible']:group['visible']=p['visible'];self.revision+=1
            return self.snapshot(name)
        if method=='browser.tab.create':
            entry={'id':p['id'] if 'id' in p else self.fresh(),'target':None,'url':url,'title':''}
            self.materialize(identity,generation,entry,infos,background=p.get('background',False))
            with self.commit(identity,generation):
                group['tabs'].append(entry)
                if not p.get('background',False):group['selected']=entry['id']
                self.changed((entry['target'],))
        else:
            entry=self.entry(group,p.get('tab'))  # Recheck after observed native closures.
            index=group['tabs'].index(entry)
            if method=='browser.tab.select':
                self.materialize(identity,generation,entry,infos)
                with self.commit(identity,generation):
                    group['selected']=entry['id'];self.changed()
            elif method=='browser.tab.close':
                target=entry['target']
                if target and not self.browser.cdp.call('Target.closeTarget',{'targetId':target})['success']:
                    raise Fault('browser_failed','native target did not close')
                with self.commit(identity,generation):
                    group['tabs'].remove(entry)
                    if group['selected']==entry['id']:self.adjacent(group,index)
                    self.changed((target,))
                if group['visible'] and group['selected']:
                    self.materialize(identity,generation,self.entry(group,group['selected']),infos)
                return {**self.snapshot(name),'empty':not group['tabs'],'closed':{'id':entry['id'],'url':entry['url']}}
            else:
                destination=self.get(p['to_group']) if method=='browser.tab.move' else group
                before=p.get('before')
                if before is not None:self.entry(destination,before)
                if before==entry['id'] and destination is group:
                    if method=='browser.tab.reorder':return self.snapshot(name)
                    with self.commit(identity,generation):
                        group['selected']=entry['id'];self.changed((entry['target'],))
                    return {'source':self.snapshot(name),'destination':self.snapshot(name),'source_empty':not group['tabs']}
                with self.commit(identity,generation):
                    group['tabs'].remove(entry)
                    if method=='browser.tab.move' and group['selected']==entry['id']:self.adjacent(group,index)
                    at=next((i for i,e in enumerate(destination['tabs']) if e['id']==before),len(destination['tabs']))
                    destination['tabs'].insert(at,entry)
                    if method=='browser.tab.move':destination['selected']=entry['id']
                    elif group['selected'] is None:group['selected']=entry['id']
                    self.changed((entry['target'],) if method=='browser.tab.move' else ())
                if method=='browser.tab.move':
                    return {'source':self.snapshot(name),'destination':self.snapshot(p['to_group']),'source_empty':not group['tabs']}
        return self.snapshot(name)

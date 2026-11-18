# Experimental native workspace browser (personal fork)

This opt-in Linux fork feature runs sandboxed full Chromium on the core host and
streams bounded PNG frames to a dedicated authenticated native Herdr viewer.
It is not an agent browser automation API and is absent from CLI/schema discovery.
The terminal protocol and existing agent commands are unchanged.

**Not an integrated MVP acceptance claim.** The real core/Chrome transport,
DevTools, ownership, profiles and reconnect checks below pass; actual native GPUI
acceptance is tracked by the desktop counterpart. Do not install this over a
production core merely to try it.

## Runtime / isolation

Requires Python 3 (stdlib only) and full supported Chrome. Tested with
`/opt/google/chrome/chrome` 157.0.8103.50. Worker source is embedded in the core
binary. `HC_BROWSER_CHROME` explicitly enables it; unset means unsupported.
Chromium must run nonroot with its sandbox enabled. No sandbox-disable fallback,
no debug TCP listener, no external CDP endpoint. Security updates to the Chrome
package remain the operator's responsibility; no logged-in production deployment
is validated here.

Use a new real mode0700 scratch root under `/var/tmp`, isolated HC_SESSION and
HC_SOCKET, HOME/TMPDIR and all XDG directories. Remove inherited HC routing and
terminal variables. Never use production sockets, user data, desktop displays or
`just install`. The executable test runners create these environments themselves:

```sh
source ~/.local/share/herdr-fork-tools/env.sh
CARGO_BUILD_JOBS=2 CARGO_PROFILE_DEV_DEBUG=0 cargo build -p herdr-core
python3 -m unittest discover -s scripts -p 'test_browser*.py' -v
python3 scripts/browser_transport_smoke.py
python3 scripts/browser_worker_smoke.py
python3 scripts/browser_security_smoke.py
python3 scripts/browser_startup_smoke.py
python3 scripts/browser_debugger_input_smoke.py
```

All browser IPC uses a dedicated framed native connection on the existing private
Unix socket, or a dedicated authenticated SSH `hc connect` transport for that
machine. Do not reuse shared `remote::Line`: different viewers must retain distinct
connection identities. No local fallback for an unavailable remote workspace.
Native intent admission trusts software running as the authenticated OS account;
it cannot cryptographically prove a human. Pages/mods must never receive that
privileged connection.

## Implemented experimental subset

Revision2 negotiate; explicit lazy ensure; one exclusive 30s controller lease;
heartbeat/release/confirmed forced takeover; real tabs/navigation/history/reload;
page and real DevTools viewport/input/frame pull; exact per-surface frame ACK;
confirmed stop; live page/PID/unsent draft/memory survives native viewer disconnect.

Chrome's matching bundled DevTools frontend is instantiated by its own inspect
UI, uses Chrome's native host attachment, and is undocked within headless Chrome
so the app can place its full stream bottom/right. No custom frontend JS binding,
WebSocket shim, or privileged bridge is injected into any page. Actual core-side
pointer/text/Enter Console input, DOM inspection, Console evaluation, Network
logging, debugger pause/stepOver/resume have reproducible real tests. These do
not replace native GPUI interactive acceptance.

Each workspace has its own persistent profile. Actual window.open targets retain
original opener identity; they are not recreated by URL. Permission baseline
explicitly denies geolocation/camera/mic/notifications/ambient clipboard reads
and writes. Downloads are denied pending supported transfer UI. Private HOME,
no desktop session bus/display and empty private helper PATH prevent ordinary
external protocol helper launches; the synthetic real-input test registers a
private handler and confirms it never runs.

Page hiding/disconnect/release never stops Chrome. Reacquisition creates a new
generation and resets frame ACKs. Release/expiry/takeover closes real DevTools
frontends, not the inspected pages. Stop retains the profile, not unsaved JS
memory. Short genuine per-browser `/var/tmp/hcbr.*` runtime directories avoid
Chromium socket limits even when persistent profile paths contain UUIDs; cleanup
only after observed exit and no matching owned Chrome descendants.

## Experimental plaintext editing (`clipboard_text_v1`)

Revision 2 negotiation now additionally advertises `clipboard_text_v1`. This is a
private native-viewer extension, not an agent/CLI/schema method:

```text
browser.clipboard {workspace, generation, target,
                   operation: "copy" | "cut" | "paste", text?: string}
  -> {kind: "copied", text: string}
   | {kind: "no-update-observed"}
   | {kind: "paste-dispatched"}
   | {kind: "failed", message: string}
```

Paste requires a fresh string, including an explicit empty string; text is
forbidden for Copy/Cut. Plaintext is bounded to 65,536 UTF-8 bytes (the existing
wire request bound also applies). Invalid parameters, stale ownership and unknown
or private targets are RPC errors. Only `copied` exports text. The app must
recheck focused ownership before reading/writing its normal GPUI clipboard; core
never accesses an OS clipboard. No clipboard text is logged or persisted by this
method. Context-menu metadata and one-shot actions are described below.

The first valid operation lazily creates a separate ephemeral BrowserContext and
an owned private target/session. Its fixed DOM/native clipboard handlers are
never installed in source pages. Public tab/surface/navigation/close operations
cannot target this helper. Paste seeds fresh text with native helper Copy and
verifies it with native helper Paste before dispatching source Paste. Copy/Cut
first seed and verify a fresh sentinel, issue the source command once, and poll
native helper Paste for at most a 500ms observation window, plus bounded CDP ACK
waits. No source selection/DOM extraction, Clipboard API permission grant,
synthetic DOM clipboard event or IME insertion is used. Existing ambient website
clipboard permissions remain denied; explicit user-initiated native Paste is the
intended authorized read.

Native commands use `Input.dispatchKeyEvent.commands` and wait for actual CDP
ACKs, not ordinary asynchronous input receipts. They generate trusted keydowns;
a page's keydown `preventDefault()` can suppress the command. An independent
native-menu command without that keydown is **not demonstrated**. An unchanged
sentinel reports `no-update-observed`, not proven cancellation. Empty/no-format
observations at the end of the window produce an empty plaintext projection;
nontext-only formats fail rather than exporting the old sentinel. Rich formats
are not transported. ACKs are not ClipboardHost write-commit fences, and
independent eligible gesture writes can race; no writer-attribution claim is made.

Operations serialize per browser. Controller generation, browser instance,
explicit target and native focused-element chain are captured and checked before
source dispatch and before export; revocation invalidates pending exports. The
focus stamp includes root and focused-child loaders plus native backend-node IDs,
not just the root loader. Readonly isolated-world `document.activeElement` and
native DOM descriptions walk iframe owners, bounded to 64 frames / depth 32.
Object handles are released. No field value or selected text is inspected. The
private helper can have WebContents focus: source identity uses its retained
active-element chain, not `document.hasFocus()`. Known unsupported/changed focus
returns a payload-free healthy failure; genuine transport loss still poisons
editing. No DOM focus change is used to repair a mismatch. This
is not atomic with arbitrary renderer navigation or transient focus changes
between observations; it is not an independently targeted native-menu command. A genuine CDP failure/timeout
poisons editing until explicit browser stop/restart. No automatic Cut retry or
undo is attempted: deletion may already have occurred before ownership loss,
transport failure, document change or an oversized/nontext result. `failed` and
`paste-dispatched` do not promise an editor effect. Helper contexts are disposed
before normal browser shutdown; Chrome exit is the final cleanup fallback.

Custom open/closed shadow-host focus is unsupported. Native
`DOM.describeNode(depth:0,pierce:true)` exposes `shadowRoots` metadata even when
JavaScript's closed-host `shadowRoot` getter is null. The focus inspector rejects
custom roots and unknown/malformed root metadata; ordinary input/textarea
`user-agent` roots remain allowed. It does not treat a shadow host backend ID as
its descendant iframe/editor identity. Both initial opaque focus and focus that
moves into a custom shadow host during a native command fail without exporting
text. This is rejection, not shadow-frame routing support.

Checks: `python3 -m unittest discover -s scripts -p 'test_browser_edit.py' -v`
runs process-free public-dispatch validation/poisoning tests. The additional real
Chrome test requires explicit `HC_BROWSER_EDIT_NATIVE_TEST=1`, installed sandboxed
`/opt/google/chrome/chrome`, a normal user, scratch HC_SESSION and HOME/XDG paths.
It launches only an owned worker, never hc/core or the desktop. It checks real
Copy/Paste/Cut, cancellation/no-update reuse, empty Paste and private-target
rejection. These checks are not OS clipboard, native Wayland/GPUI, trusted-mod
context menu or issue #4 acceptance.

## Experimental trusted context menus (`context_menu_v1`)

The same private revision-2 negotiation additionally advertises `context_menu_v1`:

```text
browser.context {workspace, generation, surface:{kind:"page",target}, x, y}
 -> {token, target, document:{frame,loader}, link:string|null,
     editable, password, read_only, disabled, selection_present,
     can_copy, can_cut, can_paste}
browser.context_action {workspace, generation, target, token,
                        action, text?:string}
```

Actions: `copy`, `cut`, `paste`, `copy_link`, `open_link`, `new_tab`, `select_all`.
Clipboard actions return the editing results above; Paste requires fresh bounded
plaintext. `copy_link` returns `{kind:"link",text:URL}`. Other actions return
`{kind:"action-dispatched",action}`; `new_tab` also returns `target` and
`opener_preserved:false`. It creates a background target in the source browser
context/profile without reopening the source. A preserved opener cannot be
requested through the demonstrated `Target.createTarget` path; this is not full
popup/opener compatibility. Open/new-tab destinations must pass HTTP(S) hostname
validation and the existing navigation gate. No external protocol is executed.

Coordinates are finite viewport CSS pixels in `[0,4096)`, additionally bounded by
the configured viewport. DOM native hit-testing selects backend node/frame IDs;
a fixed readonly function inspects that node in a CDP isolated world with no
universal-access grant. Main-world prototype overrides do not implement this
query. Only bounded link metadata and exact public booleans are returned: no
selected text, field value or source clipboard mirror. An internal readonly
hit/focused-element relationship boolean is validated and removed from the public
response. Native focus snapshots bracket inspection and are retained privately. Link URLs are untrusted website
data, not code or authority. Password fields disallow Copy/Cut; readonly and
disabled fields disallow mutation. Metadata remains advisory about live editor
state: only native commands implement editing, and the page may change focus or
DOM between inspection and dispatch. A query does not itself focus the hit node.
Copy/Cut/Paste/Select All require the native focused frame to equal the hit frame,
the protected hit/focus relationship to match, and the focus/backend/loader stamp
to remain unchanged. A mismatch is rejected, never silently refocused. Link
Copy/Open/New Tab do not require the hit link to be focused.

At most 16 random, memory-only tokens per browser, with a 10-second lifetime.
Each is bound to connection, generation, browser instance, explicit source target,
and both main and actual hit-frame loader identities. Query checks document
identity before/after inspection; actions consume a token before use and recheck
documents and ownership. Wrong/foreign/expired/replayed tokens fail closed. Native
Copy/Cut/Paste also recheck the context after helper seeding and before export;
a stale guard returns a known failed result without poisoning healthy editing.
No automatic Cut retry or undo is permitted, including after a lost ownership
race: a native edit may already have happened. Site keydown/copy handlers can
change focus during a command; the post-command focus guard discards their
pending export on a mismatch. A same-focus custom clipboard handler may still
supply arbitrary text, as normal browser semantics permit. There is no atomic navigation fence.

Release, expiry, controller replacement, navigation/history/reload, tab
select/create/close, stop, direct clipboard actions and changed page viewport
invalidate menus. Pointer-down, wheel and key-down input invalidate them too;
move, mouse-up and key-up do not. Normal frame pulls never invalidate menus.
The app still owns global/focused-widget validation and must discard pending OS
clipboard exports after focus/ownership changes. Websites never receive this
protocol or a trusted mod bridge. Trusted app/mod UI supplies menus, not a native
Chromium menu. No app menu presentation or OS clipboard acceptance is claimed.

`test_browser_context.py` exercises the public worker dispatch seam. Its real
fixture additionally requires `HC_BROWSER_CONTEXT_NATIVE_TEST=1` and the same
normal-user scratch/sandbox precautions as the editing test. Top-level native
metadata, main-world override isolation, Copy Link and native Copy are exercised.
`test_browser_context_frames.py` additionally opts in with
`HC_BROWSER_CONTEXT_FRAMES_NATIVE_TEST=1` and an owned
`HC_BROWSER_CONTEXT_FRAMES_EVIDENCE` directory. Native same-origin child routing,
focus-diversion rejection, ordinary Copy focus-change export rejection and child
navigation samples are exercised. Required positive sibling-diversion routing
remains red: safe rejection is not functional routing. OOPIF positive acceptance
also remains red. Native iframe-owner hits now return `unsupported_capability`
instead of parent-node editable metadata; no cross-process routing is invented.
If native hit/resolve/isolated frame inspection is unsupported, the API fails
closed rather than granting broader access or using main-world extraction.

`test_browser_shadow_focus.py` additionally requires
`HC_BROWSER_SHADOW_FOCUS_NATIVE_TEST=1` plus the frame-test opt-in/evidence
envelope above. Owned open/closed hosts contain two same-origin iframe editors.
Native receipts cover pre-command rejection, unchanged source command counters,
post-command export discard, and subsequent ordinary textarea Copy success.
The pre-fix fixture reproduced real sibling-witness exports under identical host
focus stamps; it is not merely a static identity-collision test. No OS clipboard
or GUI acceptance follows from these headless checks. Never automatically retry
or undo Cut after a dispatched command.

## Experimental pane groups (`pane_groups_v1`)

This additive private revision-2 capability keeps one browser/profile per
workspace and a live ordered catalog per browser pane. Multiple panes must share
one dedicated controller connection/lease; they must not release that workspace
lease on each individual panel drop. Connection EOF/revocation retains groups
and pages. No generated agent API/schema methods are added.

All calls below carry `workspace,generation,group`. IDs are opaque ASCII
`[A-Za-z0-9_:-]{1,128}` (including pane-scoped `pane-a:initial`). These
are JSON/catalog identities, never filesystem paths. Stable tab IDs are workspace-catalog unique and distinct
from actual Chromium target IDs. Generated IDs are `bt-<UUID>`; create may accept
an app-minted ID so queued New + Navigate can address the new logical tab.

```text
browser.group.ensure {group, visible:bool, url?:homepage, reset_frames?:bool,
  restore?:{tabs:[{id,url}],selected:id|null}}
browser.tabs {group}
  -> {group, selected:stableID|null,
      tabs:[{id:stableID,target:nativeTargetID|null,url,title}], revision}
browser.tab.create {group,id?:stableID,url?:"about:blank",background?:bool}
browser.tab.select {group,tab:stableID}
browser.tab.reorder {group,tab:stableID,before?:stableID|null}
  -> group snapshot
browser.tab.close {group,tab:stableID}
  -> group snapshot + {empty:bool,closed:{id,url}}
browser.tab.move {group,tab:stableID,to_group,before?:stableID|null}
  -> {source:group snapshot,destination:group snapshot,source_empty:bool}
browser.group.visible {group,visible:bool}
  -> group snapshot
```

Ensure returns the same snapshot shape. On explicit renderer reattach, after its
previous frame pump has drained, `reset_frames:true` clears only this group's
loaded page/tool frame sequences. The new consumer begins at ACK0 without
pretending to consume an old frame. Sibling streams and live pages remain intact;
periodic observation must omit this flag. Heartbeat and confirmed workspace stop
remain controller-level operations and do not carry `group`.

A trusted context-menu `new_tab` checks catalog capacity before native dispatch,
then registers its observed native target in the source group in the background.
Its grouped acknowledgement includes the validated requested `url`; the legacy
ungrouped four-field acknowledgement remains unchanged. Website-created
popups are not automatically adopted by this path. The app may separately select
the resulting stable tab ID; native target IDs are not valid grouped tab IDs.

Empty restore is `{tabs:[],selected:null}`;
nonempty restore requires its selected ID. At most 16 group records, 64 entries
per group and 128 entries per workspace, including unloaded entries. The existing
512KiB request and 4MiB reply bounds still apply. Titles are bounded to 1024
characters. Safe URL validation reuses the existing HTTP(S)/about:blank gate;
URL fields retain the last eligible observed URL if a live page leaves that set.
Restore/create IDs cannot collide with another group's entries. Move validates
both existing groups and destination insertion membership before mutation; it
accepts only the listed input fields, never another workspace/machine route.

Warm core groups win over stale saved restore data: ensure does not navigate any
loaded target. On a cold catalog, restored entries are lazy and only the selected
entry of an explicitly ensured visible pane materializes. An untouched sole
startup blank is reused without adding a second default tab; a hidden restored
pane leaves it unassigned until a selected page needs it. Otherwise the first
group adopts existing public legacy top-level pages, matching saved IDs by URL
when possible and appending unmatched live pages. Existing live selection wins.
Targets with an engine opener remain unassigned, including during migration;
engine popup policy/pane placement is deferred. Later groups cannot steal targets.
No layout/default-workspace read should call ensure or start core implicitly.

Create always materializes its new tab; background creation retains the existing
group selection. Select materializes only its named lazy entry. Closing a visible
selected tab materializes its chosen adjacent replacement if necessary; closing
the last tab returns empty without creating a blank or stopping Chrome/profile.
Reorder preserves selection. Move transfers the same entry/target/history without
native create/close/navigate calls and selects it in the destination. Lazy moves
remain lazy until a subsequent select/visible ensure. Missing/null `before` appends.
Empty groups remain catalog records and count toward 16; there is no group-drop
API/automatic eviction in this increment. Stopping the workspace browser clears
the live catalog, not the profile. External target closure is reconciled on
snapshot reads, without silently reopening that page.

Navigate/reload/history/devtools and viewport/frame/input accept top-level
`tab:stableID` in place of their native target, only when `group` is present.
Resolution occurs at execution against that exact group entry; the entry must
already be loaded by the preceding create/select/ensure. It never follows global
or group selection and never auto-materializes for input. Surface methods can
omit `surface` for a default page surface when using `tab`, or provide its kind
without a target. If both identities are supplied, they must agree. Clipboard,
context and context-action owner requests still require the exact native target
(or exact `surface.target`); their tokens never follow a stable tab reference.
Every optional-group native-target call checks membership before native IO.
Calls without `group` retain the legacy actual-target API and global tabs shape.

Select/close/move invalidate menus and edit capabilities without poisoning the
native helper or ending the controller. Move resets exact per-surface ACK state
for the actual page and its DevTools stream so the receiving pane starts at ACK0.
Selection/reorder retain per-target ACK state; app clients must keep ACKs scoped
to actual target/controller identity. Late frames still require app pane/tab/target
epoch checks before presentation. Visibility only records a flag: it does not
freeze/throttle Chromium or implement hidden-frame enforcement. A visible ensure
or explicit select loads lazy selection; `group.visible` alone does not.

`test_browser_groups.py` covers public worker dispatch and bounded external-CDP
fakes. The additional owned native test uses `HC_BROWSER_GROUPS_NATIVE_TEST=1`,
scratch HC_SESSION/HOME/XDG and optional `HC_BROWSER_GROUPS_EVIDENCE` JSON path.
It checks cold URL laziness, warm restore precedence, shared cookies, queued
client-ID creation/navigation, live form/marker/history preservation across
select/move, moved frame ACK0 and last-tab closure without engine stop. This is
not issue #2 acceptance: app two-pane presentation, saved-layout migration,
favicons/loading, hidden throttling, focus/keyboard integration and OS/GPUI
acceptance remain separate work.

## Limits / unfinished requirements

- No live reattachment across **core** restart: worker EOF closes its browsers.
  No separately supervised worker or saved-URL restart restoration yet.
- No transfers, chooser/dialog presentation, integrated OS clipboard gestures,
  IME, find/zoom/reopen or complete recovery UX. The bounded editing experiment
  above is not completion of the clipboard/context-menu feature.
- Native app and second-machine routing acceptance remain separate checks.
- At most8 running workspace browsers and16 outstanding RPCs/viewer. Whole PNG
  replies <=4MiB; physical sides <=4096, pixels <=16,777,216. One frame request
  outstanding per surface, next exact sequence acknowledges consumption. Decode
  clients must validate IHDR before allocating and reject stale generations.
- Serial broker, non-input CDP timeout5s and outer worker timeout20s. Input waits
  at most50ms for its ACK: `{accepted:true,pending:true,receipt}` reports only an
  observed authorized write, not completion/effect. This keeps actual pointer-
  triggered debugger pauses from blocking their frontend step/resume controls.
  At most64 combined pending receipts and unconsumed errors, ACK deadline30s;
  heartbeat reports `{input_pending,input_errors:[{receipt,kind,message}]}` to the
  same current controller/generation. Late errors are not silently converted to
  success; normal immediate errors remain RPC errors. Revocation discards old
  receipts before replacement access. Full receipt capacity rejects new input
  until reaping/heartbeat consumption frees capacity. Hung/corrupt worker reset
  can lose live state; no false promise of transparent crash recovery.
- Cancellation checked before broker enqueue and result delivery. Already
  dispatched Chromium actions cannot be undone; cancellation races within a
  multi-CDP operation are not exhaustively verified.
- Chrome menus, device access, codecs/DRM, physical keyboard layouts, native
  Wayland/X11 behavior and long-lived resource pressure are not universally
  verified. Keep this opt-in and do not claim the full product complete.

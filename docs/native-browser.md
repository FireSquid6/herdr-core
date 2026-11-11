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

## Limits / unfinished requirements

- No live reattachment across **core** restart: worker EOF closes its browsers.
  No separately supervised worker or saved-URL restart restoration yet.
- No transfers, chooser/dialog presentation, clipboard gestures, IME, find/zoom/
  reopen or complete recovery UX. No placeholder success for these features.
- Native app and second-machine routing acceptance remain separate checks.
- At most8 running workspace browsers and16 outstanding RPCs/viewer. Whole PNG
  replies <=4MiB; physical sides <=4096, pixels <=16,777,216. One frame request
  outstanding per surface, next exact sequence acknowledges consumption. Decode
  clients must validate IHDR before allocating and reject stale generations.
- Serial broker, per-CDP timeout5s and outer worker timeout20s. Hung/corrupt worker
  reset can lose live state; no false promise of transparent crash recovery.
- Cancellation checked before broker enqueue and result delivery. Already
  dispatched Chromium actions cannot be undone; cancellation races within a
  multi-CDP operation are not exhaustively verified.
- Chrome menus, device access, codecs/DRM, physical keyboard layouts, native
  Wayland/X11 behavior and long-lived resource pressure are not universally
  verified. Keep this opt-in and do not claim the full product complete.

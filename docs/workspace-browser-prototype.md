# Workspace browser prototype (personal fork, incomplete)

**Stage A is blocked. This branch does not implement or advertise a workspace
browser.** No browser methods, worker, stream, DevTools frontend, or human-viewer
admission are exposed. Do not install it expecting the browser feature. The
existing terminal protocol and CLI remain unchanged.

The user-authorized prototype must first demonstrate sandboxed full Chromium
rendering and functional bottom-docked DevTools inside the actual native Herdr
desktop. Unit tests, a screenshot, or a successful CDP response do not pass that
gate. Full runtime implementation must not proceed on an unproven rendering path.

## Working independent pieces

- `crates/herdr-core/src/browser.rs`: deterministic workspace-ID validation and
  exclusive connection-bound ownership. Confirmed takeover, monotonic 30-second
  heartbeat expiry, release/disconnect, and nonreusable worker-incarnation /
  sequence generations have tests. This policy does not itself revoke network
  queues or DevTools: a future serialized worker must close old channels before
  publishing a grant and recheck authorization immediately before each effect.
- `scripts/browser_probe.py`: executable Linux real-Chromium sandbox/CDP
  prerequisite probe. It uses inherited CDP pipes, never a debug TCP port, and
  never accepts sandbox-disabling extra flags. It isolates HOME, TMPDIR, XDG
  paths and the explicit profile inside a fresh mode-0700 scratch directory.
  It records the actual executable/version/UID/exit result, checks
  `chrome://sandbox` and captures it **if Chromium can start**, then reaps its own
  process group and checks for remaining private-environment processes. That
  successful path has not been exercised here because sandbox startup fails.
  Cleanup failure preserves the live profile and reports FAIL; unrelated paths
  and processes are never removed/signalled. Its safety tests are not browser
  acceptance tests.
- Existing real-core/PTY test sessions support `HC_TEST_ROOT` (default remains
  `/var/tmp`) and use a private per-session TMPDIR. Use a short absolute path to
  stay within Unix socket path limits. Keep the parent private and owned by the
  testing account. No test points at a live core.

## Reproduce without production installation

Requirements: Linux, Python 3, a **full sandbox-capable Chromium** executable,
non-root account, and a private writable scratch parent. The probe never starts
core, systemd services, a real desktop display or a browser with a user's normal
profile. It opens about:blank and chrome://sandbox, not real research accounts.

```sh
S="$HOME/.hermes/cache/scratch/workspace-browser-core"
mkdir -p "$S"
chmod 700 "$S"
TMPDIR="$S" PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_browser_probe.py
TMPDIR="$S" PYTHONDONTWRITEBYTECODE=1 python3 scripts/browser_probe.py --browser /absolute/path/to/full/chrome --scratch "$S"
```

Probe exit 0 proves only its sandbox/CDP prerequisite; exit 1 records a failure.
Each run prints its evidence directory and retains report.json, version output
and Chromium stderr; a successful run also retains sandbox.txt / sandbox.png.
Fresh private profiles are deleted after observed cleanup, not reused as browser
sessions. Never interpret this tool as proof of persistence, authentication,
remote security, native input, DevTools, downloads or uploads.

Rust: use the repository's pinned Ghostty submodule and Zig **0.16.0**, not 0.17.
With cargo/rustfmt/clippy and that Zig available:

```sh
git submodule update --init --depth 1
export TMPDIR="$S" HC_TEST_ROOT="$S/tests"
mkdir -p "$HC_TEST_ROOT"
# If this path exceeds Unix socket limits, use a shorter real scratch parent,
# not a production socket override. Executable-identity tests need real paths.
cargo test --locked
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
just check
```

These commands never install the resulting `target/debug/herdr-core` binary.
Do not run `just install`. The full repository recipe also needs cargo-deny and
the Windows cross-target toolchain; missing tools must be reported, not treated
as passed checks.

## Observed blocker and deployment prerequisites

On minibox-one, existing cached full Chromium versions 145.0.7632.6,
146.0.7680.76 and 158.0.8127.18 abort with `No usable sandbox!`, before the first
CDP response. Effective UID is 1000. Ubuntu's
`kernel.apparmor_restrict_unprivileged_userns=1`, `unshare -Ur true` fails EPERM,
and cached sandbox helpers are user-owned mode 0755 rather than installed setuid
helpers. Existing Chrome/Chromium AppArmor profiles cover packaged system paths,
not these cache paths; no matching installed browser was found.

Smallest prerequisite unblock: an administrator provides a supported full
Chromium installation with a working sandbox for this account and its absolute
executable path, or approves and loads a narrowly scoped AppArmor rule for a
fixed Chromium executable. Do not disable the global user-namespace policy,
chmod a user-controlled helper setuid, or disable Chromium's sandbox. No such changes
have been made. A virtual display does not fix this kernel-level failure; CEF
also needs a functioning Chromium sandbox. No CEF/display fallback is claimed
tested. Desktop additionally reports missing native build/display prerequisites.

Before any logged-in deployment, the operator must own Chromium provisioning
and regular security updates; a pinned cache version is not a maintenance plan.
Runtime protocol capabilities must be discovered against that version (or a
supported version range pinned and tested). No version is supported by this
feature yet.

Future recovery guarantees remain requirements, **not implemented behavior**:
live memory survives desktop disconnect; a separately supervised browser worker
can outlive core; explicit browser stop retains URLs/profile only; crash/reboot
restores URLs/profile best-effort, never promises unsent forms or JS memory.
`setsid` alone is not independence from a systemd service cgroup. No automatic
inactive-browser eviction is authorized.

## Coordination and evidence

Core owns the shared proposal `browser-contract.md`, `core-progress.md` and
`core-evidence.md` in `/home/firesquid/documents/herdr-workspace-browser`.
Desktop owns app/integration evidence and its separate repository. The proposal
is not an implemented API: machine routing, viewer admission/gesture issuance
and native physical-key mapping remain unresolved review items. No conflicting
wire API was created. The sessions remain **IN WORK**; the parent/user owns
closure. Shared evidence records every gate item as PASS / FAIL / NOT TESTED,
with exact commands, artifacts and remaining requirements.

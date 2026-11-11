#!/usr/bin/env python3
"""Linux real-browser preflight. No hc/production services, no debug listener.

This is only the sandbox/CDP prerequisite, NOT the native Herdr Stage A gate.
Use a real full Chromium executable; there is deliberately no extra-flags option.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import resource
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time


def launch_args(profile):
    return [
        "--headless",
        f"--user-data-dir={profile}",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]


def sandbox_enabled(text):
    """Require layer-1 isolation and Seccomp, including Chromium's newer table."""
    def row(pattern):
        return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None

    legacy = row(r"^(?:Namespace|SUID) Sandbox[\t ]+Yes[\t ]*$")
    namespace = (
        row(r"^Layer 1 Sandbox[\t ]+Namespace[\t ]*$")
        and row(r"^PID namespaces[\t ]+Yes[\t ]*$")
        and row(r"^Network namespaces[\t ]+Yes[\t ]*$")
    )
    return (legacy or namespace) and row(r"^Seccomp-BPF sandbox[\t ]+Yes[\t ]*$")


def private_processes(private):
    needle = f"XDG_RUNTIME_DIR={private / 'xdg_runtime_dir'}".encode()
    found = []
    for path in Path("/proc").glob("[0-9]*/environ"):
        try:
            if needle in path.read_bytes().split(b"\0"):
                found.append(int(path.parent.name))
        except (OSError, ProcessLookupError):
            pass
    return sorted(found)


class Pipe:
    """Bounded, synchronous CDP prerequisite probe; never exposed as an API."""

    def __init__(self, read, write, timeout):
        self.read = read
        self.write = write
        self.deadline = time.monotonic() + timeout
        self.pending = bytearray()
        self.sequence = 0

    def call(self, method, params=None, session=None):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        data = json.dumps(message).encode() + b"\0"
        while data:
            data = data[os.write(self.write, data):]
        while True:
            if b"\0" not in self.pending:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.read], [], [], remaining)[0]:
                    raise TimeoutError(f"CDP timeout at {method}")
                chunk = os.read(self.read, 65536)
                if not chunk:
                    raise RuntimeError(f"CDP EOF at {method}; see chromium.stderr")
                self.pending.extend(chunk)
                if len(self.pending) > 16 * 1024 * 1024:
                    raise RuntimeError("CDP probe receive bound exceeded")
                continue
            raw, _, rest = self.pending.partition(b"\0")
            self.pending = bytearray(rest)
            response = json.loads(raw)
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            return response["result"]


def run(browser, scratch, timeout=15):
    if os.geteuid() == 0:
        raise PermissionError("run Chromium as a non-root account")
    browser = browser.resolve(strict=True)
    scratch = scratch.resolve(strict=True)
    if scratch.stat().st_uid != os.geteuid():
        raise PermissionError("scratch parent must belong to this account")
    directory = Path(tempfile.mkdtemp(prefix="browser-probe-", dir=scratch))
    private = directory / "private"
    private.mkdir(mode=0o700)
    env = os.environ.copy()
    for key in ["DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK"]:
        env.pop(key, None)
    for key in ["HOME", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"]:
        path = private / key.lower()
        path.mkdir(mode=0o700)
        env[key] = str(path)
    for name in ["profile", "downloads", "staging"]:
        (private / name).mkdir(mode=0o700)
    args = [str(browser), *launch_args(private / "profile")]
    report = {
        "result": "FAIL", "scope": "sandbox/CDP prerequisite ONLY; not native Stage A",
        "uid": os.getuid(), "euid": os.geteuid(), "browser": str(browser), "argv": args,
        "sandbox": "NOT TESTED", "native_gpui": "NOT TESTED", "devtools": "NOT TESTED",
        "profile_mode": oct((private / "profile").stat().st_mode & 0o777),
        "debug_transport": "inherited pipe; no debug port flag",
    }
    for key in ["apparmor_restrict_unprivileged_userns", "unprivileged_userns_clone"]:
        path = Path("/proc/sys/kernel") / key
        report[key] = path.read_text().strip() if path.exists() else "unavailable"
    pid = None
    fds = []
    reaped = False
    try:
        version = subprocess.run([str(browser), "--version"], env=env, capture_output=True, timeout=timeout)
        (directory / "version.stdout").write_bytes(version.stdout)
        (directory / "version.stderr").write_bytes(version.stderr)
        report["version"] = version.stdout.decode(errors="replace").strip()
        report["version_exit_code"] = version.returncode
        if version.returncode:
            raise RuntimeError("browser --version failed")
        to_child, to_browser = os.pipe()
        from_browser, from_child = os.pipe()
        fds = [to_child, to_browser, from_browser, from_child]
        with open(os.devnull, "rb") as null, (directory / "chromium.stdout").open("wb") as out, (directory / "chromium.stderr").open("wb") as err:
            # Duplicate sources above fd 9 so dup2 to Chromium's fd 3/4 cannot
            # clobber another source. posix_spawn has no unsafe preexec callback.
            import fcntl
            source_in = fcntl.fcntl(to_child, fcntl.F_DUPFD_CLOEXEC, 10)
            source_out = fcntl.fcntl(from_child, fcntl.F_DUPFD_CLOEXEC, 10)
            fds.extend([source_in, source_out])
            actions = [(os.POSIX_SPAWN_DUP2, a, b) for a, b in [
                (null.fileno(), 0), (out.fileno(), 1), (err.fileno(), 2), (source_in, 3), (source_out, 4),
            ]]
            actions.extend((os.POSIX_SPAWN_CLOSE, fd) for fd in fds if fd not in (3, 4))
            # This group is for bounded cleanup of OUR probe, not a claim that
            # setsid escapes systemd cgroups or provides independent supervision.
            pid = os.posix_spawn(str(browser), args, env, file_actions=actions, setsid=True)
        for fd in [to_child, from_child, source_in, source_out]:
            os.close(fd)
            fds.remove(fd)
        report["pid"] = pid
        pipe = Pipe(from_browser, to_browser, timeout)
        report["cdp_version"] = pipe.call("Browser.getVersion")
        target = pipe.call("Target.createTarget", {"url": "chrome://sandbox"})["targetId"]
        session = pipe.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        text = ""
        while time.monotonic() < pipe.deadline:
            value = pipe.call("Runtime.evaluate", {"expression": "document.body && document.body.innerText", "returnByValue": True}, session)
            text = value.get("result", {}).get("value", "") or ""
            if "Seccomp" in text:
                break
            time.sleep(0.05)
        (directory / "sandbox.txt").write_text(text)
        if not sandbox_enabled(text):
            raise RuntimeError("chrome://sandbox did not prove both isolation and Seccomp-BPF; see sandbox.txt")
        report["sandbox"] = "PASS"
        (directory / "browser.status").write_text(Path(f"/proc/{pid}/status").read_text())
        image = pipe.call("Page.captureScreenshot", {"format": "png"}, session)
        png = base64.b64decode(image["data"], validate=True)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("captureScreenshot did not return PNG")
        (directory / "sandbox.png").write_bytes(png)
        pipe.call("Browser.close")
        report["result"] = "PASS"
    except (OSError, RuntimeError, TimeoutError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    finally:
        for fd in fds:
            os.close(fd)
        if pid is not None:
            until = time.monotonic() + 2
            while time.monotonic() < until:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    reaped = True
                    report["browser_exit_code"] = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(0.02)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not reaped:
                _, status = os.waitpid(pid, 0)
                report["browser_exit_code"] = os.waitstatus_to_exitcode(status)
            if report["browser_exit_code"] != 0:
                report["result"] = "FAIL"
        until = time.monotonic() + 2
        remaining = private_processes(private)
        while remaining and time.monotonic() < until:
            time.sleep(0.02)
            remaining = private_processes(private)
        report["remaining_processes"] = remaining
        if remaining:
            # Do not delete live profiles or signal processes outside our group.
            report["cleanup"] = "FAIL"
            report["result"] = "FAIL"
        else:
            # Only remove the private directory created by this invocation.
            # Preserve stderr, version, screenshot and JSON evidence beside it.
            shutil.rmtree(private)
            report["cleanup"] = "PASS"
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return directory, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    options = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    directory, report = run(options.browser, options.scratch)
    print(json.dumps({"evidence": str(directory), **report}, indent=2))
    raise SystemExit(0 if report["result"] == "PASS" else 1)

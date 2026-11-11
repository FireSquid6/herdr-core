//! Opt-in native viewer browser transport. Not part of the agent method table.
//!
//! Account-authenticated socket/SSH software is trusted. Admission records native
//! client intent, not a claim to cryptographically distinguish humans from agents.
use crate::{rpc::RpcError, runtime::Core, wire::Conn};
use serde_json::{Value, json};
use std::{sync::Arc, sync::OnceLock};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

const WORKER: &str = include_str!("../../../scripts/browser_worker.py");
const MAX_REPLY: usize = 4 * 1024 * 1024;
static WORKER_STATE: OnceLock<tokio::sync::Mutex<Option<Worker>>> = OnceLock::new();

struct Pending<'a>(&'a std::sync::atomic::AtomicU32);

impl Drop for Pending<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, std::sync::atomic::Ordering::AcqRel);
    }
}

struct Worker {
    _child: tokio::process::Child,
    input: tokio::process::ChildStdin,
    output: BufReader<tokio::process::ChildStdout>,
}

fn unavailable() -> RpcError {
    RpcError::new(
        "unsupported_capability",
        "experimental native workspace browser is not enabled on this core",
        "use the feature binary on Linux with HC_BROWSER_CHROME pointing to supported sandboxed full Chrome",
    )
}

fn identity(conn: &Conn) -> &str {
    conn.browser_identity.get_or_init(crate::platform::random_hex)
}

impl Worker {
    fn start(core: &Core) -> Result<Self, RpcError> {
        let chrome = std::env::var("HC_BROWSER_CHROME").map_err(|_| unavailable())?;
        let mut child = tokio::process::Command::new("python3")
            .args(["-u", "-c", WORKER])
            .arg(core.paths.state.join("browsers"))
            .arg(chrome)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| {
                RpcError::new("browser_failed", e.to_string(), "install Python 3 and supported full Chrome")
            })?;
        let input = child.stdin.take().expect("piped stdin");
        let output = BufReader::new(child.stdout.take().expect("piped stdout"));
        Ok(Self { _child: child, input, output })
    }

    async fn call(&mut self, id: &str, method: &str, params: Value) -> Result<Value, RpcError> {
        let message = json!({"identity": id, "method": method, "params": params});
        let mut bytes = serde_json::to_vec(&message).map_err(io_error)?;
        if bytes.len() > 131_000 {
            return Err(RpcError::invalid_params("browser request exceeds 128KiB"));
        }
        bytes.push(b'\n');
        self.input.write_all(&bytes).await.map_err(io_error)?;
        let mut reply = Vec::new();
        // Read bounded chunks; even a corrupt private worker cannot allocate an
        // unbounded line in the core process.
        loop {
            let available = self.output.fill_buf().await.map_err(io_error)?;
            if available.is_empty() {
                return Err(io_error("browser worker closed its response pipe"));
            }
            let length = available.iter().position(|b| *b == b'\n').map_or(available.len(), |n| n + 1);
            if reply.len() + length > MAX_REPLY {
                return Err(io_error("browser response exceeds 4MiB"));
            }
            reply.extend_from_slice(&available[..length]);
            self.output.consume(length);
            if reply.last() == Some(&b'\n') {
                break;
            }
        }
        let reply: Value = serde_json::from_slice(&reply).map_err(io_error)?;
        if let Some(error) = reply.get("error") {
            return Err(RpcError::new(
                error["kind"].as_str().unwrap_or("browser_failed"),
                error["message"].as_str().unwrap_or("browser worker failed"),
                "check browser state; discard stale generation work and explicitly reacquire if needed",
            ));
        }
        Ok(reply["result"].clone())
    }
}

fn io_error(error: impl std::fmt::Display) -> RpcError {
    RpcError::new(
        "worker_failed",
        error.to_string(),
        "inspect isolated core/browser stderr; browser live state may be lost",
    )
}

pub async fn dispatch(core: &Core, conn: Option<&Arc<Conn>>, method: &str, params: Value) -> Result<Value, RpcError> {
    if std::env::var_os("HC_BROWSER_CHROME").is_none() {
        return Err(unavailable());
    }
    let conn = conn.ok_or_else(|| {
        RpcError::new(
            "viewer_required",
            "browser requires a dedicated framed native connection",
            "negotiate revision 2",
        )
    })?;
    if method == "browser.negotiate" {
        if params["revision"] != 2 {
            return Err(RpcError::new(
                "unsupported_version",
                "browser wire revision must be 2",
                "use revision 2 native client",
            ));
        }
        if params["client"] != "herdr-native" {
            return Err(RpcError::new(
                "viewer_required",
                "native client intent required",
                "use the native browser panel",
            ));
        }
        conn.browser_admitted.store(true, std::sync::atomic::Ordering::Release);
        return Ok(
            json!({"revision":2,"capabilities":["page_png","input","devtools_stream"],"lease_ms":30000,"max_frame_bytes":MAX_REPLY}),
        );
    }
    if !conn.browser_admitted.load(std::sync::atomic::Ordering::Acquire) {
        return Err(RpcError::new(
            "viewer_required",
            "native viewer has not negotiated",
            "call browser.negotiate on this dedicated connection",
        ));
    }
    if params.get("machine").is_some() {
        return Err(RpcError::invalid_params(
            "browser requires a direct dedicated connection to the workspace machine",
        ));
    }
    let pending = &conn.browser_pending;
    pending.fetch_add(1, std::sync::atomic::Ordering::AcqRel);
    let _pending = Pending(pending);
    if pending.load(std::sync::atomic::Ordering::Acquire) > 16 {
        return Err(RpcError::new(
            "slow_consumer",
            "too many outstanding browser requests",
            "keep one frame request outstanding per surface",
        ));
    }
    let mut state = WORKER_STATE.get_or_init(Default::default).lock().await;
    crate::server::check_cancelled()?;
    if conn.browser_closed.load(std::sync::atomic::Ordering::Acquire) {
        return Err(RpcError::new("not_owner", "viewer connection closed", "connect and explicitly reacquire"));
    }
    if state.is_none() {
        *state = Some(Worker::start(core)?);
    }
    let result = bounded_call(&mut state, identity(conn), method, params).await;
    crate::server::check_cancelled()?;
    if conn.browser_closed.load(std::sync::atomic::Ordering::Acquire) {
        return Err(RpcError::new("not_owner", "viewer connection closed during request", "discard pending work"));
    }
    result
}

// A hung/corrupt worker must not hold every browser request forever. Reset
// after timeout/transport loss, never reuse an ambiguously aligned stdio reply.
async fn bounded_call(state: &mut Option<Worker>, id: &str, method: &str, params: Value) -> Result<Value, RpcError> {
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(20),
        state.as_mut().expect("started worker").call(id, method, params),
    )
    .await
    .unwrap_or_else(|_| Err(io_error("private browser worker exceeded 20 second deadline")));
    if result.as_ref().is_err_and(|error| error.kind == "worker_failed") {
        // kill_on_drop closes both CDP transport ends when the worker dies.
        // A subsequent ensure may fail profile_locked rather than inventing a
        // running browser if Chromium has not finished exiting yet.
        state.take();
    }
    result
}

pub async fn disconnect(conn: &Conn) {
    conn.browser_closed.store(true, std::sync::atomic::Ordering::Release);
    let Some(id) = conn.browser_identity.get() else { return };
    let Some(state) = WORKER_STATE.get() else { return };
    let mut state = state.lock().await;
    if state.is_some() {
        let _ = bounded_call(&mut state, id, "disconnect", json!({})).await;
    }
}

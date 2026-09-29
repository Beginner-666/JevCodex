use super::protocol::RouteRequest;
use super::protocol::RouteResponse;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::AsyncBufReadExt;
use tokio::io::AsyncWriteExt;
use tokio::io::BufReader;
use tokio::io::Lines;
use tokio::process::Child;
use tokio::process::ChildStdin;
use tokio::process::ChildStdout;
use tokio::process::Command;

pub(super) struct SidecarClient {
    command: Vec<String>,
    jev_model: String,
    attempt_timeout: Duration,
    max_retries: u32,
    total_deadline: Duration,
    process: Option<SidecarProcess>,
}

struct SidecarProcess {
    _child: Child,
    stdin: ChildStdin,
    stdout: Lines<BufReader<ChildStdout>>,
}

impl SidecarClient {
    pub fn new(
        command: Vec<String>,
        jev_model: String,
        attempt_timeout: Duration,
        max_retries: u32,
        total_deadline: Duration,
    ) -> Self {
        Self {
            command,
            jev_model,
            attempt_timeout,
            max_retries,
            total_deadline,
            process: None,
        }
    }

    pub async fn request(&mut self, request: &RouteRequest) -> Result<RouteResponse, String> {
        match tokio::time::timeout(self.total_deadline, self.request_with_restart(request)).await {
            Ok(result) => result,
            Err(_) => {
                self.process = None;
                Err("timeout".to_string())
            }
        }
    }

    /// Start the persistent child during TUI bootstrap. The child remains lazy-restartable if it
    /// exits before the first route request (for example because its Python dependency is absent).
    pub async fn start(&mut self) -> Result<(), String> {
        if self.process.is_none() {
            self.process = Some(self.spawn().await?);
        }
        Ok(())
    }

    async fn request_with_restart(
        &mut self,
        request: &RouteRequest,
    ) -> Result<RouteResponse, String> {
        match self.request_once(request).await {
            Ok(response) => Ok(response),
            Err(first_error) => {
                self.process = None;
                self.request_once(request).await.map_err(|second_error| {
                    format!("{first_error}; restart failed: {second_error}")
                })
            }
        }
    }

    async fn request_once(&mut self, request: &RouteRequest) -> Result<RouteResponse, String> {
        if self.process.is_none() {
            self.process = Some(self.spawn().await?);
        }
        let Some(process) = self.process.as_mut() else {
            return Err("sidecar was not initialized".to_string());
        };
        let mut bytes = serde_json::to_vec(request).map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        process
            .stdin
            .write_all(&bytes)
            .await
            .map_err(|error| format!("stdin write failed: {error}"))?;
        process
            .stdin
            .flush()
            .await
            .map_err(|error| format!("stdin flush failed: {error}"))?;
        let line = process
            .stdout
            .next_line()
            .await
            .map_err(|error| format!("stdout read failed: {error}"))?
            .ok_or_else(|| "sidecar exited".to_string())?;
        let response: RouteResponse =
            serde_json::from_str(&line).map_err(|error| format!("invalid response: {error}"))?;
        if response.id != Some(request.id) {
            return Err("response id mismatch".to_string());
        }
        if !response.ok {
            return Err(response.error.unwrap_or_else(|| "jev error".to_string()));
        }
        Ok(response)
    }

    async fn spawn(&self) -> Result<SidecarProcess, String> {
        let (program, args) = self
            .command
            .split_first()
            .ok_or_else(|| "sidecar command is empty".to_string())?;
        let mut child = Command::new(program)
            .args(args)
            .env("JEV_CODEX_ROUTER_MODEL", &self.jev_model)
            .env(
                "JEV_CODEX_ROUTER_ATTEMPT_TIMEOUT_MS",
                self.attempt_timeout.as_millis().to_string(),
            )
            .env("JEV_CODEX_ROUTER_MAX_RETRIES", self.max_retries.to_string())
            .env(
                "JEV_CODEX_ROUTER_TOTAL_DEADLINE_MS",
                self.total_deadline.as_millis().to_string(),
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|error| format!("sidecar start failed: {error}"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "sidecar stdin unavailable".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "sidecar stdout unavailable".to_string())?;
        if let Some(stderr) = child.stderr.take() {
            tokio::spawn(async move {
                let mut lines = BufReader::new(stderr).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    tracing::warn!(message = %line, "Jev sidecar");
                }
            });
        }
        Ok(SidecarProcess {
            _child: child,
            stdin,
            stdout: BufReader::new(stdout).lines(),
        })
    }
}

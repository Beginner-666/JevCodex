use serde::Deserialize;
use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize)]
pub(super) struct RouteRequest {
    pub id: u64,
    #[serde(rename = "type")]
    pub request_type: &'static str,
    pub prompt: String,
    pub current_model: String,
    pub current_effort: String,
    pub previous_turn_status: &'static str,
    pub metrics: RoutingMetrics,
    pub profiles: BTreeMap<String, RouteProfile>,
}

/// Execution data is deliberately optional so the protocol can grow without coupling routing to
/// any one Codex event payload. V3 populates the native context size; later execution signals can
/// be added by the TUI without changing the sidecar's decision API.
#[derive(Debug, Clone, Default, Serialize)]
pub(super) struct RoutingMetrics {
    pub context_tokens: Option<u64>,
    pub previous_turn_failed: bool,
    pub failure_count: u32,
    pub files_touched: Option<u32>,
    pub diff_lines: Option<u32>,
    pub tool_count: Option<u32>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(super) enum RouteProfile {
    Keep,
    Route {
        model: String,
        effort: String,
        model_description: String,
        effort_description: String,
        routing_semantics: &'static str,
    },
}

#[derive(Debug, Clone, Deserialize)]
pub(super) struct RouteResponse {
    pub id: Option<u64>,
    pub ok: bool,
    pub choice: Option<String>,
    pub confidence: Option<f64>,
    pub probabilities: Option<BTreeMap<String, f64>>,
    pub latency_ms: Option<u64>,
    pub error: Option<String>,
}

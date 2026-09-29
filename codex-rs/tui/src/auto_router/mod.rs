//! Persistent, fail-open Jev routing at the TUI new-turn boundary.

mod process;
mod profiles;
mod protocol;

use codex_app_server_protocol::UserInput;
use codex_config::config_toml::AutoRouterModeToml;
use codex_config::config_toml::AutoRouterPromptStorageToml;
use codex_config::config_toml::AutoRouterToml;
use codex_protocol::openai_models::ModelPreset;
use codex_protocol::openai_models::ReasoningEffort;
use process::SidecarClient;
use profiles::ValidProfile;
use profiles::build_valid_profiles;
use profiles::current_rank;
use protocol::RouteRequest;
use protocol::RoutingMetrics;
use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeMap;
use std::collections::HashSet;
use std::collections::VecDeque;
use std::time::Duration;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

const DEFAULT_ATTEMPT_TIMEOUT_MS: u64 = 1_200;
const DEFAULT_TOTAL_DEADLINE_MS: u64 = 2_500;
const DEFAULT_MAX_RETRIES: u32 = 1;
const DEFAULT_MIN_CONFIDENCE: f64 = 0.60;
const DEFAULT_DOWNGRADE_CONFIDENCE: f64 = 0.80;
const DOWNGRADE_SELECTED_PROBABILITY: f64 = 0.65;
const DOWNGRADE_PROBABILITY_MARGIN: f64 = 0.25;
const DEFAULT_DOWNGRADE_MAX_CONTEXT_TOKENS: u64 = 20_000;
const DEFAULT_RETAIN_RECENT_DECISIONS: usize = 20;
const MAX_PROMPT_PREVIEW_CHARS: usize = 240;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AutoRouterControl {
    Status,
    On,
    Off,
    Auto,
    Explain,
    Pin { profile: String },
}

impl AutoRouterControl {
    pub(crate) fn parse(value: &str) -> Option<Self> {
        let parts = value
            .split_whitespace()
            .map(str::to_ascii_lowercase)
            .collect::<Vec<_>>();
        match parts.as_slice() {
            [] => Some(Self::Status),
            [command] if command == "status" => Some(Self::Status),
            [command] if command == "on" => Some(Self::On),
            [command] if command == "off" => Some(Self::Off),
            [command] if command == "auto" => Some(Self::Auto),
            [command] if command == "explain" => Some(Self::Explain),
            [command, family, effort]
                if command == "pin"
                    && matches!(family.as_str(), "luna" | "sol")
                    && matches!(effort.as_str(), "medium" | "high") =>
            {
                Some(Self::Pin {
                    profile: format!("{family}_{effort}"),
                })
            }
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RouteReason {
    JevAccepted,
    KeepCurrentRecommended,
    UserOverride,
    ManualPinned,
    LowConfidenceUpgradeRejected,
    LowConfidenceDowngradeRejected,
    DowngradeBlockedByContext,
    InvalidProfile,
    JevUnavailable,
    JevTimeout,
    ModelUnavailable,
    EffortUnavailable,
    AutoRouterDisabled,
    ManualSelectionPaused,
    EmptyPrompt,
}

impl RouteReason {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::JevAccepted => "jev-accepted",
            Self::KeepCurrentRecommended => "keep-current-recommended",
            Self::UserOverride => "user-override",
            Self::ManualPinned => "manual-pinned",
            Self::LowConfidenceUpgradeRejected => "low-confidence-upgrade-rejected",
            Self::LowConfidenceDowngradeRejected => "low-confidence-downgrade-rejected",
            Self::DowngradeBlockedByContext => "downgrade-blocked-by-context",
            Self::InvalidProfile => "invalid-profile",
            Self::JevUnavailable => "jev-unavailable",
            Self::JevTimeout => "jev-timeout",
            Self::ModelUnavailable => "model-unavailable",
            Self::EffortUnavailable => "effort-unavailable",
            Self::AutoRouterDisabled => "autorouter-disabled",
            Self::ManualSelectionPaused => "manual-selection-paused",
            Self::EmptyPrompt => "empty-prompt",
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct RoutingOutcome {
    pub(crate) model: String,
    pub(crate) effort: Option<ReasoningEffort>,
    pub(crate) notice: Option<String>,
    pub(crate) reason: RouteReason,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum RuntimeState {
    Disabled,
    Auto,
    ManualPaused,
    Pinned(String),
}

#[derive(Debug, Clone)]
struct Settings {
    configured: bool,
    enabled: bool,
    mode: AutoRouterModeToml,
    provider: String,
    jev_model: String,
    attempt_timeout: Duration,
    max_retries: u32,
    total_deadline: Duration,
    min_confidence: f64,
    upgrade_min_confidence: f64,
    downgrade_min_confidence: f64,
    downgrade_max_context_tokens: u64,
    allowed_models: HashSet<String>,
    min_profile: String,
    max_profile: String,
    allow_luna_high: bool,
    include_context_tokens: bool,
    logging_enabled: bool,
    log_prompts: bool,
    retain_recent_decisions: usize,
    store_prompt_text: AutoRouterPromptStorageToml,
    sidecar_command: Vec<String>,
}

impl Settings {
    fn from_toml(config: Option<&AutoRouterToml>) -> Self {
        let configured = config.is_some();
        let config = config.cloned().unwrap_or_default();
        let jev = config.jev.unwrap_or_default();
        let routing = config.routing.unwrap_or_default();
        let context = config.context.unwrap_or_default();
        let logging = config.logging.unwrap_or_default();
        let sidecar_command = jev.sidecar_command.unwrap_or_else(default_sidecar_command);
        let total_deadline_ms = jev
            .total_deadline_ms
            .or(jev.timeout_ms)
            .unwrap_or(DEFAULT_TOTAL_DEADLINE_MS)
            .max(1);
        let attempt_timeout_ms = jev
            .attempt_timeout_ms
            .unwrap_or(DEFAULT_ATTEMPT_TIMEOUT_MS)
            .max(1)
            .min(total_deadline_ms);
        Self {
            configured,
            enabled: configured && config.enabled.unwrap_or(true),
            mode: config.mode.unwrap_or_default(),
            provider: config
                .provider
                .unwrap_or_else(|| "vercel_ai_gateway".to_string()),
            jev_model: jev.model.unwrap_or_else(|| "typesafe-ai/jev".to_string()),
            attempt_timeout: Duration::from_millis(attempt_timeout_ms),
            max_retries: jev.max_retries.unwrap_or(DEFAULT_MAX_RETRIES),
            total_deadline: Duration::from_millis(total_deadline_ms),
            min_confidence: threshold(jev.min_confidence, DEFAULT_MIN_CONFIDENCE),
            upgrade_min_confidence: threshold(
                routing.upgrade_min_confidence,
                DEFAULT_MIN_CONFIDENCE,
            ),
            downgrade_min_confidence: threshold(
                routing.downgrade_min_confidence,
                DEFAULT_DOWNGRADE_CONFIDENCE,
            ),
            downgrade_max_context_tokens: routing
                .downgrade_max_context_tokens
                .unwrap_or(DEFAULT_DOWNGRADE_MAX_CONTEXT_TOKENS),
            allowed_models: routing
                .allowed_models
                .unwrap_or_else(|| vec!["gpt-5.6-luna".to_string(), "gpt-5.6-sol".to_string()])
                .into_iter()
                .collect(),
            min_profile: routing
                .min_profile
                .unwrap_or_else(|| "luna_medium".to_string()),
            max_profile: routing
                .max_profile
                .unwrap_or_else(|| "sol_high".to_string()),
            allow_luna_high: routing.allow_luna_high.unwrap_or(true),
            include_context_tokens: context.include_context_tokens.unwrap_or(true),
            logging_enabled: logging.enabled.unwrap_or(true),
            log_prompts: logging.log_prompts.unwrap_or(false),
            retain_recent_decisions: logging
                .retain_recent_decisions
                .unwrap_or(DEFAULT_RETAIN_RECENT_DECISIONS)
                .clamp(1, 1_000),
            store_prompt_text: logging.store_prompt_text.unwrap_or_default(),
            sidecar_command,
        }
    }
}

#[derive(Debug, Clone)]
struct RoutingRecord {
    timestamp_secs: u64,
    prompt_hash: String,
    prompt_preview: Option<String>,
    current_profile: String,
    jev_choice: Option<String>,
    jev_confidence: Option<f64>,
    probabilities: Option<BTreeMap<String, f64>>,
    metrics: RoutingMetrics,
    policy_final_profile: String,
    applied_final_profile: String,
    reason: RouteReason,
    routing_latency_ms: u64,
    shadow: bool,
}

pub(crate) struct AutoRouter {
    settings: Settings,
    catalog: Vec<ModelPreset>,
    sidecar: SidecarClient,
    next_id: u64,
    runtime_state: RuntimeState,
    unavailable_reported: bool,
    gateway_credentials_configured: bool,
    recent_decisions: VecDeque<RoutingRecord>,
}

impl AutoRouter {
    pub(crate) fn new(config: Option<&AutoRouterToml>, catalog: Vec<ModelPreset>) -> Self {
        let settings = Settings::from_toml(config);
        let sidecar = SidecarClient::new(
            settings.sidecar_command.clone(),
            settings.jev_model.clone(),
            settings.attempt_timeout,
            settings.max_retries,
            settings.total_deadline,
        );
        Self {
            runtime_state: if settings.enabled {
                RuntimeState::Auto
            } else {
                RuntimeState::Disabled
            },
            settings,
            catalog,
            sidecar,
            next_id: 1,
            unavailable_reported: false,
            gateway_credentials_configured: gateway_credentials_configured(),
            recent_decisions: VecDeque::new(),
        }
    }

    /// Eagerly launch the persistent sidecar after model discovery. The Gateway HTTP client inside
    /// the sidecar remains lazy, so missing credentials or dependencies cannot break TUI startup.
    pub(crate) async fn warm_up(&mut self) {
        if self.runtime_state != RuntimeState::Auto
            || !gateway_provider(&self.settings.provider)
            || !self.gateway_credentials_configured
        {
            return;
        }
        if let Err(error) = self.sidecar.start().await {
            tracing::warn!(%error, "failed to warm up Jev auto router sidecar");
        } else {
            tracing::info!("Jev auto router sidecar started");
        }
    }

    pub(crate) fn update_catalog(&mut self, catalog: Vec<ModelPreset>) {
        self.catalog = catalog;
    }

    pub(crate) fn control(&mut self, control: AutoRouterControl) -> String {
        match control {
            AutoRouterControl::On => self.runtime_state = RuntimeState::Auto,
            AutoRouterControl::Off => self.runtime_state = RuntimeState::Disabled,
            AutoRouterControl::Auto => {
                self.runtime_state = RuntimeState::Auto;
                self.settings.mode = AutoRouterModeToml::Auto;
            }
            AutoRouterControl::Pin { profile } => {
                let (profiles, _) = build_valid_profiles(&self.catalog, &self.settings);
                if !profiles.iter().any(|candidate| candidate.id == profile) {
                    return format!("Auto Router: cannot pin unavailable profile {profile}");
                }
                self.runtime_state = RuntimeState::Pinned(profile);
            }
            AutoRouterControl::Explain => return self.explain(),
            AutoRouterControl::Status => {}
        }
        self.status()
    }

    pub(crate) fn pause_for_manual_selection(&mut self) -> Option<String> {
        if matches!(
            self.runtime_state,
            RuntimeState::Auto | RuntimeState::Pinned(_)
        ) {
            self.runtime_state = RuntimeState::ManualPaused;
            Some(
                "Auto Router: paused by manual model selection · use /autoroute auto to resume"
                    .to_string(),
            )
        } else {
            None
        }
    }

    pub(crate) fn status(&self) -> String {
        match &self.runtime_state {
            RuntimeState::Disabled if !self.settings.configured => {
                "Auto Router: OFF · not configured".to_string()
            }
            RuntimeState::Disabled => "Auto Router: OFF".to_string(),
            RuntimeState::ManualPaused => {
                "Auto Router: PAUSED · manual model selection".to_string()
            }
            RuntimeState::Pinned(profile) => format!("Auto Router: PINNED · {profile}"),
            RuntimeState::Auto => {
                let mode = match self.settings.mode {
                    AutoRouterModeToml::Auto => "auto",
                    AutoRouterModeToml::Shadow => "shadow",
                };
                format!("Auto Router: ON · Jev · {mode}")
            }
        }
    }

    pub(crate) async fn route(
        &mut self,
        items: &[UserInput],
        current_model: String,
        current_effort: Option<ReasoningEffort>,
        context_tokens: Option<u64>,
    ) -> RoutingOutcome {
        let prompt = prompt_from_inputs(items);
        let effective_effort = current_effort.clone().unwrap_or_else(|| {
            self.catalog
                .iter()
                .find(|preset| preset.model == current_model)
                .map(|preset| preset.default_reasoning_effort.clone())
                .unwrap_or(ReasoningEffort::Medium)
        });
        let (profiles, mut criteria) = build_valid_profiles(&self.catalog, &self.settings);
        let current_profile = profile_label(&profiles, &current_model, &effective_effort);
        // `keep_current` already represents the current final action. Do not ask Jev to choose
        // between that and an identical concrete profile such as `sol_medium`.
        criteria.remove(&current_profile);
        let metrics = RoutingMetrics {
            context_tokens: self
                .settings
                .include_context_tokens
                .then_some(context_tokens)
                .flatten(),
            ..RoutingMetrics::default()
        };

        match self.runtime_state.clone() {
            RuntimeState::Disabled => {
                return self.fallback_with_record(
                    current_model,
                    current_effort,
                    &prompt,
                    current_profile,
                    metrics,
                    RouteReason::AutoRouterDisabled,
                    None,
                );
            }
            RuntimeState::ManualPaused => {
                return self.fallback_with_record(
                    current_model,
                    current_effort,
                    &prompt,
                    current_profile,
                    metrics,
                    RouteReason::ManualSelectionPaused,
                    None,
                );
            }
            RuntimeState::Pinned(profile) => {
                let Some(target) = profiles.iter().find(|candidate| candidate.id == profile) else {
                    return self.fallback_with_record(
                        current_model,
                        current_effort,
                        &prompt,
                        current_profile,
                        metrics,
                        RouteReason::ModelUnavailable,
                        Some(format!(
                            "◈ Jev pin unavailable: {profile} · keeping current"
                        )),
                    );
                };
                let target = target.clone();
                self.record_and_log(RoutingRecord::new(
                    &prompt,
                    &current_profile,
                    Some(target.id.to_string()),
                    None,
                    None,
                    metrics,
                    target.id.to_string(),
                    target.id.to_string(),
                    RouteReason::ManualPinned,
                    0,
                    false,
                    &self.settings,
                ));
                return RoutingOutcome {
                    model: target.model.clone(),
                    effort: Some(target.effort.clone()),
                    notice: Some(format!("◈ Jev pin: {}", display_profile(&target))),
                    reason: RouteReason::ManualPinned,
                };
            }
            RuntimeState::Auto => {}
        }

        if prompt.trim().is_empty() {
            return self.fallback_with_record(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::EmptyPrompt,
                None,
            );
        }
        if profiles.is_empty() {
            let notice = self.unavailable_notice("no valid routing profiles");
            return self.fallback_with_record(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::ModelUnavailable,
                notice,
            );
        }

        if let Some(explicit_profile) = detect_explicit_override(&prompt) {
            let Some(target) = profiles
                .iter()
                .find(|candidate| candidate.id == explicit_profile)
                .or_else(|| {
                    explicit_profile
                        .ends_with("_high")
                        .then(|| explicit_profile.replace("_high", "_medium"))
                        .and_then(|fallback| {
                            profiles.iter().find(|candidate| candidate.id == fallback)
                        })
                })
            else {
                return self.fallback_with_record(
                    current_model,
                    current_effort,
                    &prompt,
                    current_profile,
                    metrics,
                    RouteReason::EffortUnavailable,
                    Some(format!(
                        "◈ Jev: requested profile {explicit_profile} unavailable · keeping current"
                    )),
                );
            };
            let target = target.clone();
            self.record_and_log(RoutingRecord::new(
                &prompt,
                &current_profile,
                Some(target.id.to_string()),
                None,
                None,
                metrics,
                target.id.to_string(),
                target.id.to_string(),
                RouteReason::UserOverride,
                0,
                false,
                &self.settings,
            ));
            return RoutingOutcome {
                model: target.model.clone(),
                effort: Some(target.effort.clone()),
                notice: Some(format!(
                    "◈ Jev: user override · {}",
                    display_profile(&target)
                )),
                reason: RouteReason::UserOverride,
            };
        }

        if !gateway_provider(&self.settings.provider) || !self.gateway_credentials_configured {
            let notice = if !self.gateway_credentials_configured && !self.unavailable_reported {
                self.unavailable_reported = true;
                Some(
                    "Auto Router unavailable: AI_GATEWAY_API_KEY not configured · keeping current"
                        .to_string(),
                )
            } else {
                None
            };
            return self.fallback_with_record(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::JevUnavailable,
                notice,
            );
        }

        let request_id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        let request = RouteRequest {
            id: request_id,
            request_type: "route",
            prompt: prompt.clone(),
            current_model: current_model.clone(),
            current_effort: effective_effort.to_string(),
            previous_turn_status: "unknown",
            metrics: metrics.clone(),
            profiles: criteria,
        };
        let started = std::time::Instant::now();
        let response = match self.sidecar.request(&request).await {
            Ok(response) => response,
            Err(error) => {
                let reason = if error.to_ascii_lowercase().contains("timeout") {
                    RouteReason::JevTimeout
                } else {
                    RouteReason::JevUnavailable
                };
                let notice = self.unavailable_notice(&error);
                return self.fallback_with_record(
                    current_model,
                    current_effort,
                    &prompt,
                    current_profile,
                    metrics,
                    reason,
                    notice,
                );
            }
        };
        let elapsed_ms = response
            .latency_ms
            .unwrap_or_else(|| duration_millis_u64(started.elapsed()));
        let Some(choice) = response.choice.as_deref() else {
            return self.invalid_response_fallback(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::InvalidProfile,
                "missing choice",
                elapsed_ms,
            );
        };
        let Some(confidence) = response
            .confidence
            .filter(|value| (0.0..=1.0).contains(value))
        else {
            return self.invalid_response_fallback(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::InvalidProfile,
                "invalid confidence",
                elapsed_ms,
            );
        };
        if !valid_probabilities(response.probabilities.as_ref(), request.profiles.keys()) {
            return self.invalid_response_fallback(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::InvalidProfile,
                "invalid probability distribution",
                elapsed_ms,
            );
        }
        self.unavailable_reported = false;
        let probabilities = response
            .probabilities
            .as_ref()
            .map(|values| aggregate_final_action_probabilities(values, &current_profile));
        if choice == "keep_current" {
            self.record_and_log(RoutingRecord::new(
                &prompt,
                &current_profile,
                Some(choice.to_string()),
                Some(confidence),
                probabilities,
                metrics,
                current_profile.clone(),
                current_profile.clone(),
                RouteReason::KeepCurrentRecommended,
                elapsed_ms,
                self.settings.mode == AutoRouterModeToml::Shadow,
                &self.settings,
            ));
            return RoutingOutcome {
                model: current_model,
                effort: current_effort,
                notice: Some(format!(
                    "◈ Jev: keep {current_profile} · conf {confidence:.2}"
                )),
                reason: RouteReason::KeepCurrentRecommended,
            };
        }
        let Some(target) = profiles
            .iter()
            .find(|profile| profile.id == choice)
            .cloned()
        else {
            return self.invalid_response_fallback(
                current_model,
                current_effort,
                &prompt,
                current_profile,
                metrics,
                RouteReason::InvalidProfile,
                "unknown profile",
                elapsed_ms,
            );
        };
        let policy = self.policy_reason(
            &target,
            &profiles,
            &current_model,
            &effective_effort,
            confidence,
            probabilities.as_ref().expect("validated probabilities"),
            metrics.context_tokens,
        );
        let accepted = policy == RouteReason::JevAccepted;
        let policy_final = if accepted {
            target.id.to_string()
        } else {
            current_profile.clone()
        };
        let shadow = self.settings.mode == AutoRouterModeToml::Shadow;
        let applied_final = if accepted && !shadow {
            target.id.to_string()
        } else {
            current_profile.clone()
        };
        self.record_and_log(RoutingRecord::new(
            &prompt,
            &current_profile,
            Some(choice.to_string()),
            Some(confidence),
            probabilities,
            metrics,
            policy_final,
            applied_final,
            policy,
            elapsed_ms,
            shadow,
            &self.settings,
        ));

        let suggestion = display_profile(&target);
        if !accepted {
            return RoutingOutcome {
                model: current_model,
                effort: current_effort,
                notice: Some(format!(
                    "◈ Jev: keep {current_profile} · {} ({confidence:.2})",
                    policy.as_str()
                )),
                reason: policy,
            };
        }
        if shadow {
            return RoutingOutcome {
                model: current_model,
                effort: current_effort,
                notice: Some(format!("◈ Jev shadow: {suggestion} · conf {confidence:.2}")),
                reason: RouteReason::JevAccepted,
            };
        }
        RoutingOutcome {
            model: target.model,
            effort: Some(target.effort),
            notice: Some(format!("◈ Jev: {suggestion} · conf {confidence:.2}")),
            reason: RouteReason::JevAccepted,
        }
    }

    fn policy_reason(
        &self,
        target: &ValidProfile,
        profiles: &[ValidProfile],
        current_model: &str,
        current_effort: &ReasoningEffort,
        confidence: f64,
        probabilities: &BTreeMap<String, f64>,
        context_tokens: Option<u64>,
    ) -> RouteReason {
        match current_rank(profiles, current_model, current_effort) {
            Some(rank) if target.rank > rank => {
                if confidence < self.settings.min_confidence
                    || confidence < self.settings.upgrade_min_confidence
                {
                    RouteReason::LowConfidenceUpgradeRejected
                } else {
                    RouteReason::JevAccepted
                }
            }
            Some(rank) if target.rank < rank => {
                if downgrade_signal_count(
                    confidence,
                    self.settings.downgrade_min_confidence,
                    target.id,
                    probabilities,
                ) < 2
                {
                    RouteReason::LowConfidenceDowngradeRejected
                } else if context_tokens
                    .is_some_and(|tokens| tokens > self.settings.downgrade_max_context_tokens)
                {
                    RouteReason::DowngradeBlockedByContext
                } else {
                    RouteReason::JevAccepted
                }
            }
            Some(_) => RouteReason::KeepCurrentRecommended,
            None if confidence >= self.settings.min_confidence => RouteReason::JevAccepted,
            None => RouteReason::LowConfidenceUpgradeRejected,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn fallback_with_record(
        &mut self,
        model: String,
        effort: Option<ReasoningEffort>,
        prompt: &str,
        current_profile: String,
        metrics: RoutingMetrics,
        reason: RouteReason,
        notice: Option<String>,
    ) -> RoutingOutcome {
        self.record_and_log(RoutingRecord::new(
            prompt,
            &current_profile,
            None,
            None,
            None,
            metrics,
            current_profile.clone(),
            current_profile.clone(),
            reason,
            0,
            self.settings.mode == AutoRouterModeToml::Shadow,
            &self.settings,
        ));
        RoutingOutcome {
            model,
            effort,
            notice,
            reason,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn invalid_response_fallback(
        &mut self,
        model: String,
        effort: Option<ReasoningEffort>,
        prompt: &str,
        current_profile: String,
        metrics: RoutingMetrics,
        reason: RouteReason,
        error: &str,
        latency_ms: u64,
    ) -> RoutingOutcome {
        let notice = self.unavailable_notice(error);
        let outcome = self.fallback_with_record(
            model,
            effort,
            prompt,
            current_profile,
            metrics,
            reason,
            notice,
        );
        if let Some(record) = self.recent_decisions.back_mut() {
            record.routing_latency_ms = latency_ms;
        }
        outcome
    }

    fn unavailable_notice(&mut self, error: &str) -> Option<String> {
        tracing::warn!(%error, "Jev auto router unavailable; keeping current model");
        if self.unavailable_reported {
            None
        } else {
            self.unavailable_reported = true;
            Some("◈ Jev unavailable · keeping current model".to_string())
        }
    }

    fn record(&mut self, record: RoutingRecord) {
        self.recent_decisions.push_back(record);
        while self.recent_decisions.len() > self.settings.retain_recent_decisions {
            self.recent_decisions.pop_front();
        }
    }

    fn record_and_log(&mut self, record: RoutingRecord) {
        if self.settings.logging_enabled {
            if self.settings.log_prompts {
                tracing::info!(
                    prompt = record.prompt_preview.as_deref().unwrap_or("<not retained>"),
                    prompt_hash = record.prompt_hash,
                    current_profile = record.current_profile,
                    jev_choice = record.jev_choice,
                    confidence = record.jev_confidence,
                    final_profile = record.policy_final_profile,
                    applied_profile = record.applied_final_profile,
                    reason = record.reason.as_str(),
                    context_tokens = record.metrics.context_tokens,
                    latency_ms = record.routing_latency_ms,
                    shadow = record.shadow,
                    "Jev routing decision"
                );
            } else {
                tracing::info!(
                    prompt_hash = record.prompt_hash,
                    current_profile = record.current_profile,
                    jev_choice = record.jev_choice,
                    confidence = record.jev_confidence,
                    final_profile = record.policy_final_profile,
                    applied_profile = record.applied_final_profile,
                    reason = record.reason.as_str(),
                    context_tokens = record.metrics.context_tokens,
                    latency_ms = record.routing_latency_ms,
                    shadow = record.shadow,
                    "Jev routing decision"
                );
            }
        }
        self.record(record);
    }

    fn explain(&self) -> String {
        let Some(record) = self.recent_decisions.back() else {
            return "Auto Router: no routing decision has been recorded for this session."
                .to_string();
        };
        let probability_summary = record
            .probabilities
            .as_ref()
            .map(|probabilities| {
                probabilities
                    .iter()
                    .map(|(profile, probability)| format!("{profile}={probability:.2}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .unwrap_or_else(|| "n/a".to_string());
        let prompt = record.prompt_preview.as_deref().unwrap_or("not retained");
        let choice = record.jev_choice.as_deref().unwrap_or("n/a");
        let confidence = record
            .jev_confidence
            .map(|value| format!("{value:.2}"))
            .unwrap_or_else(|| "n/a".to_string());
        let context_tokens = record
            .metrics
            .context_tokens
            .map(|value| value.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        let mode = if record.shadow { "shadow" } else { "auto" };
        format!(
            "Auto Router · last decision\nTime: {}\nPrompt: {prompt}\nPrompt hash: {}\nCurrent profile: {}\nJev choice: {choice}\nConfidence: {confidence}\nContext tokens: {context_tokens}\nPrevious failure: {} · failure count: {}\nPolicy reason: {}\nPolicy final: {}\nApplied final: {} ({mode})\nRouting latency: {} ms\nProbabilities: {probability_summary}\nRetained decisions: {}/{}",
            record.timestamp_secs,
            record.prompt_hash,
            record.current_profile,
            record.metrics.previous_turn_failed,
            record.metrics.failure_count,
            record.reason.as_str(),
            record.policy_final_profile,
            record.applied_final_profile,
            record.routing_latency_ms,
            self.recent_decisions.len(),
            self.settings.retain_recent_decisions,
        )
    }
}

impl RoutingRecord {
    #[allow(clippy::too_many_arguments)]
    fn new(
        prompt: &str,
        current_profile: &str,
        jev_choice: Option<String>,
        jev_confidence: Option<f64>,
        probabilities: Option<BTreeMap<String, f64>>,
        metrics: RoutingMetrics,
        policy_final_profile: String,
        applied_final_profile: String,
        reason: RouteReason,
        routing_latency_ms: u64,
        shadow: bool,
        settings: &Settings,
    ) -> Self {
        Self {
            timestamp_secs: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
            prompt_hash: format!("{:x}", Sha256::digest(prompt.as_bytes())),
            prompt_preview: (settings.store_prompt_text
                == AutoRouterPromptStorageToml::SessionOnly)
                .then(|| prompt.chars().take(MAX_PROMPT_PREVIEW_CHARS).collect()),
            current_profile: current_profile.to_string(),
            jev_choice,
            jev_confidence,
            probabilities,
            metrics,
            policy_final_profile,
            applied_final_profile,
            reason,
            routing_latency_ms,
            shadow,
        }
    }
}

fn prompt_from_inputs(items: &[UserInput]) -> String {
    items
        .iter()
        .filter_map(|item| match item {
            UserInput::Text { text, .. } => Some(text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn detect_explicit_override(prompt: &str) -> Option<String> {
    let lower = prompt.to_ascii_lowercase();
    let compact = lower.split_whitespace().collect::<String>();
    for family in ["luna", "sol"] {
        let negated = [
            format!("do not use {family}"),
            format!("don't use {family}"),
            format!("不要用{family}"),
            format!("别用{family}"),
        ]
        .iter()
        .any(|pattern| lower.contains(pattern) || compact.contains(&pattern.replace(' ', "")));
        if negated {
            continue;
        }
        let instructed = [
            format!("use {family}"),
            format!("switch to {family}"),
            format!("with {family}"),
            format!("用{family}"),
            format!("使用{family}"),
            format!("切换到{family}"),
            format!("这次用{family}"),
        ]
        .iter()
        .any(|pattern| lower.contains(pattern) || compact.contains(&pattern.replace(' ', "")));
        if instructed {
            let high = lower.contains(&format!("{family} high"))
                || lower.contains(&format!("{family}/high"))
                || compact.contains(&format!("{family}high"));
            return Some(format!("{family}_{}", if high { "high" } else { "medium" }));
        }
    }
    None
}

fn profile_label(profiles: &[ValidProfile], model: &str, effort: &ReasoningEffort) -> String {
    profiles
        .iter()
        .find(|profile| profile.model == model && profile.effort == *effort)
        .map(|profile| profile.id.to_string())
        .unwrap_or_else(|| format!("{model}/{effort}"))
}

fn display_profile(profile: &ValidProfile) -> String {
    format!("{} / {}", display_model(&profile.model), profile.effort)
}

fn default_sidecar_command() -> Vec<String> {
    let python = std::env::var("JEV_CODEX_ROUTER_PYTHON").unwrap_or_else(|_| {
        if cfg!(windows) {
            "python".to_string()
        } else {
            "python3".to_string()
        }
    });
    vec![
        python,
        "-m".to_string(),
        "jev_codex_router.server".to_string(),
    ]
}

fn threshold(value: Option<f64>, default: f64) -> f64 {
    value
        .filter(|value| (0.0..=1.0).contains(value))
        .unwrap_or(default)
}

fn gateway_credentials_configured() -> bool {
    ["AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"]
        .iter()
        .any(|key| std::env::var(key).is_ok_and(|value| !value.trim().is_empty()))
}

fn gateway_provider(provider: &str) -> bool {
    matches!(provider, "vercel_ai_gateway" | "jev")
}

fn valid_probabilities<'a>(
    probabilities: Option<&BTreeMap<String, f64>>,
    expected_profiles: impl Iterator<Item = &'a String>,
) -> bool {
    let Some(probabilities) = probabilities else {
        return false;
    };
    let expected = expected_profiles
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let actual = probabilities
        .keys()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    if actual != expected
        || probabilities
            .values()
            .any(|value| !value.is_finite() || !(0.0..=1.0).contains(value))
    {
        return false;
    }
    (probabilities.values().sum::<f64>() - 1.0).abs() <= 0.01
}

fn aggregate_final_action_probabilities(
    probabilities: &BTreeMap<String, f64>,
    current_profile: &str,
) -> BTreeMap<String, f64> {
    let mut aggregated = BTreeMap::new();
    for (profile, probability) in probabilities {
        let final_profile = if profile == "keep_current" {
            current_profile
        } else {
            profile
        };
        *aggregated.entry(final_profile.to_string()).or_insert(0.0) += probability;
    }
    aggregated
}

fn downgrade_signal_count(
    confidence: f64,
    confidence_threshold: f64,
    selected_profile: &str,
    probabilities: &BTreeMap<String, f64>,
) -> usize {
    let selected_probability = probabilities
        .get(selected_profile)
        .copied()
        .unwrap_or_default();
    let runner_up_probability = probabilities
        .iter()
        .filter(|(profile, _)| profile.as_str() != selected_profile)
        .map(|(_, probability)| *probability)
        .fold(0.0, f64::max);
    let margin = selected_probability - runner_up_probability;

    usize::from(confidence >= confidence_threshold)
        + usize::from(selected_probability >= DOWNGRADE_SELECTED_PROBABILITY)
        + usize::from(margin >= DOWNGRADE_PROBABILITY_MARGIN)
}

fn display_model(model: &str) -> String {
    model
        .strip_prefix("gpt-")
        .unwrap_or(model)
        .split('-')
        .map(|part| {
            let mut characters = part.chars();
            characters
                .next()
                .map(|first| first.to_uppercase().chain(characters).collect())
                .unwrap_or_default()
        })
        .collect::<Vec<String>>()
        .join(" ")
}

fn duration_millis_u64(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

#[cfg(test)]
#[path = "auto_router_tests.rs"]
mod tests;

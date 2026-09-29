use super::*;
use codex_config::config_toml::AutoRouterJevToml;
use codex_protocol::openai_models::ReasoningEffortPreset;
use std::collections::BTreeMap;
use std::path::PathBuf;

fn model(model: &str, efforts: &[ReasoningEffort]) -> ModelPreset {
    ModelPreset {
        id: model.to_string(),
        model: model.to_string(),
        display_name: model.to_string(),
        description: format!("{model} description"),
        model_specialty: None,
        default_reasoning_effort: ReasoningEffort::Medium,
        supported_reasoning_efforts: efforts
            .iter()
            .cloned()
            .map(|effort| ReasoningEffortPreset {
                description: format!("{effort} description"),
                effort,
            })
            .collect(),
        supports_personality: false,
        additional_speed_tiers: Vec::new(),
        service_tiers: Vec::new(),
        default_service_tier: None,
        available_access_programs: None,
        is_default: false,
        upgrade: None,
        show_in_picker: true,
        multi_agent_version: None,
        availability_nux: None,
        supported_in_api: true,
        input_modalities: Vec::new(),
    }
}

fn routing_catalog() -> Vec<ModelPreset> {
    vec![
        model(
            "gpt-5.6-luna",
            &[ReasoningEffort::Medium, ReasoningEffort::High],
        ),
        model(
            "gpt-5.6-sol",
            &[ReasoningEffort::Medium, ReasoningEffort::High],
        ),
    ]
}

fn fake_sidecar_command(arguments: &[&str]) -> Vec<String> {
    let python = if cfg!(windows) { "python" } else { "python3" };
    let script =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../router/tests/fake_sidecar.py");
    std::iter::once(python.to_string())
        .chain(std::iter::once(script.to_string_lossy().into_owned()))
        .chain(arguments.iter().map(|argument| (*argument).to_string()))
        .collect()
}

fn router_config(arguments: &[&str], timeout_ms: u64, mode: AutoRouterModeToml) -> AutoRouterToml {
    AutoRouterToml {
        enabled: Some(true),
        mode: Some(mode),
        jev: Some(AutoRouterJevToml {
            timeout_ms: Some(timeout_ms),
            sidecar_command: Some(fake_sidecar_command(arguments)),
            ..AutoRouterJevToml::default()
        }),
        ..AutoRouterToml::default()
    }
}

fn user_prompt(text: &str) -> Vec<UserInput> {
    vec![UserInput::Text {
        text: text.to_string(),
        text_elements: Vec::new(),
    }]
}

#[test]
fn catalog_only_generates_supported_bounded_profiles() {
    let settings = Settings::from_toml(Some(&AutoRouterToml::default()));
    let catalog = vec![
        model(
            "gpt-5.6-luna",
            &[ReasoningEffort::Low, ReasoningEffort::Medium],
        ),
        model(
            "gpt-5.6-sol",
            &[
                ReasoningEffort::Medium,
                ReasoningEffort::High,
                ReasoningEffort::XHigh,
            ],
        ),
    ];
    let (profiles, criteria) = build_valid_profiles(&catalog, &settings);
    assert_eq!(
        profiles
            .iter()
            .map(|profile| profile.id)
            .collect::<Vec<_>>(),
        vec!["luna_medium", "sol_medium", "sol_high"]
    );
    assert_eq!(
        criteria.keys().cloned().collect::<Vec<_>>(),
        vec!["keep_current", "luna_medium", "sol_high", "sol_medium"]
    );
}

#[test]
fn confidence_gate_is_asymmetric() {
    let router = AutoRouter::new(Some(&AutoRouterToml::default()), Vec::new());
    let profiles = vec![
        ValidProfile {
            id: "luna_medium",
            model: "gpt-5.6-luna".to_string(),
            effort: ReasoningEffort::Medium,
            rank: 1,
        },
        ValidProfile {
            id: "sol_high",
            model: "gpt-5.6-sol".to_string(),
            effort: ReasoningEffort::High,
            rank: 4,
        },
    ];
    let upgrade_probabilities = BTreeMap::from([
        ("luna_medium".to_string(), 0.3),
        ("sol_high".to_string(), 0.7),
    ]);
    let uncertain_downgrade_probabilities = BTreeMap::from([
        ("luna_medium".to_string(), 0.55),
        ("sol_high".to_string(), 0.45),
    ]);
    let clear_downgrade_probabilities = BTreeMap::from([
        ("luna_medium".to_string(), 0.8),
        ("sol_high".to_string(), 0.2),
    ]);
    assert_eq!(
        router.policy_reason(
            &profiles[1],
            &profiles,
            "gpt-5.6-luna",
            &ReasoningEffort::Medium,
            0.7,
            &upgrade_probabilities,
            Some(1_000),
        ),
        RouteReason::JevAccepted
    );
    assert_eq!(
        router.policy_reason(
            &profiles[0],
            &profiles,
            "gpt-5.6-sol",
            &ReasoningEffort::High,
            0.7,
            &uncertain_downgrade_probabilities,
            Some(1_000),
        ),
        RouteReason::LowConfidenceDowngradeRejected
    );
    assert_eq!(
        router.policy_reason(
            &profiles[0],
            &profiles,
            "gpt-5.6-sol",
            &ReasoningEffort::High,
            0.9,
            &clear_downgrade_probabilities,
            Some(1_000),
        ),
        RouteReason::JevAccepted
    );
    assert_eq!(
        router.policy_reason(
            &profiles[0],
            &profiles,
            "gpt-5.6-sol",
            &ReasoningEffort::High,
            0.9,
            &clear_downgrade_probabilities,
            Some(80_000),
        ),
        RouteReason::DowngradeBlockedByContext
    );
}

#[test]
fn final_action_probabilities_merge_keep_current_with_current_profile() {
    let probabilities = BTreeMap::from([
        ("keep_current".to_string(), 0.28),
        ("luna_medium".to_string(), 0.71),
        ("sol_medium".to_string(), 0.01),
    ]);

    assert_eq!(
        aggregate_final_action_probabilities(&probabilities, "sol_medium"),
        BTreeMap::from([
            ("luna_medium".to_string(), 0.71),
            ("sol_medium".to_string(), 0.29),
        ])
    );
}

#[test]
fn downgrade_accepts_two_of_three_signals() {
    let router = AutoRouter::new(Some(&AutoRouterToml::default()), Vec::new());
    let profiles = vec![
        ValidProfile {
            id: "luna_medium",
            model: "gpt-5.6-luna".to_string(),
            effort: ReasoningEffort::Medium,
            rank: 1,
        },
        ValidProfile {
            id: "sol_medium",
            model: "gpt-5.6-sol".to_string(),
            effort: ReasoningEffort::Medium,
            rank: 2,
        },
    ];
    let probabilities = BTreeMap::from([
        ("luna_medium".to_string(), 0.71),
        ("sol_medium".to_string(), 0.29),
    ]);

    assert_eq!(
        router.policy_reason(
            &profiles[0],
            &profiles,
            "gpt-5.6-sol",
            &ReasoningEffort::Medium,
            0.64,
            &probabilities,
            Some(1_000),
        ),
        RouteReason::JevAccepted
    );
}

#[test]
fn malformed_probability_distributions_are_rejected() {
    let expected = ["keep_current".to_string(), "luna_medium".to_string()];
    assert!(!valid_probabilities(None, expected.iter()));
    assert!(!valid_probabilities(
        Some(&BTreeMap::from([
            ("keep_current".to_string(), 0.2),
            ("luna_medium".to_string(), 0.2),
        ])),
        expected.iter(),
    ));
    assert!(valid_probabilities(
        Some(&BTreeMap::from([
            ("keep_current".to_string(), 0.2),
            ("luna_medium".to_string(), 0.8),
        ])),
        expected.iter(),
    ));
}

#[test]
fn explicit_override_detection_is_instruction_scoped_and_bilingual() {
    assert_eq!(
        detect_explicit_override("use Sol high for this migration").as_deref(),
        Some("sol_high")
    );
    assert_eq!(
        detect_explicit_override("这次用 Luna 修复拼写").as_deref(),
        Some("luna_medium")
    );
    assert_eq!(
        detect_explicit_override("Sol is mentioned in the docs"),
        None
    );
    assert_eq!(detect_explicit_override("do not use Sol for this"), None);
}

#[test]
fn manual_model_selection_pauses_until_auto_resumes() {
    let mut router = AutoRouter::new(Some(&AutoRouterToml::default()), routing_catalog());
    assert!(
        router
            .pause_for_manual_selection()
            .is_some_and(|notice| notice.contains("paused"))
    );
    assert_eq!(router.runtime_state, RuntimeState::ManualPaused);
    assert_eq!(
        router.control(AutoRouterControl::Auto),
        "Auto Router: ON · Jev · auto"
    );
    assert_eq!(router.runtime_state, RuntimeState::Auto);
}

#[test]
fn auto_control_enables_routing_and_leaves_shadow_mode() {
    let config = AutoRouterToml {
        enabled: Some(false),
        mode: Some(AutoRouterModeToml::Shadow),
        ..AutoRouterToml::default()
    };
    let mut router = AutoRouter::new(Some(&config), routing_catalog());

    assert_eq!(
        router.control(AutoRouterControl::Auto),
        "Auto Router: ON · Jev · auto"
    );
    assert_eq!(router.runtime_state, RuntimeState::Auto);
    assert_eq!(router.settings.mode, AutoRouterModeToml::Auto);
}

#[tokio::test]
async fn timeout_fails_open_to_the_current_profile() {
    let config = router_config(
        &["--delay-ms", "200", "--choice", "sol_high"],
        30,
        AutoRouterModeToml::Auto,
    );
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    router.gateway_credentials_configured = true;
    router.warm_up().await;

    let started = std::time::Instant::now();
    let outcome = router
        .route(
            &user_prompt("investigate the failure"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::Medium),
            Some(100),
        )
        .await;

    assert_eq!(outcome.model, "gpt-5.6-luna");
    assert_eq!(outcome.effort, Some(ReasoningEffort::Medium));
    assert_eq!(
        outcome.notice.as_deref(),
        Some("◈ Jev unavailable · keeping current model")
    );
    assert!(started.elapsed() < Duration::from_millis(500));
}

#[tokio::test]
async fn unknown_profile_fails_open() {
    let config = router_config(
        &["--choice", "unknown_profile"],
        800,
        AutoRouterModeToml::Auto,
    );
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    router.gateway_credentials_configured = true;

    let outcome = router
        .route(
            &user_prompt("do the work"),
            "gpt-5.6-sol".to_string(),
            Some(ReasoningEffort::Medium),
            Some(100),
        )
        .await;

    assert_eq!(outcome.model, "gpt-5.6-sol");
    assert_eq!(outcome.effort, Some(ReasoningEffort::Medium));
    assert_eq!(
        outcome.notice.as_deref(),
        Some("◈ Jev unavailable · keeping current model")
    );
}

#[tokio::test]
async fn missing_api_key_warns_once_and_keeps_current() {
    let config = router_config(&[], 800, AutoRouterModeToml::Auto);
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    router.gateway_credentials_configured = false;

    let first = router
        .route(
            &user_prompt("first"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::High),
            Some(100),
        )
        .await;
    let second = router
        .route(
            &user_prompt("second"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::High),
            Some(100),
        )
        .await;

    assert_eq!(first.model, "gpt-5.6-luna");
    assert!(
        first
            .notice
            .as_deref()
            .is_some_and(|notice| notice.contains("not configured"))
    );
    assert_eq!(second.model, "gpt-5.6-luna");
    assert_eq!(second.notice, None);
}

#[tokio::test]
async fn shadow_mode_reports_but_does_not_apply_the_route() {
    let config = router_config(
        &["--choice", "sol_high", "--confidence", "0.99"],
        800,
        AutoRouterModeToml::Shadow,
    );
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    router.gateway_credentials_configured = true;

    let outcome = router
        .route(
            &user_prompt("deep architecture investigation"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::Medium),
            Some(8_000),
        )
        .await;

    assert_eq!(outcome.model, "gpt-5.6-luna");
    assert_eq!(outcome.effort, Some(ReasoningEffort::Medium));
    assert!(
        outcome
            .notice
            .as_deref()
            .is_some_and(|notice| notice.contains("Jev shadow"))
    );
}

#[tokio::test]
async fn explicit_user_override_skips_jev_and_wins() {
    let config = router_config(&["--choice", "luna_medium"], 800, AutoRouterModeToml::Auto);
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    router.gateway_credentials_configured = true;

    let outcome = router
        .route(
            &user_prompt("这次用 Sol high 完成重构"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::Medium),
            Some(2_000),
        )
        .await;

    assert_eq!(outcome.model, "gpt-5.6-sol");
    assert_eq!(outcome.effort, Some(ReasoningEffort::High));
    assert_eq!(outcome.reason, RouteReason::UserOverride);
}

#[tokio::test]
async fn pin_does_not_call_jev_and_auto_clears_it() {
    let config = router_config(&["--delay-ms", "500"], 800, AutoRouterModeToml::Auto);
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    assert_eq!(
        router.control(AutoRouterControl::Pin {
            profile: "sol_medium".to_string(),
        }),
        "Auto Router: PINNED · sol_medium"
    );

    let started = std::time::Instant::now();
    let outcome = router
        .route(
            &user_prompt("small change"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::Medium),
            Some(100),
        )
        .await;
    assert!(started.elapsed() < Duration::from_millis(100));
    assert_eq!(outcome.model, "gpt-5.6-sol");
    assert_eq!(outcome.reason, RouteReason::ManualPinned);
    assert_eq!(
        router.control(AutoRouterControl::Auto),
        "Auto Router: ON · Jev · auto"
    );
}

#[tokio::test]
async fn explain_reads_session_history_without_new_route() {
    let config = router_config(&["--choice", "keep_current"], 800, AutoRouterModeToml::Auto);
    let mut router = AutoRouter::new(Some(&config), routing_catalog());
    router.gateway_credentials_configured = true;
    let _ = router
        .route(
            &user_prompt("inspect this"),
            "gpt-5.6-luna".to_string(),
            Some(ReasoningEffort::Medium),
            Some(8_400),
        )
        .await;
    let count = router.recent_decisions.len();

    let explanation = router.control(AutoRouterControl::Explain);

    assert!(explanation.contains("keep-current-recommended"));
    assert!(explanation.contains("Context tokens: 8400"));
    assert_eq!(router.recent_decisions.len(), count);
}

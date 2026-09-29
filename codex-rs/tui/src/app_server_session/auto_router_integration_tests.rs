//! Integration invariant for adaptive per-turn model overrides.

use super::*;
use crate::legacy_core::config::ConfigBuilder;
use codex_app_server_protocol::ServerNotification;
use core_test_support::responses;
use pretty_assertions::assert_eq;
use serde_json::Value;

async fn start_and_complete_turn(
    app_server: &mut AppServerSession,
    config: &Config,
    thread_id: ThreadId,
    model: &str,
    prompt: &str,
) -> Result<()> {
    let response = app_server
        .turn_start(
            thread_id,
            format!("client-{prompt}"),
            vec![UserInput::Text {
                text: prompt.to_string(),
                text_elements: Vec::new(),
            }],
            config.cwd.to_path_buf(),
            /*approval_policy*/ None,
            /*approvals_reviewer*/ None,
            TurnPermissionsOverride::Preserve,
            &config.workspace_roots,
            model.to_string(),
            Some(codex_protocol::openai_models::ReasoningEffort::Medium),
            /*summary*/ None,
            /*service_tier*/ None,
            /*collaboration_mode*/ None,
            /*personality*/ None,
            /*output_schema*/ None,
        )
        .await?;
    let expected_turn_id = response.turn.id;
    tokio::time::timeout(Duration::from_secs(30), async {
        while let Some(event) = app_server.next_event().await {
            if let AppServerEvent::ServerNotification(notification) = event
                && let ServerNotification::TurnCompleted(completed) = *notification
                && completed.turn.id == expected_turn_id
            {
                assert_eq!(completed.thread_id, thread_id.to_string());
                return;
            }
        }
        panic!("app-server disconnected before completing turn {expected_turn_id}");
    })
    .await?;
    Ok(())
}

#[test]
fn model_overrides_keep_one_thread_and_preserve_prior_context() {
    std::thread::Builder::new()
        .name("auto-router-integration".to_string())
        .stack_size(16 * 1024 * 1024)
        .spawn(|| {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("build integration runtime");
            runtime
                .block_on(model_overrides_keep_one_thread_and_preserve_prior_context_inner())
                .expect("same-thread integration succeeds");
        })
        .expect("spawn integration thread")
        .join()
        .expect("integration thread does not panic");
}

async fn model_overrides_keep_one_thread_and_preserve_prior_context_inner() -> Result<()> {
    let server = responses::start_mock_server().await;
    let model_requests = responses::mount_sse_sequence(
        &server,
        vec![
            responses::sse(vec![
                responses::ev_response_created("response-1"),
                responses::ev_assistant_message("message-1", "answer-one"),
                responses::ev_completed("response-1"),
            ]),
            responses::sse(vec![
                responses::ev_response_created("response-2"),
                responses::ev_assistant_message("message-2", "answer-two"),
                responses::ev_completed("response-2"),
            ]),
            responses::sse(vec![
                responses::ev_response_created("response-3"),
                responses::ev_assistant_message("message-3", "answer-three"),
                responses::ev_completed("response-3"),
            ]),
        ],
    )
    .await;
    let home = tempfile::tempdir()?;
    std::fs::write(
        home.path().join("config.toml"),
        format!(
            r#"
model = "gpt-5.6-luna"
model_provider = "same-thread-test"
[model_providers.same-thread-test]
name = "OpenAI"
base_url = "{}/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
"#,
            server.uri()
        ),
    )?;
    let config = ConfigBuilder::default()
        .codex_home(home.path().to_path_buf())
        .build()
        .await?;
    let mut app_server = crate::start_embedded_app_server_for_picker(&config).await?;
    let started = app_server.start_thread(&config).await?;
    let thread_id = started.session.thread_id;

    for (model, prompt) in [
        ("gpt-5.6-luna", "turn-one"),
        ("gpt-5.6-sol", "turn-two"),
        ("gpt-5.6-luna", "turn-three"),
    ] {
        start_and_complete_turn(&mut app_server, &config, thread_id, model, prompt).await?;
    }

    let requests = model_requests.requests();
    assert_eq!(requests.len(), 3);
    assert_eq!(
        requests
            .iter()
            .map(|request| request.body_json()["model"].clone())
            .collect::<Vec<_>>(),
        vec![
            Value::String("gpt-5.6-luna".to_string()),
            Value::String("gpt-5.6-sol".to_string()),
            Value::String("gpt-5.6-luna".to_string()),
        ]
    );
    for request in &requests {
        let metadata: Value = serde_json::from_str(
            request.body_json()["client_metadata"]["x-codex-turn-metadata"]
                .as_str()
                .expect("canonical turn metadata"),
        )?;
        assert_eq!(metadata["thread_id"], thread_id.to_string());
    }
    assert!(requests[1].body_contains_text("turn-one"));
    assert!(requests[1].body_contains_text("answer-one"));
    assert!(requests[2].body_contains_text("turn-one"));
    assert!(requests[2].body_contains_text("turn-two"));
    assert!(requests[2].body_contains_text("answer-two"));

    app_server.shutdown().await?;
    Ok(())
}

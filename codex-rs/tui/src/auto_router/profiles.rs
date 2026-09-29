use super::Settings;
use super::protocol::RouteProfile;
use codex_protocol::openai_models::ModelPreset;
use codex_protocol::openai_models::ReasoningEffort;
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ValidProfile {
    pub id: &'static str,
    pub model: String,
    pub effort: ReasoningEffort,
    pub rank: usize,
}

const PROFILE_ORDER: [&str; 4] = ["luna_medium", "luna_high", "sol_medium", "sol_high"];

pub(super) fn build_valid_profiles(
    catalog: &[ModelPreset],
    settings: &Settings,
) -> (Vec<ValidProfile>, BTreeMap<String, RouteProfile>) {
    let min_rank = configured_rank(&settings.min_profile).unwrap_or(1);
    let max_rank = configured_rank(&settings.max_profile).unwrap_or(PROFILE_ORDER.len());
    let mut profiles = Vec::new();
    let mut criteria = BTreeMap::new();
    criteria.insert("keep_current".to_string(), RouteProfile::Keep);

    for preset in catalog
        .iter()
        .filter(|model| model.show_in_picker && settings.allowed_models.contains(&model.model))
    {
        let family = if is_luna(&preset.model) {
            "luna"
        } else if is_sol(&preset.model) {
            "sol"
        } else {
            continue;
        };
        for (effort, suffix, semantics) in [
            (
                ReasoningEffort::Medium,
                "medium",
                if family == "luna" {
                    "Minimum allowed profile. Use for simple, local, well-specified coding tasks, mechanical edits, straightforward searches, documentation changes, small configuration changes, and low-complexity implementation."
                } else {
                    "Use for difficult coding or debugging involving broader repository context, ambiguity, multiple interacting components, non-trivial design decisions, or root-cause investigation."
                },
            ),
            (
                ReasoningEffort::High,
                "high",
                if family == "luna" {
                    "Use for moderately complex work that benefits from deeper reasoning but does not require Sol, such as ordinary debugging, multi-file implementation, test writing, and moderate refactoring."
                } else {
                    "Maximum allowed profile. Reserve for the hardest tasks: deep root-cause debugging, architecture-level decisions, highly ambiguous multi-step work, complex reasoning across many components, or tasks where mistakes are expensive."
                },
            ),
        ] {
            if family == "luna" && suffix == "high" && !settings.allow_luna_high {
                continue;
            }
            let id = match (family, suffix) {
                ("luna", "medium") => "luna_medium",
                ("luna", "high") => "luna_high",
                ("sol", "medium") => "sol_medium",
                ("sol", "high") => "sol_high",
                _ => continue,
            };
            let Some(rank) = configured_rank(id) else {
                continue;
            };
            if rank < min_rank || rank > max_rank {
                continue;
            }
            let Some(effort_preset) = preset
                .supported_reasoning_efforts
                .iter()
                .find(|candidate| candidate.effort == effort)
            else {
                continue;
            };
            profiles.push(ValidProfile {
                id,
                model: preset.model.clone(),
                effort: effort.clone(),
                rank,
            });
            criteria.insert(
                id.to_string(),
                RouteProfile::Route {
                    model: preset.model.clone(),
                    effort: effort.to_string(),
                    model_description: preset.description.clone(),
                    effort_description: effort_preset.description.clone(),
                    routing_semantics: semantics,
                },
            );
        }
    }
    profiles.sort_by_key(|profile| profile.rank);
    // Ranks describe the capability order that is actually available to this account. If Luna
    // high is absent, Sol medium becomes rank 2 instead of leaving a meaningless gap.
    for (index, profile) in profiles.iter_mut().enumerate() {
        profile.rank = index + 1;
    }
    (profiles, criteria)
}

pub(super) fn current_rank(
    profiles: &[ValidProfile],
    model: &str,
    effort: &ReasoningEffort,
) -> Option<usize> {
    profiles
        .iter()
        .find(|profile| profile.model == model && profile.effort == *effort)
        .map(|profile| profile.rank)
}

fn configured_rank(profile: &str) -> Option<usize> {
    PROFILE_ORDER
        .iter()
        .position(|candidate| *candidate == profile)
        .map(|rank| rank + 1)
}

fn is_luna(model: &str) -> bool {
    model.eq_ignore_ascii_case("luna") || model.to_ascii_lowercase().contains("-luna")
}

fn is_sol(model: &str) -> bool {
    model.eq_ignore_ascii_case("sol") || model.to_ascii_lowercase().contains("-sol")
}

# Security Policy

## Secrets and authentication data

JevCodex uses the normal Codex login for generation and `OPENROUTER_API_KEY` for Jev routing. Neither credential belongs in this repository.

Do not commit:

- `.env`, `.env.local`, or another environment file containing real values;
- `~/.codex/auth.json`, session files, cookies, tokens, or browser profiles;
- personal `~/.codex/config.toml` files containing local paths or credentials;
- terminal transcripts, benchmark artifacts, or logs that may contain prompts or headers;
- prebuilt packages copied from a user-specific Codex installation.

The root `.gitignore` ignores `.env*` except the placeholder `.env.example`. The sidecar reads the OpenRouter key at runtime and does not include it in NDJSON responses.

Before publishing, inspect the working tree, staged diff, and commit history for secrets. If a real credential has ever been committed, deleting it later is insufficient: revoke it, issue a new credential, and remove it from Git history before publishing.

## Prompt data

The auto-router records a SHA-256 prompt hash for explanations. Full prompt logging is disabled by default. With `store_prompt_text = "session_only"`, only a short preview is retained in memory for the current TUI process.

Benchmark artifacts can contain prompts, agent messages, command output, diffs, and absolute paths. Treat `e2e-artifacts/` as private unless reviewed and sanitized.

## Reporting a vulnerability

Do not open a public issue containing credentials, private prompts, or exploit details. Contact the repository owner privately through GitHub first.

For vulnerabilities in Codex or code inherited from upstream, use OpenAI's official [Bugcrowd program](https://bugcrowd.com/engagements/openai). For Codex security boundaries, sandboxing, approvals, and network controls, see [Agent approvals & security](https://developers.openai.com/codex/agent-approvals-security).

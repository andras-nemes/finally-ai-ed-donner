# Review

## Findings

1. High - `README.md` documents runnable project commands and directories that are not present in the repository. The Quick Start tells users to copy `.env.example`, run `./scripts/start_mac.sh` / `.\scripts\start_windows.ps1`, and run Playwright from `test/docker-compose.test.yml` (`README.md:33`, `README.md:43`, `README.md:48`, `README.md:89-93`), but this checkout has no `.env.example`, `scripts/`, `test/`, `Dockerfile`, or `docker-compose.test.yml`. Anyone following the new README will fail before starting the app. Either add the referenced files as part of this change or make the README match the currently available backend-only workflow.

2. High - The plan now says the Massive market data integration is not implemented, but the codebase still implements and selects it. `planning/PLAN.md:155-157` calls Massive a future placeholder, while `backend/app/market/factory.py:24-28` instantiates `MassiveDataSource` whenever `MASSIVE_API_KEY` is set, `backend/app/market/massive_client.py` contains the poller implementation, and the backend tests cover that path. This contradiction will mislead future implementers and reviewers about whether the real-data path exists. Restore the optional Massive wording or remove/disable the implementation consistently.

3. Medium - The LLM provider documentation is internally inconsistent. `README.md:33` and `README.md:63` still say users need an OpenRouter API key, but the variable is now `OPENAI_API_KEY`; `planning/PLAN.md:124-125` has the same OpenRouter/OpenAI mismatch. The Cerebras skill is also inconsistent: its frontmatter still describes LiteLLM + OpenRouter + Cerebras (`.claude/skills/cerebras/SKILL.md:2-3`), while the body switches to `OPENAI_API_KEY` and `MODEL = "gpt-4o"` but keeps `EXTRA_BODY = {"provider": {"order": ["cerebras"]}}` (`.claude/skills/cerebras/SKILL.md:13`, `.claude/skills/cerebras/SKILL.md:26-27`). Pick one provider path and update the env var, model name, dependency guidance, and examples together.

4. Medium - The new repository-level Claude Stop hook unconditionally runs Codex and overwrites `planning/REVIEW.md` on every stop event (`.claude/settings.json:7-14`). That means normal Claude sessions can unexpectedly dirty the worktree, spend review time, and replace a review file the user may have edited or wanted to preserve. If the independent review should be opt-in, keep it behind the plugin/command or add a guard so it only runs when explicitly requested.

## Notes

- Reviewed tracked changes and untracked additions under `.claude-plugin/`, `.claude/agents/`, `.claude/commands/`, and `independent-reviewer/`.
- No tests were run; the changes under review are documentation and Claude/plugin configuration.

# Project Instructions

- This project runs research through native OpenCode agents and the local evidence MCP. Keep model/provider authentication in OpenCode; do not pin a provider or commit credentials.
- For development that depends on third-party libraries, SDKs, APIs or configuration, use Context7 automatically: resolve-library-id first, then query-docs for the relevant version and task. Do not require the user to say "use context7" each time.
- Cross-check Context7 examples against the installed version, current official documentation and executable tests. Indexed documentation can be stale. Use the current OpenCode V2 project config; do not copy obsolete Kilo, Codex, Grok, or OpenCode V1 configuration into this project.
- Context7 is a coding-documentation source, not a source of live crypto prices, token status or economic facts. Documentation queries contain only public library names and API questions.
- Research collection must use crypto_evidence to retain budget accounting and saved evidence. Direct CoinGecko/Etherscan MCP calls require approval and are auxiliary only: they are not automatically archived or budgeted. Never bypass a research deadline, spend paid credits or treat an uncited MCP answer as a verified report fact.
- Project questions default to deep crypto-research: question-driven broad discovery, original primary and independent sources, targeted gap closure, detailed Russian synthesis and a sufficiency review. Quick scans require an explicit request. Preserve request/deadline ceilings; maximize useful evidence, not duplicate pages. New runs require coverage.json; structural ok is not proof of a complete answer or justified valuation. Use crypto-code for implementation.
- The critic and existing-run audit make no new external requests. Research agents edit only their assigned run artifacts; select crypto-code for implementation work.
- Keep README concise, env.example free of real secrets, and .env/artifacts/reports excluded from Git. Publish only the final full study in reports/ through the checked local publisher; service artifacts stay in artifacts/runs. Export the environment before OpenCode starts; do not claim automatic dotenv loading.
- Validate with `python3 -m unittest discover -s tests -v`, `opencode debug config`, and `opencode debug agents`. Do not invoke a model-backed research run or change global configuration unless requested.

## Git identity

Author and committer must be the user from `git config user.name` and `git config user.email`.

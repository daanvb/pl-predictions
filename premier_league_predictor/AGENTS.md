# Project working preferences

Use economy mode by default. Keep investigation, tool output and explanations focused on the requested change.

## Model preferences

- Small CSS, wording and icon changes: GPT-5.6 Luna, low reasoning.
- Normal development: GPT-5.6 Terra, medium reasoning.
- Difficult architecture or debugging: GPT-6 Astra, high reasoning.
- These are selection preferences, not automatic model switching. Do not claim a model change unless a tool confirms it. When explicitly asked to create a new task, use the appropriate model and reasoning setting.
- Do not create extra tasks or delegate to agents merely to change models or save usage.

## Implementation and validation

- Batch related small UI adjustments and inspect only relevant code.
- Run targeted tests only during development; no Docker or browser QA unless necessary.
- For a batch needing visual validation, perform one browser check after the batch. Repeat only if a failure or subsequent fix requires it.
- Capture the relevant element or area directly where supported. Avoid full-screen screenshots and separate resizing workflows.
- Run the full Docker build before publishing an HA release, or when build-sensitive changes require it. The build must pass, including its complete regression suite, before publication.
- Treat app version and changelog updates as release-sensitive. Keep both changelogs identical and verify version consistency.
- For a user-requested stealth update, bump only the Home Assistant add-on version in `config.yaml`. Keep `APP_VERSION` unchanged and do not add a changelog entry.
- Publish user-requested stealth updates automatically after the required release checks pass. Ask for approval before publishing a major release.
- Do not skip release checks under the targeted-tests preference. A previous HA release failed because a changelog test assumed all releases contained every heading.
- No web research unless current external information or explicit user instructions require it.

## Task handovers

- Prefer a fresh task for each major feature; create it only when the user explicitly requests a new task.
- Supply a short handover: objective, branch and worktree, relevant files, completed changes, tests run, remaining work and release status.
- Carry these economy preferences into the handover.

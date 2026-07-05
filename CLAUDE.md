# Project AI Constitution (CLAUDE.md)

## 1. Token Optimization Principles (CRITICAL)
- Do not read entire files over 500 lines without explicit user permission.
- Always check the file tree or use `ripgrep(rg)` to find the destination before opening files.
- Exclude files unrelated to the specific task from the context.

## 2. Tech Stack Guidelines
Execute commands in the respective directories (`frontend` or `backend`).

### Backend (FastAPI)
- Path: `./backend`
- Test: `pytest`
- Lint/Format: `ruff check` or `black . --check`
- Rules: Use async routers (`async def`) and strict type hinting with Pydantic.

### Frontend (React)
- Path: `./frontend`
- Test: `npm test` or `yarn test`
- Lint/Format: `npm run lint`
- Rules: Maintain component separation, optimize state hooks, strict TypeScript typing.

## 3. CI/CD Automation Mode
- If `CI=true`, do not generate interactive questions. Execute the prompt and terminate.

## 4. Language Policy
- **CRITICAL:** Even though these instructions are in English, you MUST generate all human-readable outputs (such as commit messages, PR descriptions, and code review comments) in **Korean (한국어)**.
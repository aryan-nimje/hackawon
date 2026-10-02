# Security Notes

## Secrets

- All API keys and secrets live only in `backend/.env` (never commit this file).
- Copy `backend/.env.example` to `backend/.env` and fill in values locally.
- If any key was ever committed to version control, **rotate it immediately** and treat the old key as compromised.

## Configuration

- Environment variables are loaded exclusively via `backend/config.py` (pydantic-settings).
- No other file should read secrets directly from the environment.
- Keys must never reach the browser; the frontend only calls our backend API.

## Untrusted Input

- Citizen reports and news text are treated as untrusted data in LLM prompts (delimited, never as instructions).
- LLM outputs are validated against Pydantic schemas; invalid JSON is rejected or retried.
- The frontend strips/escapes HTML before rendering user-provided text (no `dangerouslySetInnerHTML`).

## Logging

- Never log API keys, auth headers, or full request bodies containing secrets.
- Health endpoints report mock mode status but never expose secret values.

## CORS & Rate Limiting

- CORS is restricted to origins listed in `ALLOWED_ORIGINS`.
- LLM-backed endpoints use rate limiting to protect API budget.

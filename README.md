# radioshake-ci

CI workflow shells for the RadioShake Android app.

**No application source code lives in this repository.**

The workflows here clone the application source from a private host at runtime,
run the required build or test steps, and publish results back. This separation
keeps the public CI configuration (runner setup, step ordering, emulator config)
visible while the proprietary source remains private.

## What lives here

- `.github/workflows/` — GitHub Actions workflow definitions
- `SECURITY.md` — threat model, token scopes, blast-radius analysis

## What does not live here

- Application source code
- Signing keys or credentials of any kind
- Build outputs or artefacts (these are pushed to the private package registry)

## Triggering

Workflows are triggered by schedule (nightly) or by explicit dispatch. There are
no `push` or `pull_request` triggers. See `SECURITY.md` for the full trusted-event
model.

# Security Model — radioshake-ci

This document is the standing threat-model record for this repository.
It is PUBLIC. It contains no secret values, no private hostnames, no IP addresses,
and no service-account names. Secret **names** and their scopes are listed so that
auditors can verify least-privilege without exposing credentials.

---

## Trusted-event model

Workflows in this repository are triggered only by:

- `schedule` — cron-based nightly runs
- `workflow_dispatch` — manual owner-initiated runs
- `repository_dispatch` — programmatic triggers sent by the private CI host

**Never** by `pull_request` or `push`. This eliminates the fork-PR attack surface
entirely: no external contributor can trigger a workflow run that touches secrets.

Only the repository owner can push to the default branch. This is enforced by two
complementary controls:

1. **GitHub permission model**: the repository has zero collaborators (verified via
   API — collaborator count = 1, which is the owner). No external account has write
   access.
2. **Branch protection on `main`**: force-pushes and branch deletion are blocked via
   classic branch protection (applied 2026-08-25). The owner can merge directly
   without a pull-request review; CI workflows remain unblocked.

---

## Token scopes (Forgejo — split least-privilege)

Three scoped tokens are used. Each is stored as a GitHub Actions secret and has
the narrowest scope that allows its function:

| Secret name | Forgejo scope | Purpose |
|---|---|---|
| `FORGEJO_CLONE_TOKEN` | `read:repository` | Clone the private application source at runtime; read-only, cannot push |
| `FORGEJO_PKG_TOKEN` | `write:package` | Push build artefacts to the private package registry |
| `FORGEJO_STATUS_TOKEN` | `write:repository` | POST commit-status updates back to the private repository |

No single token has more than one capability. A leaked clone token cannot write
packages or statuses, and vice versa.

---

## Blast radius of a compromised runner

If a runner job is fully compromised (e.g., malicious dependency in the build
toolchain), an attacker can:

- **Read** the application source (clone scope)
- **Write** packages to the private registry (package scope)
- **Write** commit statuses to the private repository (status scope)
- **POST** results to the results dashboard (ingest token scope)

An attacker **cannot**:

- Connect to the physical test device — it is on an isolated private network with
  no reachable tunnel from GitHub-hosted runners
- Access release signing keys — these are stored exclusively in the private CI host
  and never transferred to GitHub
- Access Play Console or any app-store credentials — same isolation as signing keys
- Push code to the private repository (clone token is read-only)
- Access any other project or service not explicitly listed above

---

## Masking policy

Every secret value that a workflow step derives at runtime (e.g., a token read
from a file, a value extracted from an API response) MUST be masked before it
can appear in log output:

```yaml
- name: Mask derived value
  run: |
    VALUE=$(some-command-that-produces-a-secret)
    echo "::add-mask::$VALUE"
```

Masking is a mitigation, not a guarantee — GitHub Actions masks known patterns in
log output but cannot prevent all exfiltration channels. Masking reduces accidental
exposure in public log viewers; it does not replace proper secret hygiene.

Audits should verify that every workflow step that handles a derived credential
applies `::add-mask::` before the value is used or logged.

---

## Audit checklist

When reviewing a workflow change, verify:

1. No new triggers beyond `schedule`, `workflow_dispatch`, `repository_dispatch`
2. No new secrets added beyond those documented in the table above
3. Every derived credential is masked with `::add-mask::`
4. No private hostnames, IPs, or service-account names appear in workflow YAML
5. Clone step uses `FORGEJO_CLONE_TOKEN`, not a broader token
6. Status-write step uses `FORGEJO_STATUS_TOKEN`, not the clone token

---

*Last reviewed: 2026-08-25*

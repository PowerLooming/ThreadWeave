# Security Policy

ThreadWeave runs inside the network it is capturing from, and it holds
material that was never meant to be public: decisions, client context,
personnel notes. A vulnerability here is not a website bug, it is a leak
of an organization's internal memory. We treat reports accordingly.

## Reporting a vulnerability

Email **hello@threadweave.net** with the subject line `security`, and include:

- what you found, and the impact you believe it has
- the version or commit you tested
- the smallest reproduction you can manage
- whether you have shared it anywhere else

Please do not open a public GitHub issue for a vulnerability. If you want
to encrypt the report, ask for a key in a first email without details.

We acknowledge within three working days, give you an assessment within
ten, and keep you informed until it is fixed. With your permission we
credit you in the release notes.

## Supported versions

The latest release is supported. Security fixes land in a patch release
on `master`; there are no maintained older branches.

| Version | Supported |
|---|---|
| latest release | yes |
| anything older | no, please upgrade |

## In scope

- authentication and API keys (`THREADWEAVE_REQUIRE_AUTH`, per-daemon keys)
- tenant isolation: one tenant reading another tenant's captures
- the confidentiality gates: a capture readable by someone without the
  clearance, wing, client assignment or named ACL it requires
- the PII gate storing or leaking content it should have stripped
- the opt-out registry not being enforced at ingest
- the audit log missing a read, a denial or a deletion
- connector credential handling (MSAL cache, per-daemon env files, Graph
  and Google tokens)
- anything that lets captured content leave the on-prem host

## Out of scope

- a deployment the operator exposed to the internet
- missing hardening on a host the operator controls (TLS, firewall, disk)
- content that was already public before it was captured
- denial of service from an authenticated key you were given
- reports produced only by a scanner, with no demonstrated impact

## What ThreadWeave does with your data

Capture without disclosure is surveillance, so the privacy layer is part
of the product, not an add-on: per-person opt-out enforced at ingest,
audited per-entry deletion, and a one-way flow where content moves from
the source system into the on-prem store and never back out. See
[docs/privacy.md](docs/privacy.md) and
[docs/ai-publication-boundary.md](docs/ai-publication-boundary.md).

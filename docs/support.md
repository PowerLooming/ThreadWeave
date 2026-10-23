# Support

ThreadWeave is maintained by one developer, Harald Daltveit, working on it
alongside other commitments. This page says how to reach him, what you can
expect back and what is not on offer, so an IT or procurement team can read the
answer instead of guessing.

## Where to go

| You have | Go to | What happens |
|---|---|---|
| A question, a how-to, a deployment doubt, an idea | [GitHub Discussions](https://github.com/PowerLooming/ThreadWeave/discussions) | A public answer that the next team can find. This is the right place for anything that is not a defect. |
| A defect you can reproduce | [Bug report](https://github.com/PowerLooming/ThreadWeave/issues/new?template=bug_report.yml) | Triaged in the open, fixed in a release, and the issue is closed against the release notes. |
| A feature request | [Feature request](https://github.com/PowerLooming/ThreadWeave/issues/new?template=feature_request.yml) | Read and prioritised. No response time is promised. |
| A security vulnerability | Email hello@threadweave.net, see [SECURITY.md](../SECURITY.md) | Private, acknowledged and fixed before anything is published. Never a public issue. |

If you are evaluating for a pilot and want the answers in writing before you
commit, open one Discussion thread with your questions rather than sending
several emails. The answers stay findable, and the next evaluator reads them
instead of asking again.

## Response expectations

These are targets in working days, Central European Time.

| Report | First response | Outcome |
|---|---|---|
| Security vulnerability | within 3 working days | an assessment within 10 working days, then a fix and credit if you want it |
| Defect with a reproduction | within 5 working days | a fix release, or an explanation of why it is not a ThreadWeave defect |
| Question in Discussions | within 5 working days | an answer, or a pointer to the doc that should have answered it |
| Feature request, anything else | no target | read and judged with the rest of the backlog |

The targets are what one maintainer can hold to, not a contract. There is no
24x7 rota, no telephone support, no on-call and no SLA behind this page. If your
process needs a contractual response time before a pilot, raise it in
Discussions and you will get a straight answer about what can be committed to.
The answer may be no, and it is better to hear that before the pilot than after.

There is no paid support plan published today. Anything beyond best-effort
support in the open is a commercial decision the maintainer makes case by case,
and it is not implied by this page.

## What to include in a report

- the version you are running: `GET /api/v1/health` returns `version`, and the
  release tag you installed is the other place to look
- what you ran, in the smallest form that shows the problem, with the exact
  error text
- the daemon log line from `~/.threadweave/logs/<name>.log`. Daemons log poll
  results, message and thread ids and errors, not message bodies, but read the
  line before you paste it
- whether `threadweave demo --serve` reproduces it. The demo runs on fictional
  data with its own database, so it separates a ThreadWeave defect from a tenant
  or permission problem faster than anything else

## What support does not cover

- Your host. TLS, firewall, disk, OS hardening, backups and restores belong to
  the operator. See [data-handling.md](data-handling.md) for what is stored and
  where.
- Your tenant. App registrations, admin consent, Conditional Access and
  licensing are your administrator's work. The docs name the permissions;
  [m365-connectors.md](m365-connectors.md) is the setup path.
- Cloud assistant integrations. There is no Microsoft 365 Copilot or Graph
  connector and no route from a cloud assistant into ThreadWeave. That is a
  decision rather than a backlog item, and the reasoning and the test that pins
  it are in [ai-publication-boundary.md](ai-publication-boundary.md). Requests
  for one will be answered with a no.
- Custom connectors for your systems. The interface is `BaseConnector` in
  `src/threadweave/connectors/base.py`, and the docstring at the top of that
  file says what a connector has to do. Build against it if you want to; the
  maintainer does not do bespoke development under this support route.

## Related

- [SECURITY.md](../SECURITY.md): how to report a vulnerability, and what is in
  and out of scope
- [data-handling.md](data-handling.md): what data flows where, subprocessors,
  retention and deletion
- [upgrade-policy.md](upgrade-policy.md): versions, upgrades, and what happens
  when a connector's vendor API breaks
- [privacy.md](privacy.md): the privacy model the product ships

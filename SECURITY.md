# Security Policy

## Supported versions

Only the revision identified by the currently deployed Private Alpha and its
immediate rollback revision receive security fixes. No long-term support
period is promised during the alpha.

## Reporting a vulnerability

Do not include birth data, credentials, server addresses, access tokens, or
other sensitive material in a public issue. Use GitHub private vulnerability
reporting:

<https://github.com/sa3952/Project-Armillary/security/advisories/new>

The operator aims to acknowledge a complete report within seven calendar days
during the Private Alpha. This is a best-effort target, not a response or fix
SLA. If the private channel is unavailable, send only a minimal contact request
to `privacy@projectarmillary.com`; do not put vulnerability details, birth data,
credentials, tokens, or server information in that email.

## Authorized testing boundary

Source review and testing against a system you own are welcome. This policy
does not authorize testing the hosted service, credential guessing, account or
invite enumeration, denial-of-service or load testing, social engineering,
access to another person's data, persistence, destructive actions, or testing
third-party infrastructure. Do not submit real birth data as a proof of
concept. Stop if testing could affect availability, confidentiality, another
person, or a system you do not own, and report the issue through the private
channel instead.

No bug bounty, safe-harbor promise, or authorization beyond the boundaries
above is offered by this document.

The Corresponding Source exporter generates one exact
`.well-known/security.txt` publication input with a release-derived expiry.
Its presence in the source repository does not prove that the production
domain serves the path, content type, redirect, canonical URL, or current
expiry; those remain live deployment checks.

## Security scope

Public source and tests cover the application and the provided deployment
templates. They do not prove the behavior of a particular hosting provider,
hypervisor, DNS resolver, certificate authority, operator workstation,
browser, RAM, crash dump, swap device, backup system, or support-access path.

No release is described as vulnerability-free, whole-repository security
approved, or suitable for professional advice.

# Security policy

Only revisions named by the active and immediate rollback receipts receive Private Alpha fixes；no
long-term support period is promised.

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/sa3952/Project-Armillary/security/advisories/new).
Never put birth data、credentials、host locators、tokens or another person's data in a public issue or
report. The operator aims to acknowledge a complete report within seven days, best-effort, not an SLA.
If the private channel is unavailable, send only a minimal contact request to
`privacy@projectarmillary.com`without vulnerability details.

## Authorized boundary

Source review and testing systems you own are welcome. This policy does not authorize hosted-service
testing、credential／invite enumeration、DoS／load testing、social engineering、persistence、destruction、
third-party infrastructure testing or access to another person's data. Stop before affecting
availability or confidentiality. No bug bounty or additional safe-harbor promise is offered.

The exporter produces`/.well-known/security.txt`with release-derived expiry；source presence does not
prove live path、content type、redirect、canonical URL or expiry. Public source and templates also do not
prove provider、DNS、CA、operator device、browser、RAM、swap、backup or support-access behavior. Nothing
here claims vulnerability absence、whole-repository approval or professional suitability.

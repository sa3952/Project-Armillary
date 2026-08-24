# Frontend

## Purpose

The browser user interface, client context, privacy lifecycle, and export serializers.

## Owner

Frontend engineering. Sebastian owns product wording and UX decisions. Paths named by an active
Claude handoff remain protected until exact revisions are delivered.

Current state：there is no active frontend freeze or Claude path lock。`docs/frontend-frozen/` is
historical evidence；future protection requires a new explicit Sebastian handoff naming exact paths。

## Allowed

HTML, CSS, JavaScript, icons, bounded frontend tests, accessible interaction, and serializers
that consume the normalized canonical result snapshot.

## Forbidden

Independent astronomy calculations, a second response assembly path, live-DOM export
reconstruction, silent acceptance of incompatible schema versions, remote telemetry without
privacy review, or edits to protected assets outside an exact handoff.

## Source of truth

The current API/output/export contracts define semantics. Files in this directory define browser
presentation and interaction; they do not override backend contracts or Sebastian decisions.

`zh-TW/tokens.css` is the runtime source of truth for current token **values**；
`docs/design/DESIGN_TOKENS.md` is a superseded proposal and must not overwrite it。Sebastian decisions
remain the authority for changing the aesthetic direction。

## Naming

Use lowercase hyphenated asset/module names and stable semantic vocabulary from the contracts.
Cache keys for changed assets are one dependency graph.

Browser runtime assets are grouped below a BCP 47 locale directory. The initial Traditional
Chinese site lives in `zh-TW/`; `/` redirects deterministically to `/zh-TW/` and the application
does not infer a locale from browser headers. API routes remain language-neutral under `/api/`.

## Retention

Track maintained assets and focused fixtures. Do not retain birth inputs, precise coordinates,
downloaded charts, browser state, build artifacts, or captured user data in the repository.

Armillary design-vote candidates left the runtime tree on 2026-08-06. Their byte-for-byte private
archive is routed from `docs/archive/README.md`; do not restore that archive under a locale directory
or admit it to a frontend release without a new explicit product selection and browser review.

## Tests and gates

Use serializer/unit tests plus real browser evidence for first interaction, non-default input,
network request, result content, console, error path, and mobile viewport when responsive
behavior changes.

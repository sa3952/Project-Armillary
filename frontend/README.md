# Frontend

## Purpose

The browser user interface, client context, privacy lifecycle, and export serializers.

## Owner

Frontend engineering. Sebastian owns product wording and UX decisions. Paths named by an active
Claude handoff remain protected until exact revisions are delivered.

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

## Naming

Use lowercase hyphenated asset/module names and stable semantic vocabulary from the contracts.
Cache keys for changed assets are one dependency graph.

## Retention

Track maintained assets and focused fixtures. Do not retain birth inputs, precise coordinates,
downloaded charts, browser state, build artifacts, or captured user data in the repository.

## Tests and gates

Use serializer/unit tests plus real browser evidence for first interaction, non-default input,
network request, result content, console, error path, and mobile viewport when responsive
behavior changes.

# Privacy Architecture

Birth date, time, timezone, and precise coordinates are treated as highly
sensitive input.

The application:

- has no account database;
- does not persist chart requests or responses;
- has one bundled immutable SQLite place catalog containing public product
  data; it is opened read-only and stores no query, chart, or account data;
- does not add analytics, telemetry, Sentry, APM, writable user-data database,
  or cache clients;
- does not intentionally process, hash, retain, or log client IP addresses;
- disables Uvicorn access logging;
- exposes no live OpenAPI or interactive API documentation in the hosted
  profile;
- emits only closed-vocabulary operational events from application-created
  values.

The intended production proxy:

- terminates HTTPS;
- authenticates each invite independently;
- disables access logging, including for `/api/chart`;
- does not forward `X-Forwarded-For`, `X-Real-IP`, or the original client IP
  to the application.

These application and template controls do not prove what a hosting provider,
hypervisor, network, certificate authority, operating system, browser, RAM,
swap, crash dump, or support-access system may observe. Those boundaries
require deployment-specific verification.

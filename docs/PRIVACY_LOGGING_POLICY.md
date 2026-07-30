# Privacy Logging Policy

Birth date, birth time, timezone, coordinates, request and response bodies,
headers, query parameters, IP addresses, User-Agent values, caller-provided
request identifiers, and exception text are forbidden from application
operational events.

Application events are reconstructed exclusively from a closed vocabulary of
application-created values. The emitter validates that vocabulary again before
writing an event. Uvicorn access logging is disabled in the supported hosted
command, and the intended NGINX configuration disables access logging rather
than attempting to redact sensitive entries afterward.

## ASGI lifecycle limitation

The boundary handles ordinary route errors, malformed input, rejection,
serialization/send errors, streaming failures, and background-task failures
without deliberately logging sensitive request material.

Python process-control exceptions must remain able to terminate or control the
process. On the **process-control exception re-raise path**, the application
**does not guarantee that security headers or the completion event are still
emitted**. This is an explicit
availability and observability limitation; it is not permission to serialize
the exception or request data.

Application controls do not prove the logging behavior of NGINX, systemd,
journald, the kernel, hosting provider, hypervisor, browser, certificate
authority, or support-access systems. Those layers require deployment-specific
configuration and canary verification.

# Privacy logging policy

Application events must not contain birth／location inputs、request／response bodies、headers、query、IP、
User-Agent、caller request IDs or exception text. Events are rebuilt from and revalidated against a
closed application vocabulary；supported Uvicorn／NGINX configurations disable access logging rather
than redact after collection.

Ordinary route、validation、send／stream and background-task failures are covered by bounded canaries.
Process-control exceptions must still terminate／control the process；that re-raise path does not guarantee
security headers or completion event, but never permits serializing request or exception content.
Provider、proxy、systemd、kernel and browser layers require their own configuration and canary evidence.

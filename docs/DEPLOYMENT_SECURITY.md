# Deployment Security

The intended boundary is:

```text
Internet -> NGINX OSS on host -> application bound to host-local/private path
```

Production acceptance requires:

- only TCP 80/443 exposed publicly;
- SSH restricted to the operator's current source address with provider
  console recovery;
- application port not publicly reachable;
- HTTPS with tested certificate issuance and renewal;
- independent invite credentials stored only as bcrypt `htpasswd` hashes;
- NGINX access log disabled and error log restricted;
- no client-IP forwarding to the application;
- host journald bounded to seven days and 50 MiB;
- container logs bounded to 5 MiB times two files;
- host and container swap disabled and core dumps disabled;
- no automatic snapshot, backup, or data volume;
- application network configured without Internet egress;
- current and previous images retained for rollback.

The source repository contains no real credential, server address, TLS key,
SSH key, provider account identifier, or firewall source address.

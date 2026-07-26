# Privacy Threat Model

## Protected data

- birth date and time;
- timezone;
- precise latitude, longitude, and altitude;
- chart request and response;
- invite credentials;
- client network metadata.

## Trust boundaries

```text
browser
  -> DNS and Internet transport
  -> HTTPS reverse proxy and invite authentication
  -> application container
  -> in-process Swiss Ephemeris calculation
  -> response returned to browser
```

The application has no intended outbound network path, database, cache, or
chart persistence. The browser retains form state for the active page and may
create user-requested downloads.

## Main threats and controls

| Threat | Current control | Residual boundary |
| --- | --- | --- |
| Request or response enters application logs | Closed-vocabulary logging and real-server canaries | Runtime, native library, and OS behavior still require review |
| Proxy records body or client metadata | Public NGINX template disables access log and IP forwarding | Actual host and provider configuration must be verified |
| Oversized or malformed input consumes resources | Content-type, body-size, schema, concurrency, and timeout boundaries | Deliberate distributed denial of service is not fully addressed |
| Data persists in application storage | No account DB, chart DB, cache, or persistence path | RAM, swap, crash dump, browser downloads, and provider systems |
| Dependency or ephemeris tampering | Hashed locks, source build, ephemeris hashes, SBOM and release receipt | Upstream compromise and build-host compromise remain possible |
| Service source differs from published source | Image is built from the exact public revision and records its digest | Operator must verify deployment receipt after each release |

## Non-goals

The Private Alpha does not claim anonymous network access, end-to-end
encryption beyond browser-to-proxy TLS, secure memory erasure, resistance to a
malicious hosting provider, or protection from a compromised browser or
operator device.

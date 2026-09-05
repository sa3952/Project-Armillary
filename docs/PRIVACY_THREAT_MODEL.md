# Privacy threat model

Protected data：birth date／time、timezone、precise location、chart request／response、invite credentials and
client network metadata.

```text
browser → Internet／DNS → HTTPS proxy／invite auth → application → Swiss Ephemeris → browser response
```

The application has no intended outbound path or user persistence；the browser may retain active-page
state and user-requested downloads.

| Threat | Control | Residual boundary |
|---|---|---|
| Sensitive application log | Closed vocabulary and real-server canary | Native／OS behavior |
| Proxy metadata capture | No access log or IP forwarding | Actual host／provider |
| Resource exhaustion | Type／size／schema／concurrency／timeout bounds | Distributed DoS |
| User-data persistence | No account／chart DB or cache | RAM、browser、provider systems |
| Supply-chain drift | Locks、source build、data hashes、SBOM、receipt | Upstream／builder compromise |
| Source mismatch | Image／frontend receipts bind public revisions | Per-release operator readback |

Non-goals：anonymous network access、secure RAM erasure、malicious-provider resistance、compromised browser／
operator protection or guarantees beyond browser-to-proxyTLS.

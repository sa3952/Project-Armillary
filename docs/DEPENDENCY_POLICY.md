# Dependency Policy

Production Python dependencies are explicitly selected, pinned, and hashed.
Build dependencies are separated from runtime dependencies. Swiss Ephemeris is
built from the exact `pyswisseph 2.10.3.2` source distribution rather than an
unverified runtime wheel.

Each release must:

1. verify every lock hash;
2. generate an SBOM;
3. run Python and image vulnerability scanners;
4. reject a Grype database older than 72 hours;
5. preserve raw matches for manual reachability triage without claiming that
   a scanner count proves exploitability;
6. publish exact third-party source hashes and licenses;
7. document any accepted unresolved vulnerability.

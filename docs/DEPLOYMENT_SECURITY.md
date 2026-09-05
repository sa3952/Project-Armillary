# Deployment security

Supported topology：Internet → hostNGINX → non-public application path. Release acceptance verifies
public80／443only；operator-address-restrictedSSH with console recovery；TLS issuance／renewal；independent
bcrypt invite credentials；disabled proxy access log／client-IP forwarding；bounded journald／container
logs；disabled swap／core dumps／automatic backup／data volume；no application Internet egress；and a
current／previous rollback pair.

No real credential、host、key、provider ID or firewall source belongs in this repository. Templates do not
prove a particular host；live state requires host receipts and canaries.

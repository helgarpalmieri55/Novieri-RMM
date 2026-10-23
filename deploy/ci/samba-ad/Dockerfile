# A throwaway Samba AD domain controller for the agent-winrm CI job
# (plan 2026-09-28-windows-push-winrm Task 9). It exists so the Kerberos push
# path is exercised against a real KDC: a TGT minted from a password, a
# service ticket for HTTP/pc1.corp.example.com, AES keys. Nothing here is a
# deployable artefact and every secret in provision.sh is a CI throwaway.
FROM debian:12-slim

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      samba samba-ad-dc samba-ad-provision samba-dsdb-modules samba-vfs-modules krb5-user winbind ldb-tools dnsutils \
 && rm -rf /var/lib/apt/lists/*

COPY provision.sh /usr/local/bin/provision.sh
RUN chmod 0755 /usr/local/bin/provision.sh

EXPOSE 88 88/udp 389 464
ENTRYPOINT ["/usr/local/bin/provision.sh"]

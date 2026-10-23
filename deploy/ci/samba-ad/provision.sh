#!/usr/bin/env bash
# Provision the CI domain controller, export the target's HTTP keytab to
# /shared, then serve KDC/LDAP in the foreground (plan Task 9, M4: no
# `A || B && C` precedence, each samba-tool line stands alone, the SPN is on
# the principal whose keys are exported).
#
# CI THROWAWAYS ONLY: this domain lives for one job and the passwords below
# protect nothing.
set -euo pipefail

REALM="CORP.EXAMPLE.COM"
DOMAIN="CORP"
ADMIN_PASS="Passw0rd!ci-throwaway"
SVC_PASS="Push0rd!ci-throwaway"
PC1_PASS="Pc1-Machine!ci-throwaway"
BASE_DN="DC=corp,DC=example,DC=com"
SAM=/var/lib/samba/private/sam.ldb

if [ ! -f "$SAM" ]; then
  rm -f /etc/samba/smb.conf
  samba-tool domain provision \
    --use-rfc2307 --realm="$REALM" --domain="$DOMAIN" \
    --server-role=dc --dns-backend=SAMBA_INTERNAL --adminpass="$ADMIN_PASS" \
    --option="dns forwarder = 127.0.0.1"
  cp /var/lib/samba/private/krb5.conf /etc/krb5.conf

  # The push service account: the identity the site-server agent uses.
  samba-tool user create svc-push "$SVC_PASS"
  samba-tool user setexpiry svc-push --noexpiry

  # A computer account for the target, and the HTTP SPN bound to IT: the
  # keytab exported below must belong to the principal that owns the SPN, so
  # the acceptor validates AP-REQs for HTTP/pc1.corp.example.com.
  samba-tool computer create PC1
  # `computer create` leaves the account DISABLED with no password, hence no
  # keys: exportkeytab would then write nothing at all (observed on Samba
  # 4.17: "Export one principal" and no file). Give it keys and enable it, as
  # a domain join does for a real Windows computer.
  samba-tool user setpassword 'PC1$' --newpassword="$PC1_PASS"
  samba-tool user enable 'PC1$'
  samba-tool spn add "HTTP/pc1.corp.example.com" 'PC1$'

  # AES only, as a real domain-joined Windows computer advertises (28 on
  # Windows; 24 = AES128|AES256 here). Without it the KDC would only issue
  # RC4 service tickets, which the agent never requests.
  printf 'dn: CN=PC1,CN=Computers,%s\nchangetype: modify\nreplace: msDS-SupportedEncryptionTypes\nmsDS-SupportedEncryptionTypes: 24\n' \
    "$BASE_DN" > /tmp/pc1-enctypes.ldif
  ldbmodify -H "$SAM" /tmp/pc1-enctypes.ldif

  mkdir -p /shared
  rm -f /shared/http.keytab
  samba-tool domain exportkeytab /shared/http.keytab --principal="HTTP/pc1.corp.example.com"
  # Fail here, loudly, rather than let the acceptor start with no keys.
  test -s /shared/http.keytab
  chmod 0644 /shared/http.keytab
  touch /shared/provisioned
fi

# Foreground; the DC serves the KDC on 88 (TCP/UDP) and LDAP.
exec samba -i --no-process-group

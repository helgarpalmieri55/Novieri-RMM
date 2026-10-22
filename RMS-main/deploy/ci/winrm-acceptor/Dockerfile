# The INDEPENDENT WinRM acceptor for the agent-winrm CI job (plan Task 9, §I5).
# MIT Kerberos (via Debian's python3-gssapi) + pyspnego: a separate
# implementation of SPNEGO, RFC 4121 wrap, MS-WSMV message encryption and RFC
# 5929 channel binding. If the agent's Go framing, RRC, key choice or CBT is
# wrong, decryption or acceptance fails HERE.
FROM debian:12-slim

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python3-dev python3-gssapi libkrb5-dev gcc libc6-dev krb5-config ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/acceptor/requirements.txt
RUN python3 -m venv --system-site-packages /opt/acceptor/venv \
 && /opt/acceptor/venv/bin/pip install --no-cache-dir -r /opt/acceptor/requirements.txt

COPY krb5.conf /etc/krb5.conf
COPY server.py /opt/acceptor/server.py

EXPOSE 5985 5986 5987
ENTRYPOINT ["/opt/acceptor/venv/bin/python", "/opt/acceptor/server.py"]

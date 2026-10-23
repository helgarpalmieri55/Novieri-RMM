# Host networking for a shared, multi-homed deploy

**What this is.** The host-level network configuration ITMS needs when it does
**not** own the machine or the default route. `README.md` covers the stack;
this covers the three things underneath it that no container can fix, because
each one is a property of the host kernel or the host firewall.

**Who needs it.** Anyone whose ITMS answers on an address that is not the
default route's, or whose host already publishes `:443` for something else. On
a dedicated single-NIC host, skip this file entirely: `ITMS_BIND_IP=0.0.0.0` and
the defaults are correct.

**Run `scripts/host-check.sh` first.** It changes nothing and prints every fact
this document asks you to know.

---

## 1. The bind address

`docker-compose.yml` publishes traefik on `${ITMS_BIND_IP}`. Two reasons it is
not simply `0.0.0.0`:

- **A wildcard bind fails outright on a shared host.** Linux refuses
  `0.0.0.0:443` when any specific address already holds port 443. So on a host
  where something else publishes `10.0.0.5:443`, leaving `ITMS_BIND_IP` at
  `0.0.0.0` does not share the port badly — traefik never starts, and it takes
  the stack's other ten services with it.
- **The converse also holds.** If the *other* platform binds `0.0.0.0:443`, it
  holds the port on every interface and no second address frees it. One of the
  two must move. `host-check.sh` §2 prints the bind address specifically
  because "is the port busy" is the wrong question.

Whatever address you choose must be the one your DNS name resolves to and the
one your `:443` certificate covers. `ITS_OIDC_ISSUER`, `ITS_GATEWAY_URL`,
`ITS_GATEWAY_RELAY_URL` and `ITS_S3_PUBLIC_ENDPOINT` all embed that hostname,
and **agents resolve the name** — a name pointing at the other address reaches
the other platform, with a valid certificate, and nothing looks broken.

---

## 2. The return path, when ITMS is not on the default route's NIC

### The failure

A host with two NICs and one default route sends every reply out the default
route, **whatever address the reply is sourced from**. So:

```
default via 10.0.0.1 dev eth0
10.1.1.0/29 dev eth1 src 10.1.1.4      # on-link only — no gateway
```

A client at `192.168.5.20` connects to `10.1.1.4:8443`. The request arrives on
`eth1`. The reply is sourced `10.1.1.4` — the socket's local address — but the
route lookup for `192.168.5.20` finds the default route, so it **leaves via
`eth0`**. A router doing anti-spoofing, or any stateful firewall that saw the
inbound flow on the other segment, drops it.

**This is the worst-shaped failure in the deployment.** It is not an outage.
The TCP handshake completes, TLS completes, and the connection then stalls or
dies intermittently depending on which devices are on which path. Agents look
flaky. The gateway gets blamed, and it will hold up under every test you run
against it, because from `eno1`-adjacent networks it genuinely works.

### The fix that does NOT work here, and why it is worth naming

The textbook answer to asymmetric replies on a multi-homed host is a second
routing table selected by **source address**:

```yaml
# DO NOT USE THIS. It is here as a counter-example; see below.
      routes:
        - to: default
          via: 172.22.104.25
          table: 100
      routing-policy:
        - from: 172.22.104.28
          table: 100
```

That is correct for a service **running on the host**. It is wrong for a
service in a **container behind a published port**, which is every service in
this stack, and it was applied to a live deploy before the flaw was noticed. Two
independent reasons:

**1. It cannot see the packets it is meant to redirect.** Docker publishes a port
by DNAT. An inbound connection to `172.22.104.28:443` has its destination
rewritten to the container's address in PREROUTING, and the reply is sent by the
*container*, not the host. At the moment the kernel makes the routing decision
for that reply, its source is still the container's `172.19.240.x`; the rewrite
back to `172.22.104.28` happens afterwards, in POSTROUTING. A rule matching
`from 172.22.104.28` is evaluated before that rewrite exists, so the reply
never matches it. The rule changes nothing about the problem.

**2. It breaks host-to-container traffic, which is how you test the deploy.**
The rule does match one thing: traffic the host itself originates from
`172.22.104.28`. `curl https://172.22.104.28/` on the host is DNATed to the
container address, the kernel re-runs the route lookup with the source already
fixed at `172.22.104.28`, and the rule sends it to table 100 — which holds only
a default route. The packet goes to the router, which knows nothing of the
container network, and is dropped. The symptom is a **connect timeout** to your
own listening port, with a healthy Traefik and empty logs, and it costs a long
time to attribute because every layer looks correct.

If a previous version of this document had you create
`/etc/netplan/60-itms-eno2-routing.yaml`, remove it:

```sh
sudo ip rule del from 172.22.104.28 lookup 100      # the live rule
sudo rm /etc/netplan/60-itms-eno2-routing.yaml      # and the persistent one
sudo netplan try
ip rule show                                        # no "from 172.22.104.28"
```

### The fix that addresses the actual path: connection marking

⚠ **UNVERIFIED.** The analysis above is verifiable by inspecting the packet
path; the remedy below has **not** been executed on a live host. Rehearse it
somewhere you can lose the connection before you rely on it.

Reply routing through DNAT has to key on something that exists at
forward-routing time, which the source address does not. The standard mechanism
is to mark the *connection* on the way in and restore the mark on the way out,
then route on the mark:

```sh
# Mark connections that arrive on eno2, and restore that mark on their replies.
sudo iptables -t mangle -A PREROUTING -i eno2 -m conntrack --ctstate NEW \
  -j CONNMARK --set-mark 100
sudo iptables -t mangle -A PREROUTING -j CONNMARK --restore-mark
sudo iptables -t mangle -A OUTPUT     -j CONNMARK --restore-mark

# Route marked traffic out eno2.
sudo ip route add default via 172.22.104.25 dev eno2 table 100
sudo ip rule add fwmark 100 lookup 100 priority 100
```

Because the mark travels with the conntrack entry rather than with an address,
it survives the DNAT and is present when the reply is routed. Nothing matches on
`172.22.104.28`, so host-originated traffic to container networks is untouched.

**Netplan cannot express the mangle rules**, so persistence needs
`iptables-persistent` (or an equivalent), and its ordering against Docker's own
startup must be checked after a reboot rather than assumed. The `ip route`/`ip
rule` halves *are* netplan-expressible, but splitting the mechanism across two
persistence systems is how half of it comes back after a reboot and the other
half does not; keep all four lines in one place.

Verify from a client on a network on-link for neither NIC, and verify the host
can still reach its own published port:

```sh
curl -k -m5 -o /dev/null -w '%{http_code}\n' https://itms.example.com/   # from a client
curl -k -m5 -o /dev/null -w '%{http_code}\n' https://172.22.104.28/      # on the host
```

The second command is not redundant. It is the one that catches a rule which has
captured more than it should — the failure this section exists to document.

### Pin cloud-init, or a reboot undoes this

On a cloud-init host, `/etc/netplan/50-cloud-init.yaml` is **regenerated**, and
a regeneration that drops `eno2`'s address takes ITMS's whole listening address
with it. If the interface was configured by hand after install, freeze it:

```sh
sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg <<'EOF'
network: {config: disabled}
EOF
```

A `50-cloud-init.yaml.before-fix` file lying around next to the live one is a
sign this has already happened once.

### Reverse-path filtering can drop the inbound half

Separately from the reply path, the kernel may refuse the *request*.
`rp_filter=1` (strict) drops a packet arriving on an interface that the kernel
would not route its source address back out of. The policy rule above is keyed
on `from <ITMS address>` — a source match — so it does not participate in the
reverse lookup for a client address, and strict mode drops the inbound packet
before anything sees it.

```sh
sysctl net.ipv4.conf.all.rp_filter net.ipv4.conf.eno2.rp_filter
```

`2` (loose — the Ubuntu default) is fine: it only requires the source be
routable via *some* interface. If either value is `1`, set loose mode for this
interface in `/etc/sysctl.d/60-itms-rp-filter.conf`:

```
net.ipv4.conf.eno2.rp_filter = 2
```

### Verify from the far side — and `ip route get` is not the verification

Host-local tests cannot see this class of bug: a packet that never leaves the
machine never meets the router that drops it. Only a round trip from a client on
a network on-link for neither NIC proves the path.

```sh
curl -k -m5 -o /dev/null -w '%{http_code}\n' https://itms.example.com/
```

**A previous version of this document offered
`ip route get <client> from 172.22.104.28` as proof, and it is not proof of
anything relevant here.** It reports the route the kernel would choose for a
packet the host originates from that address — which, for a containerised
service behind a published port, is not the packet that carries the reply. The
command answers a real question; it is simply not this question. Worse, it
answers *encouragingly* for a configuration that does not work, which is the
most expensive kind of check to have in a runbook.

If you want a host-local signal, the useful one is the negative:

```sh
ip route get 172.19.240.2 from 172.22.104.28     # must name a br-… bridge
```

If that resolves `via` your gateway instead of the container bridge, a policy
rule is capturing host-to-container traffic and your own published port will
time out from the host. That is a fault you can detect without leaving the
machine, which is exactly why it is worth checking here.

---

## 3. ufw does not filter Docker-published ports

Docker publishes a port by DNAT in `nat`/PREROUTING; the packet is then handled
as **forwarded** traffic through `DOCKER-USER` and `FORWARD`. It never traverses
`INPUT`, which is where ufw's rules live. So:

- **A ufw `deny` does not protect a published container port.** If ufw shows
  `443/tcp ALLOW IN 10.1.0.0/24` and a container publishes 443, that port is in
  practice reachable from everywhere the network can reach it. This applies to
  every container on the host, including ones that are not ITMS.
- **Conversely, you do not need a ufw rule for agents to reach `:8443`.** It
  will already work. An operator who adds one and sees traffic flow will
  reasonably but wrongly conclude ufw is the control.

Confirm it on your own host rather than believing this document — from a client
on a network ufw does *not* allow the port from:

```sh
curl -k -m5 -o /dev/null -w '%{http_code}\n' https://<published-ip>/
```

An answer means ufw is not filtering it. A hang or refusal means something on
this host is, and you should find out what before assuming the rest.

### What the agent ports actually need to be open to

Both, from every managed endpoint:

| Port | Used for | Authenticated by |
|---|---|---|
| `8443` | agent gateway (job dispatch) and the remote-control relay | mutual TLS, client cert issued at enrollment |
| `443` | object storage under presigned grants — **backup chunks and `app.deploy` artifacts** | the presigned grant itself: prefix-scoped, time-bounded |

`443` is the one that gets missed. It is easy to assume agents only talk to the
gateway; `deploy/README.md`'s failure table (row 12) is a whole afternoon lost
to exactly that assumption.

### If you do want to restrict them

Put the rules in `DOCKER-USER`, which is traversed before Docker's own accept
rules and is not flushed when Docker restarts. Allow first, then deny, matching
the interface ITMS is published on:

```sh
# Allow the endpoint networks to the gateway.
sudo iptables -I DOCKER-USER -i eno2 -p tcp --dport 8443 -s 172.22.109.0/24 -j RETURN
sudo iptables -I DOCKER-USER -i eno2 -p tcp --dport 8443 -s 172.22.110.0/24 -j RETURN
# Then drop the rest. -A, so it lands after the RETURNs above.
sudo iptables -A DOCKER-USER -i eno2 -p tcp --dport 8443 -j DROP
```

Three things that make this less simple than it looks, all of which have bitten
people:

1. **`--dport` here is the container port**, because DNAT has already rewritten
   the destination. It works above only because the published and container
   ports are both 8443. If you remap, match the container port.
2. **These rules do not survive a reboot.** `iptables-persistent` restores them,
   but ordering against Docker's own startup matters; verify after a reboot
   rather than assuming.
3. **Test both directions** — from an allowed source *and* a denied one. A rule
   set that denies everything, including your agents, looks identical to a
   correct one until an endpoint tries to check in.

Both ports authenticate independently of any of this: `8443` requires a client
certificate this platform issued, `443` requires a valid presigned grant. So
reachability is the priority and restriction is hardening, not the other way
round.

---

## 4. The clock

`timedatectl` must report `System clock synchronized: yes` **before** first
boot, not after. Certificate validity windows and OIDC token expiry are
absolute times: a host that is minutes off issues agent certificates that fail
validation elsewhere, and rejects tokens that are perfectly valid. "NTP service:
active" is not the same as synchronized — active means the daemon is running,
which it will happily do having never reached a source.

```sh
timedatectl show-timesync --all | head -20     # is there a reachable server?
systemctl status systemd-timesyncd
```

The usual cause on a segmented network is outbound udp/123 having nowhere to
go. Point it at an internal source in
`/etc/systemd/timesyncd.conf` rather than leaving it reaching for the internet.

---

## 5. LVM free space, if this host is also a backup target

The Linux snapshot provider runs `lvcreate -s -l 10%ORIGIN`, so it needs free
extents equal to **10% of the origin logical volume** — not 10% of the source
root, and not 10% of the data being backed up. A 232G root LV needs ~23G free
in the volume group, whether you back up 200G or one directory.

```sh
sudo vgs      # VFree is the number that matters
sudo lvs
```

`VFree` of 0 — common when an installer hands the whole disk to one LV — means
`backup.run` refuses at the Snapshot stage no matter how small a source root you
choose. Extend the VG, or run the controlled test on a host that has room.

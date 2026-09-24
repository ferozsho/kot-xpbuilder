# Troubleshooting

Symptom-first notes for failures that took real time to diagnose. Each entry
states what the error looks like, what actually caused it, and how to confirm
the fix. Procedures belong in [configuration.md](configuration.md) and
[deployment.md](deployment.md); this file holds the symptoms and the traps.

## "Packet sequence number wrong - got 45 expected 0"

### Symptom

Adding a MySQL database in Superset fails with a *Database Creation Error*:

```text
(builtins.NoneType) None
[SQL: (pymysql.err.InternalError) Packet sequence number wrong - got 45 expected 0
(Background on this error at: https://sqlalche.me/e/14/2j85)]
```

The toolbar reads *"We are unable to connect to your database"*, and the failed
attempt is never written to the `dbs` table (only the previously registered
connections remain).

### Cause

pymysql is talking to an **SSH server**, not to MySQL.

MySQL's server greeting is a packet whose fourth byte is the sequence number and
must be `0`. An SSH server greets with the identification string
`SSH-2.0-OpenSSH_8.0`, whose fourth byte is `-`; `-` is `45`. pymysql reads that
byte as the sequence number and rejects the packet, so `45` is literally the
fourth character of the SSH banner.

So the connection's host or port points at an SSH endpoint. This normally
happens when an access ticket lists SSH connection details (host, non-standard
port, key) next to the database details, and the SSH port is used where the
database port belongs. It is **not** a credential, firewall, or TLS problem.

### Confirm what is really listening

Probe the endpoint from the host that has network access to it, and read the
greeting: a MySQL 8 server answers with its version string, an SSH server with
`SSH-2.0-…`.

```bash
timeout 6 bash -c 'exec 3<>/dev/tcp/<host>/<port>; head -c 60 <&3' \
    | tr -c '[:print:]\n' '.'
```

Then authenticate for real, using the stack's own client:

```bash
docker exec <instance>_mariadb mariadb --connect-timeout=10 \
    -h <host> -P 3306 -u <user> -p -e "select version(), current_user()" <database>
```

A successful `current_user()` also proves which address the server sees the
connection coming from, which is what a `'user'@'host'` grant has to match.

### Access tickets that mix SSH and database details

Tickets that hand over an SSH account *and* a database account usually mean
"SSH in, then use the database on its loopback interface". Two traps:

- **The SSH key must be usable at all.** An SSH client silently ignores a
  private key whose file mode is group- or world-readable
  (`Load key "...": bad permissions`) and then fails with
  `Permission denied (publickey)` — which looks exactly like a key that is not
  authorised. Keys must be mode `0600`. Confirm one loads before blaming the
  server:

  ```bash
  ssh-keygen -y -f <key file> >/dev/null && echo "key loads"
  ```

- **The database password is not the SSH password.** Hosting panels often issue
  both from one ticket; they are unrelated credentials. Verify each separately
  rather than assuming the database password unlocks SSH.

Before building a tunnel, always test the database port directly: many hosting
setups expose MySQL on the public interface even when the ticket only documents
SSH access, and a tunnel adds a supervised process that can fail independently
of Superset.

### Worked example: kot5 and the client Moodle UAT database

The ticket listed SSH access (`103.241.136.96:45837`, "key file provided
previously") plus a database (`localhost:3306`, database `kefuat`, user
`kefuattempuser`). The Superset connection was pointed at the SSH port and hit
the sequence-number error above.

Findings, all verified from the VM:

| Check | Result |
| --- | --- |
| `103.241.136.96:3306` from the VM | open, MySQL 8.0.26 (Moodle `mdl_*` schema) |
| Provided database credentials | authenticate; `current_user()` = `kefuattempuser@<VM address>` |
| pymysql inside the Superset container | connects |
| SSH key on the VM | not authorised for that host — and the mode was `0644`, so ssh was ignoring it entirely |
| Database password over SSH | rejected; it is not the SSH password |

The SSH access was unnecessary: the database was reachable directly. The
connection is registered as `kefuat` in the kot5 stack with the URI
`mysql+pymysql://kefuattempuser:<password>@103.241.136.96:3306/kefuat`, which
serves 8,541 users and 295 courses. The password exists only inside that
connection (encrypted with `SUPERSET_SECRET_KEY`) and in the client's ticket —
never in this repository.

Two follow-ups came out of it: the key on the VM was set to mode `0600`, and the
missing "provided previously" key file was never received, so SSH access to that
host still depends on the client re-issuing it.

### Related

- [configuration.md](configuration.md) — attaching an external database, and
  when an SSH tunnel is genuinely required (bind it to the Docker bridge
  gateway, never `127.0.0.1`).
- [../reports/README.md](../reports/README.md) — the user report built on top of
  a connected Moodle database.

## "Clear all" leaves the dashboard filters filled in

### Symptom

On a dashboard with native filters, picking a value in a filter and clicking
**Clear all** *without* pressing **Apply** first does nothing: the control keeps
showing the picked value and both buttons stay enabled. Picking a value, pressing
**Apply**, and then clicking **Clear all** works, which is why it looks
intermittent.

### Cause

Upstream Superset 6.1.0 stages `undefined` as the cleared value for every filter
type except ranges (`FilterBar/index.tsx`, `handleClearAll`). The select filter
plugin treats an `undefined` value as *not yet initialized*, so it never resets
its own state and the widget keeps rendering the previous selection.

### Fix

`docker/patches/0001-fix-native-filter-clear-all.patch` backports the upstream
fix: the cleared value is staged as an explicit `null` (still `[null, null]` for
ranges), the staged mask is written even when the filter has no entry yet, and
the clear-all trigger fires for every in-scope filter. The patch is applied to
the vendored Superset tree during the image build, so it reaches a stack only
after that stack's image is rebuilt:

```bash
bin/xpbuilder --env-file <env> build      # or: docker compose build superset
```

`superset-frontend/src/.../FilterBar/FilterBar.test.tsx` is updated in the same
patch, so `npm test` in `superset-frontend` stays green.


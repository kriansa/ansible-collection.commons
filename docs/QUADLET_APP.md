# Quadlet App Module

## Overview

The `quadlet_app` module provides an opinionated, structured approach to deploying containerized applications using Podman Quadlet. Instead of manually managing individual quadlet files scattered across the filesystem, it enforces a consistent directory layout that groups all application components together.

Define your entire application—containers, volumes, networks, configuration, and initialization scripts—in a single directory. Point the module at it, and it handles Jinja2 templating, resource name prefixing, path preprocessing, file deployment, and systemd service management automatically.

## Directory Structure

```
myapp/
├── quadlets/              # Required: Quadlet unit files
│   ├── main.container     # Required: Primary container
│   ├── db.container       # Optional: Additional containers
│   ├── data.volume        # Optional: Volumes
│   └── app.network        # Optional: Networks
├── units/                 # Optional: Plain systemd units
│   └── backup.timer       # Schedules quadlets/backup.container
├── init/                  # Optional: One-time initialization files
│   ├── main/              # Matches quadlet stem (main.container)
│   │   └── init.sql
│   └── db/
│       └── schema.sql
└── config/                # Optional: Runtime configuration files
    ├── main/
    │   └── nginx.conf
    └── db/
        └── postgresql.conf
```

### Directory Rules

- **quadlets/**: Contains `.container`, `.volume`, `.network`, `.pod`, or `.kube` files
  - `main.container` is the only requirement
  - Files are deployed to `/etc/containers/systemd/`
  - Resource names are automatically prefixed with app name when deployed
  - Contents in these files are automatically templated like `ansible.commons.template`
  - A `.timer` here is an error — it is not a quadlet type and belongs in `units/`

- **units/**: Plain systemd units, currently `.timer` files — see [Timers](#timers)
  - Files are deployed to `/etc/systemd/system/`
  - Unit references are prefixed and templated exactly as in `quadlets/`
  - Anything with an unrecognized extension is rejected rather than ignored

- **init/**: One-time initialization files mounted into containers
  - Subdirectories use the quadlet stem without suffix (e.g., `main/` for `main.container`)
  - Files are deployed to `/srv/{appname}/init/{container}/`
  - Mounted read-only by default

- **config/**: Runtime configuration files mounted into containers
  - Subdirectories use the quadlet stem without suffix (e.g., `main/` for `main.container`)
  - Files are deployed to `/srv/{appname}/config/{container}/`
  - Can be mounted read-write or read-only

## Preprocessing Rules

The module automatically preprocesses quadlet files:

### Resource Prefixing
References to other resources and systemd units are automatically prefixed with the app name. This applies to: `Network`, `Pod`, `Volume`, `Wants`, `Requires`, `Requisite`, `BindsTo`, `PartOf`, `Upholds`, `Conflicts`, `Before`, and `After` directives.

```ini
# Before preprocessing
[Unit]
After=db.service

[Container]
Network=app.network
Volume=data.volume

# After preprocessing (app name: myapp)
[Unit]
After=myapp--db.service

[Container]
Network=myapp--app.network
Volume=myapp--data.volume:rw,Z,idmap
```

*For named `.volume` references with no options specified, `rw,Z,idmap` is applied automatically —
see [Volume Ownership](#volume-ownership).*

### Path Replacement
Volume paths using `%init%` or `%config%` placeholders are replaced with deployment paths.

```ini
# Before preprocessing
[Container]
Volume=%init%:/docker-entrypoint-initdb.d
Volume=%config%/nginx.conf:/etc/nginx/nginx.conf

# After preprocessing (app name: myapp, container: main)
[Container]
Volume=/srv/myapp/init/main:/docker-entrypoint-initdb.d:ro,Z,idmap
Volume=/srv/myapp/config/main/nginx.conf:/etc/nginx/nginx.conf:ro,Z,idmap
```

*If no mount options are specified, `ro,Z,idmap` is applied automatically.* `idmap` rather than `U`
means the deployed files keep their `root:root` ownership on the host — the kernel translates ids at
the mount instead — so the tree Ansible writes is never chowned out from under it.

### User Namespaces

Every `.container` gets `UserNS=auto:size=65536` unless it declares a `UserNS=` of its own.

Two things are happening here. First, **`auto` is the default**: rootful podman otherwise puts a
container in the host's namespace, so its root *is* the host's root. Defaulting to `auto` gives each
container its own unprivileged range instead. Volumes are unaffected either way, because they are
mounted idmapped — their on-disk ids are the container's own regardless.

Second, **the size is pinned**. Podman derives an unqualified `auto` range from the image's highest
uid, which is routinely far smaller than the uids the application actually writes with — as little
as 1024. Any file owned above the range is unmappable: it reads back as `65534` and is not writable,
*even by container root*.

```ini
# Before preprocessing
[Container]
Image=nginx

# After preprocessing
[Container]
Image=nginx
UserNS=auto:size=65536
```

An explicit `size=` or `uidmapping=`, and every non-`auto` mode, are left untouched.

> **Containers that need host identity must say so with `UserNS=host`.** Anything reading the host
> journal or another process's `/proc` entries only works while its ids *are* the host's ids. In a
> namespace those files read as `65534` and are unreadable — but world-readable `/proc` entries keep
> working, so such a container **half-works**: the unit stays green and some metrics keep flowing
> while log collection is silently dead. A host-observability agent is the usual case.

Don't be alarmed by a fragmented `/proc/PID/uid_map`. Podman reuses a container's previously
allocated range and stitches further segments on to reach the requested size, so a map can end up
looking like `0 235165 1024 / 1024 628380 64512` rather than one clean span. All of `0..65535` is
still mapped, just not contiguously on the host side — and since volumes are idmapped, which segment
an id lands in has no bearing on whether the container can read its own files.

Changing the size of an existing container's range makes podman re-chown its whole image layer
(`storage-chown-by-maps`). That is one-time and cached per mapping, but it scales with the number of
files in the image: on a Raspberry-Pi-class board it took ~15 minutes and needed `TimeoutStartSec`
raised well above the default to survive.

### Container Storage Ordering

Every `.container` gets `RequiresMountsFor=/var/lib/containers/storage` in its `[Unit]` section, so
a container never starts before the filesystem holding its image is mounted. It is skipped when the
file already lists that path, and other `RequiresMountsFor` entries are left alone — the directive
is additive in systemd, so an app can still declare its own for a different mount.

### Automatic Resource Naming
Resource name directives are automatically injected if not provided, ensuring predictable naming instead of Podman's default `systemd-` prefix.

```ini
# Before preprocessing (main.container, app name: myapp)
[Container]
Image=nginx:latest

# After preprocessing
[Container]
ContainerName=myapp--main
Image=nginx:latest
```

Applies to: `ContainerName` (`.container`), `PodName` (`.pod`), `VolumeName` (`.volume`), `NetworkName` (`.network`). Explicit names in your files are always respected.

## Volume Ownership

Every named volume has an owner, expressed in the container's own terms. It defaults to `0:0` —
correct for any image that runs as root — and an image that drops privileges internally declares the
uid it drops to with `X-Owner`:

```ini
# quadlets/data.volume
[Volume]
Driver=stratis
X-Owner=10000:10000
```

`X-Owner` is consumed by this module and never reaches the deployed file.

> **Getting it wrong is an outage, not data loss.** If the declared owner is not the uid the app
> actually runs as, the app cannot read its own volume and fails to start. Correct the value and
> redeploy: the marker path changes, so the chown re-runs. The one case to think about first is a
> volume whose files are *not* uniformly owned — flattening it to a single owner loses that
> distinction, and no redeploy brings it back.

### Why it exists

`UserNS=auto` gives each container a unique host uid range, but *which* range is decided at start
time — podman packs them in start order and sizes each one from the image's highest uid. Bumping an
unrelated container's image is enough to move a neighbour's base. A volume outlives the container,
so its files end up owned by a host uid nobody maps any more.

Podman's own answer is the `U` mount option, which recursively chowns the whole volume to the
container's mapped root on *every* mount. That works only for images that run as root — an image
that starts as root and drops privileges internally gets its data directory handed back to root on
every start, and never regains it.

Instead, volumes are mounted `rw,Z,idmap`. The kernel translates ids at the mount, so a file stored
on disk as uid 10000 *is* uid 10000 inside the container, whatever range podman picked. The mapping
is then free to drift and nothing notices, and the ownership is set once rather than on every mount.

### What gets generated

For each volume, the module deploys `{app}--{volume}-volume-init.container`: a `Type=oneshot` unit
that chowns the volume once, ordered before every container that mounts it.

It is guarded by a marker under `/var/lib/quadlet-app/` whose filename carries the ids, e.g.
`myapp--data.10000_10000.done`. Once written, `ConditionPathExists=!` skips the unit on every
subsequent boot. Changing `X-Owner` changes the marker path, so the chown runs again — exactly once
— and the module removes the volume's previous marker. Listing that directory therefore shows what
each volume is currently owned by.

The unit deliberately runs **without a user namespace**. A process inside one cannot chown a file
whose owner is unmapped, not even as container root, so a namespaced helper could never migrate a
volume off its old ownership. It runs as real host root for a fraction of a second, writes the
container-relative ids straight to disk, and is skipped thereafter.

### Constraints

- **Ids must be below 65536** — the size of the user namespace the consuming container gets. A
  higher id would be written to disk and then be invisible to the app that needs it.
- **On-disk ids are container-relative.** A volume carrying ids from the old scheme (say `310000`)
  is not readable by the container until the bootstrap unit has run.
- Failure signature when ownership is wrong: files show as `65534`, and writes fail with
  `Operation not permitted` even as root inside the container.

## Timers

A container that runs on a schedule is two files: the job itself in `quadlets/`, and a `.timer` in
`units/` that fires it.

```ini
# quadlets/backup.container
[Container]
Image=docker.io/library/postgres:16
Volume=dumps.volume:/dumps
Exec=/usr/bin/pg_dumpall -f /dumps/dump.sql

[Service]
Type=oneshot
```

```ini
# units/backup.timer
[Timer]
OnCalendar=daily
Persistent=true
```

That is the whole app-facing surface. `Unit=` and `[Install]` are filled in for you, and the
deployed file reads:

```ini
[Timer]
OnCalendar=daily
Persistent=true
Unit=myapp--backup.service

[Install]
WantedBy=timers.target
```

### Why it lives in its own directory

Quadlet has no `.timer` type — it knows `.container`, `.volume`, `.network`, `.pod`, `.kube`,
`.build`, `.image` and `.artifact`, and generates a `.service` from each. A timer is an ordinary
systemd unit, so `quadlets/` is the wrong place for it twice over: podman's generator would ignore
it, and `/etc/containers/systemd/` is a directory systemd itself never reads. It is deployed to
`/etc/systemd/system/` instead, which is why it gets a directory of its own.

### Naming what it triggers

Writing `Unit=backup.container` is the natural thing to do and names a unit that does not exist —
the generator emits `backup.service` from that file. The suffix is therefore rewritten to whatever
podman will actually produce, and then prefixed like every other reference:

| Written | Deployed |
|---------|----------|
| `Unit=backup.container` | `Unit=myapp--backup.service` |
| `Unit=backup.kube` | `Unit=myapp--backup.service` |
| `Unit=backup.pod` | `Unit=myapp--backup-pod.service` |
| `Unit=data.volume` | `Unit=myapp--data-volume.service` |
| `Unit=app.network` | `Unit=myapp--app-network.service` |
| `Unit=backup.service` | `Unit=myapp--backup.service` |

Omit `Unit=` entirely and `backup.timer` resolves against `backup.*` in `quadlets/`, picking the
right suffix for whatever type it finds.

**Targets are app-scoped.** Prefixing is unconditional, so a timer cannot drive a unit outside its
own app — the same limitation `After=` and `Wants=` already have. Nothing checks that the target
exists, so a typo deploys cleanly and then fails when the timer first fires.

### Why these are enabled when nothing else is

The module runs `systemctl enable` for timers and for nothing else. Quadlet-generated services are
*transient*: they cannot be enabled at all, and the generator compensates by applying their
`[Install]` section itself every time it runs. A `.timer` has neither property — it is a real file
on disk and nothing will arm it unless something enables it.

Enabling alone does not start a unit, and `timers.target` was reached long before Ansible connects,
so the timer is enabled **and** started. Enabling on its own would leave it idle until the next
reboot.

This happens on every `state`, including `installed`. A timer whose own file changed is also
restarted, so an edited `OnCalendar=` takes effect immediately rather than at the next boot.
An unrelated edit elsewhere in the app does not touch it.

### Constraints

- **`RemainAfterExit=yes` silently breaks the schedule.** It is the usual advice for `Type=oneshot`,
  but it leaves the job in a started state and every trigger after the first is a no-op. Leave it
  off on a timer-driven container.
- **The triggered container must not declare `[Install] WantedBy=multi-user.target`**, or it runs at
  boot as well as on schedule.
- **Restarting resets relative timers.** `OnBootSec=` and `OnUnitActiveSec=` count from the restart,
  so editing such a timer restarts its countdown. `OnCalendar=` is unaffected.
- Nothing removes a timer. Deleting it from `units/` leaves the deployed unit enabled and running;
  `systemctl disable --now` it and delete the file by hand.

```bash
systemctl list-timers 'myapp--*'            # when does it next fire
journalctl -u myapp--backup.service         # what happened when it did
```

## Usage

### Basic Deployment

```yaml
- name: Deploy nginx application
  kriansa.commons.quadlet_app:
    src: apps/nginx
    state: started
```

### With Variables

```yaml
- name: Deploy database
  kriansa.commons.quadlet_app:
    src: apps/postgres
    name: prod-db
    state: started
  vars:
    db_version: "16"
    db_password: "{{ vault_db_password }}"
```

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `src` | Yes | - | Path to application directory |
| `name` | No | basename of src | Application name (lowercase, letters/numbers/hyphens/underscores) |
| `state` | No | `installed` | Deployment state: `installed`, `started`, `restarted` |
| `force` | No | `false` | Force redeployment even if files haven't changed |
| `systemctl_timeout` | No | `120` | Timeout in seconds for systemctl commands (start, stop, restart, daemon-reload) |

### States

- **installed**: Deploy files and reload systemd, but don't manage service state
- **started**: Deploy files and ensure the main service is running. If files changed and the service is already running, restarts all app-prefixed dependencies and the main service
- **restarted**: Deploy files and restart all app-prefixed dependencies and the main service

These describe the main service only. Timers are enabled and started under every state — see
[Timers](#timers).

## Variable Templating

All files are automatically templated with Jinja2 using Ansible variables:

```ini
[Container]
Image=docker.io/library/postgres:{{ db_version }}
Environment=POSTGRES_PASSWORD={{ db_password }}
PublishPort={{ db_port }}:5432
```

Task-level variables are supported:

```yaml
- name: Deploy app
  kriansa.commons.quadlet_app:
    src: apps/myapp
  vars:
    app_version: "2.0"
    api_key: "{{ vault_api_key }}"
```

### Application Name Variable

The variable `quadlet_app_name` is automatically available in all templates containing the application name. Use it with string concatenation for environment variables, labels, or any non-directive values that need the app prefix.

**Available Variable:**
- `quadlet_app_name`: The application name (automatically available in all templates)

**Usage:**
```jinja
{{ quadlet_app_name }}--db                    # Returns: "myapp--db"
{{ quadlet_app_name }}--redis                 # Returns: "myapp--redis"
{{ quadlet_app_name }}--backend.service       # Returns: "myapp--backend.service"
```

**Examples:**
```ini
[Container]
# Reference another container by name in environment variable
Environment=DATABASE_HOST={{ quadlet_app_name }}--db
Environment=CACHE_HOST={{ quadlet_app_name }}--redis

# Use in labels
Label=app.name={{ quadlet_app_name }}
Label=app.depends-on={{ quadlet_app_name }}--api

# Useful for dynamic service references
Environment=UPSTREAM_SERVICE={{ quadlet_app_name }}--backend.service

# Container hostnames
Environment=DB_HOST={{ quadlet_app_name }}--db
```

**Note:** For directive-based references (Network=, Pod=, Volume=, After=, etc.), automatic prefixing is already applied during preprocessing. Use this variable only when you need the prefix in values that aren't automatically processed.

### Using Ansible Features

Since templates use Ansible's native templating engine, you have access to all standard Ansible features:

**Filters:**
```ini
[Container]
# Use default filter
Image={{ container_image | default('nginx:latest') }}

# JSON formatting
Environment=CONFIG={{ app_config | to_json }}

# String manipulation
Environment=HOSTNAME={{ inventory_hostname | upper }}
```

**Lookup Plugins:**
```ini
[Container]
# Read from files
Environment=SECRET={{ lookup('file', '/secrets/api-key') }}

# Environment variables
Environment=HOME={{ lookup('env', 'HOME') }}

# Ansible vault
Environment=DB_PASSWORD={{ lookup('file', 'vault/db-password.txt') }}
```

**Tests:**
```jinja
[Container]
{% if inventory_hostname is match('prod-.*') %}
Environment=LOG_LEVEL=warning
{% else %}
Environment=LOG_LEVEL=debug
{% endif %}
```

## Examples

### Simple Web Server

```
nginx/
├── quadlets/
│   └── main.container
└── config/
    └── main/
        └── default.conf
```

```ini
# quadlets/main.container
[Unit]
Description=Nginx Web Server

[Container]
Image=docker.io/library/nginx:{{ nginx_version | default('latest') }}
PublishPort={{ nginx_port | default(8080) }}:80
Volume=%config%:/etc/nginx/conf.d

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

### Database with Initialization

```
postgres/
├── quadlets/
│   ├── main.container
│   └── data.volume
└── init/
    └── main/
        ├── 01-schema.sql
        └── 02-data.sql
```

```ini
# quadlets/main.container
[Container]
Image=docker.io/library/postgres:16
Volume=data.volume:/var/lib/postgresql/data
Volume=%init%:/docker-entrypoint-initdb.d
Environment=POSTGRES_PASSWORD={{ db_password }}

[Service]
Restart=always
```

### Multi-Container Application

```
webapp/
├── quadlets/
│   ├── main.container      # Web frontend
│   ├── api.container       # API backend
│   ├── app.network         # Shared network
│   └── cache.volume        # Shared cache
└── config/
    ├── main/
    │   └── nginx.conf
    └── api/
        └── app.yaml
```

```ini
# quadlets/main.container
[Container]
Image=docker.io/library/nginx:alpine
Network=app.network
Volume=%config%:/etc/nginx/conf.d
PublishPort=8080:80

# quadlets/api.container
[Container]
Image=myregistry/api:{{ api_version }}
Network=app.network
Volume=cache.volume:/cache
Volume=%config%:/app/config

# quadlets/app.network
[Network]
Driver=bridge
```

### Scheduled Job

```
postgres/
├── quadlets/
│   ├── main.container      # The database
│   ├── backup.container    # The job, run on demand only
│   ├── app.network
│   ├── data.volume
│   └── dumps.volume
└── units/
    └── backup.timer        # When to run it
```

```ini
# quadlets/backup.container
# No [Install] section: this container runs when the timer says so, not at boot.
# No RemainAfterExit either, or only the first trigger would do anything.
[Unit]
Requires=main.service
After=main.service

[Container]
Image=docker.io/library/postgres:16
Network=app.network
Volume=dumps.volume:/dumps
Environment=PGHOST={{ quadlet_app_name }}--main
Exec=/usr/bin/pg_dumpall -f /dumps/dump.sql

[Service]
Type=oneshot

# units/backup.timer
[Timer]
OnCalendar={{ backup_schedule | default('daily') }}
Persistent=true
```

`Requires=` and `Network=` are prefixed automatically; `PGHOST` is a plain value, so it needs
`quadlet_app_name` spelled out. See
[Application Name Variable](#application-name-variable).

## Idempotency

The module uses SHA256 checksums to detect file changes. Deployment only occurs when:
- Files have changed (content differs)
- Service state doesn't match desired state
- `force: true` is set

## Service Management

Services are named: `{appname}--{filename}.service`

Examples:
- `myapp--main.service` (from `main.container`)
- `myapp--db.service` (from `db.container`)
- `myapp--backend-pod.service` (from `backend.pod` — podman appends `-pod`)

Units from `units/` keep their own suffix: `backup.timer` becomes `myapp--backup.timer`.

### Dependency Handling

When using `state=restarted`, or `state=started` with file changes while the main service is running,
the module:
1. Discovers all app-prefixed service dependencies of the main service — everything it `Requires=`
   or `Wants=`, directly or not
2. Restarts them, the main service, and any volume bootstrap unit whose `X-Owner` changed, in a
   single `systemctl restart`

One call means one systemd transaction, so every unit stops and starts exactly once, in dependency
order, with a bootstrap unit finishing before the containers that mount its volume start. This
ensures that when you update your application, all related services (databases, caches, workers,
etc.) are restarted in the correct order.

Whether the main service "is running" is read once, before the module restarts anything. Asking
later is unreliable: restarting a volume's bootstrap unit makes systemd restart every container that
requires it, main included, so a later check can find main mid-restart — `activating`, and longest
for a unit that waits on a healthcheck (`Notify=healthy`). The module would then only start main
instead of restarting the app, and a changed secondary container that main merely `Wants=` would
keep running its old image. For the same reason, bootstrap units whose `X-Owner` did not change are
never restarted.

Manage services manually:
```bash
systemctl status myapp--main.service
systemctl restart myapp--main.service
journalctl -u myapp--main.service -f
```

## Deployed Files

### Quadlet Files
- Location: `/etc/containers/systemd/`
- Naming: `{appname}--{filename}`
- Examples: `myapp--main.container`, `myapp--data.volume`

Generated units are deployed alongside the ones you wrote:
- `{appname}--{volume}-volume-init.container`, one per volume declaring `X-Owner`

### Systemd Units
- Location: `/etc/systemd/system/`
- Naming: `{appname}--{filename}`
- Example: `myapp--backup.timer`

### Configuration Files
- Init: `/srv/{appname}/init/{container}/`
- Config: `/srv/{appname}/config/{container}/`

### State
- Volume ownership markers: `/var/lib/quadlet-app/{appname}--{volume}.{uid}_{gid}.done`

## Troubleshooting

### Files show as `65534`, or writes fail as container root

The volume's on-disk ids are outside the container's user namespace. Either the volume has not been
migrated yet (its bootstrap unit never ran, or was skipped by a stale marker), or `X-Owner` names an
id the container cannot map.

```bash
systemctl status myapp--data-volume-init.service   # skipped, or failed?
ls /var/lib/quadlet-app/                           # which ownership was last applied
```

Deleting the volume's marker and restarting the unit re-runs the chown.

### `mount point /mnt/<volume> exists and is not empty`

Not caused by this module, but worth knowing on hosts using a volume plugin such as stratis. If
podman's internal mount count for a volume goes stale — a reboot with the count still set, for
instance — a container can be handed the *empty directory underneath* the mountpoint rather than the
volume. Anything it writes lands on the root filesystem, and the plugin then refuses to mount the
volume at all because the mountpoint is no longer empty.

Repair is to remove the strays, after checking whether anything of value was written there while
podman believed the volume was mounted:

```bash
ls -la /mnt/<volume>          # this is the root filesystem, not the volume
rm -rf /mnt/<volume>/*
```

An empty `/mnt/<volume>` while the volume is unmounted is the healthy state.

## Application Naming

Application names must:
- Start with a lowercase letter (a-z)
- End with a lowercase letter or number (a-z, 0-9)
- Contain only lowercase letters, numbers, hyphens, and underscores
- Be automatically converted to lowercase if not already

Valid: `myapp`, `web-server`, `api_v2`, `db1`
Invalid: `MyApp`, `1app`, `-app`, `app-`

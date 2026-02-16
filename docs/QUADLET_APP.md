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
├── init.d/                # Optional: One-time initialization files
│   ├── main.container/    # Matches quadlet filename
│   │   └── init.sql
│   └── db.container/
│       └── schema.sql
└── config.d/              # Optional: Runtime configuration files
    ├── main.container/
    │   └── nginx.conf
    └── db.container/
        └── postgresql.conf
```

### Directory Rules

- **quadlets/**: Contains `.container`, `.volume`, `.network`, `.pod`, or `.kube` files
  - `main.container` is the only requirement
  - Files are deployed to `/etc/containers/systemd/`
  - Resource names are automatically prefixed with app name when deployed
  - Contents in these files are automatically templated like `ansible.commons.template`

- **init.d/**: One-time initialization files mounted into containers
  - Subdirectories must match a quadlet filename (e.g., `main.container`)
  - Files are deployed to `/srv/{appname}/init/{container}/`
  - Mounted read-only by default

- **config.d/**: Runtime configuration files mounted into containers
  - Subdirectories must match a quadlet filename
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
Volume=myapp--data.volume
```

### Path Replacement
Volume paths using `init.d` or `config.d` are replaced with deployment paths.

```ini
# Before preprocessing
[Container]
Volume=init.d:/docker-entrypoint-initdb.d:ro,z
Volume=config.d/nginx.conf:/etc/nginx/nginx.conf:ro,z

# After preprocessing (app name: myapp)
[Container]
Volume=/srv/myapp/init/main:/docker-entrypoint-initdb.d:ro,z
Volume=/srv/myapp/config/main/nginx.conf:/etc/nginx/nginx.conf:ro,z
```

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
└── config.d/
    └── main.container/
        └── default.conf
```

```ini
# quadlets/main.container
[Unit]
Description=Nginx Web Server

[Container]
Image=docker.io/library/nginx:{{ nginx_version | default('latest') }}
PublishPort={{ nginx_port | default(8080) }}:80
Volume=config.d:/etc/nginx/conf.d:ro,z

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
└── init.d/
    └── main.container/
        ├── 01-schema.sql
        └── 02-data.sql
```

```ini
# quadlets/main.container
[Container]
Image=docker.io/library/postgres:16
Volume=data.volume:/var/lib/postgresql/data:z
Volume=init.d:/docker-entrypoint-initdb.d:ro,z
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
└── config.d/
    ├── main.container/
    │   └── nginx.conf
    └── api.container/
        └── app.yaml
```

```ini
# quadlets/main.container
[Container]
Image=docker.io/library/nginx:alpine
Network=app.network
Volume=config.d:/etc/nginx/conf.d:ro,z
PublishPort=8080:80

# quadlets/api.container
[Container]
Image=myregistry/api:{{ api_version }}
Network=app.network
Volume=cache.volume:/cache:z
Volume=config.d:/app/config:ro,z

# quadlets/app.network
[Network]
Driver=bridge
```

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

### Dependency Handling

When using `state=restarted` or when `state=started` with file changes, the module automatically:
1. Discovers all app-prefixed service dependencies of the main service
2. Restarts all dependencies first
3. Then restarts the main service

This ensures that when you update your application, all related services (databases, caches, workers, etc.) are restarted in the correct order.

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

### Configuration Files
- Init: `/srv/{appname}/init/{container}/`
- Config: `/srv/{appname}/config/{container}/`

## Application Naming

Application names must:
- Start with a lowercase letter (a-z)
- End with a lowercase letter or number (a-z, 0-9)
- Contain only lowercase letters, numbers, hyphens, and underscores
- Be automatically converted to lowercase if not already

Valid: `myapp`, `web-server`, `api_v2`, `db1`
Invalid: `MyApp`, `1app`, `-app`, `app-`

Caddy Ingress
=============

A provider for [`kriansa.quadlet`](https://github.com/kriansa/ansible-collection.quadlet)'s
`quadlet_stack` role. An app describes the vhost it needs in its own `application.yml`, and this
role writes it into the Caddy app's drop-in directory and reloads Caddy. Nothing outside the
claiming app is edited to publish it.

## Claiming a route

```yaml
# quadlet-apps/hass/application.yml
version: "v1"

resource_claims:
  kriansa.commons.caddy_ingress:
    - domain: {{ ('hass.' ~ public_dns_search) | to_json }}
      upstream: {{ 'main:8123' | to_json }}
```

| Field | | |
|---|---|---|
| `domain` | required | the vhost to serve |
| `upstream` | one of these three | `<quadlet>:<port>`, resolved to `<app>--<quadlet>:<port>` |
| `address` | | a literal `host:port`, for an upstream with no container name |
| `raw` | | the inside of the site block, written as given — see below |
| `instance` | optional, `main` | which Caddy serves it |
| `scheme` | optional, `http` | `https` for a TLS upstream |
| `tls_insecure` | optional | skip certificate verification on the upstream hop |
| `preserve_host` | optional | send the original `Host` upstream |
| `flush_interval` | optional | response buffering; `-1` disables it, for SSE and streaming |

`address` exists for a container that joins no network of its own — one running `Network=host` has
no name to resolve, and is reached at the host's address instead.

> **`instance` defaults to `main`, which is the unsafe direction when a host runs more than one
> Caddy.** Two instances usually exist because they publish on different networks with different
> trust levels, so a claim that omits `instance` on a service meant for the restricted one
> publishes it to the other. It fails open: a working site, and no error anywhere.

### When a route does not fit: `raw`

Some vhosts are more than one reverse proxy: they route by path across several upstreams, answer
with a literal, or require auth. `raw` takes the inside of the site block and writes it as given:

```yaml
resource_claims:
  kriansa.commons.caddy_ingress:
    - domain: {{ ('metrics.' ~ public_dns_search) | to_json }}
      raw: |
        basic_auth {
          agent {{ lookup('passwordstore', 'Metrics/Write subkey=hash') }}
        }
        handle /api/v1/write {
          reverse_proxy http://{{ quadlet_app_name }}--main:9090
        }
        handle {
          respond 404
        }
        log default
```

The role writes the `https://<domain> {` line around it and adds nothing else, so:

- **There is no `import common`.** Put it in the block if the vhost wants the shared snippet.
- **Upstreams are not resolved.** Name the container in full. `{{ quadlet_app_name }}` is the
  app's own name, so `{{ quadlet_app_name }}--main` stays right if the app is renamed.
- **`scheme`, `tls_insecure`, `preserve_host` and `flush_interval` are refused** next to it rather
  than silently ignored. Write the equivalent directives into the block.

The manifest is rendered before it is read, so the block can use variables and lookups like the
rest of it. Caddy's own placeholders, such as `{host}` and `{uri}`, use single braces and pass
through untouched.

Every claim on an instance is written to the same file and validated together, so a block Caddy
rejects rolls back the routes of every app on that instance, not only its own. Prefer the fields
above whenever a route fits them; `raw` is for the ones that do not.

## What the app has to provide

The Caddy app needs a drop-in directory and an import for it:

```ini
# quadlets/main.container
Volume=%config%/conf.d:/etc/caddy/conf.d
```

```caddyfile
# config/main/Caddyfile
(common) {
	root * /srv
	log default
	encode zstd gzip
	# … whatever every vhost shares
}

import /etc/caddy/conf.d/*.caddy
```

Vhosts claimed with `upstream` or `address` `import common`, so that snippet has to exist. Ship a placeholder file in
`config/<instance>/conf.d/` so the directory is there before Caddy first starts — an empty
directory matched by the glob is valid, a missing mount source is not.

## What it does

Once per Caddy instance, which it discovers from the app's `config_dir` rather than configuration:

1. Skips the instance if it is not deployed on this host. Every provider is handed every claim,
   including from apps whose consumer lives elsewhere.
2. Writes `conf.d/declared.caddy`, whole, with `setype: container_file_t`.
3. If that changed, runs `caddy validate` **inside the running container**, which is what makes
   `{env.*}` in the base Caddyfile resolve, and validates the merged result rather than the
   fragment.
4. Puts the previous file back and fails if Caddy rejects it.
5. Reloads — not restarts. A reload keeps every request already in flight, long-polls and event
   streams included, and Caddy never stops listening; a restart drops them all and leaves a gap
   where nothing answers.

A reload is not invisible, though: it closes every WebSocket Caddy is proxying. Frontends that keep
one open for live updates see it drop and reconnect, so publishing one app's route still briefly
interrupts the live views of every other app on that instance. Caddy's `stream_close_delay` can
postpone that close, but only postpone it, and generated routes do not set it.

The reload posts the new configuration to Caddy's admin API, so the Caddy app has to have one. A
base Caddyfile with `admin off` cannot be reloaded at all, and the failure is quiet: the route is
written, nothing applies it, and the site only appears the next time something restarts Caddy.

### Why the rollback matters

A configuration Caddy rejects is inert while Caddy keeps running on the last good one. It stops
being inert the moment anything restarts the container — a reboot, an unrelated deploy — and then
it takes down *every* site on the host, not only the one that is wrong. The failure would surface
far from the change that caused it.

### Removing a route

The file is rewritten whole, so an app that stops claiming a route loses it on the next run. No
state is kept between runs and nothing has to be cleaned up by hand.

### Coexisting with hand-written vhosts

A route that no app on the host owns, such as one proxying a different machine, has nowhere to be
claimed from and stays in the Caddy app's own `Caddyfile`. The two files are merged by the import
glob, so neither needs to know about the other, and an app can be moved over whenever it suits.

The one rule: **a domain cannot be claimed while it is still written by hand.** Caddy refuses the
whole configuration with `ambiguous site definition`, which step 3 catches and step 4 undoes — but
the deploy fails until the two halves travel in the same commit.

The rollback protects the routes that were already working, not the one being moved. By the time
this role runs, the Caddy app has been deployed and restarted, so the hand-written vhosts the same
commit deleted are already gone from the host. A run that fails here therefore leaves every domain
whose hand-written vhost was removed — including ones unrelated to the claim that collided —
serving nothing until a run succeeds. Deleting a vhost and claiming it is one change, and getting
half of it right is an outage.

## Variables

| | default | |
|---|---|---|
| `caddy_ingress_app` | `ingress` | the app running Caddy |
| `caddy_ingress_filename` | `declared.caddy` | the file written into each `conf.d/` |

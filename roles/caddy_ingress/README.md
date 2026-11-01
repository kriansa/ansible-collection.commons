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
| `headers_up` | optional | extra headers to set on the request to the upstream |
| `forward_auth` | optional | put an authentication service in front of the vhost — see below |

`headers_up` is a mapping of header name to value, and exists for an upstream that wants to be
told something about its proxy — most often a shared secret proving the request came through it
and not straight off the container network:

```yaml
    - domain: {{ ('nvr.' ~ public_dns_search) | to_json }}
      upstream: {{ 'main:8971' | to_json }}
      headers_up:
        X-Proxy-Secret: {{ lookup('passwordstore', 'NVR/Proxy-Secret') | to_json }}
```

Values are written into the generated file as quoted strings, so spaces and quotes are safe —
but Caddy expands its own `{...}` placeholders inside a quoted string, so a value containing
braces is read as one rather than sent literally. Keep secrets used here alphanumeric.

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
- **`scheme`, `tls_insecure`, `preserve_host`, `flush_interval` and `headers_up` are refused** next
  to it rather than silently ignored. Write the equivalent directives into the block.
- **`forward_auth` is allowed** next to it, and is written above the block. A raw block containing
  a `forward_auth` of its own is refused, because both gates would be installed and the inner one
  would decide.

The manifest is rendered before it is read, so the block can use variables and lookups like the
rest of it. Caddy's own placeholders, such as `{host}` and `{uri}`, use single braces and pass
through untouched.

Every claim on an instance is written to the same file and validated together, so a block Caddy
rejects rolls back the routes of every app on that instance, not only its own. Prefer the fields
above whenever a route fits them; `raw` is for the ones that do not.

## Putting a login in front: `forward_auth`

For an app that cannot authenticate anyone itself. Caddy asks an authentication service about
**every** request and serves the app only if it approves; the service never carries the app's
traffic, and the app needs to know nothing about it.

```yaml
resource_claims:
  kriansa.commons.caddy_ingress:
    - domain: {{ ('wiki.' ~ public_dns_search) | to_json }}
      upstream: {{ 'main:8080' | to_json }}
      forward_auth:
        profile: sso
        require_roles: [role:wiki-admin]
```

| Field | | |
|---|---|---|
| `profile` | required | which entry of `caddy_ingress_auth` to ask |
| `require_roles` | optional | deny with 403 unless the caller carries one of these |

Leave `require_roles` out when the app does its own authorization from the headers the gate sets;
then the gate only answers *who* the caller is. Include it for an app with no concept of a user,
where Caddy has to be the one that refuses.

Values are matched against the header **verbatim**, so they carry whatever prefix the service uses
— oauth2-proxy reports a Keycloak realm role as `role:<name>`. The field accepts anything that
appears in that header, so despite the name it will match a bare group just as well.

### Defining a profile

Profiles live in the playbook, not in this role, keyed by the name claims use. This one is an
**example**, written for oauth2-proxy; the role ships none, because every value here describes a
service it knows nothing about.

```yaml
caddy_ingress_auth:
  sso:
    endpoint: "http://oauth2-proxy--main:4180"   # where Caddy asks
    uri: /oauth2/auth                            # the path that answers 2xx or 401
    redirect: "https://oauth-id.example/oauth2/start"   # where an unauthenticated caller is sent
    host: oauth-id.example                       # optional: Host sent to the endpoint
    copy_headers:                                # auth response header -> upstream request header
      X-Auth-Request-Preferred-Username: X-Forwarded-User
      X-Auth-Request-Groups: X-Forwarded-Groups
    strip_headers: [X-Forwarded-User, X-Forwarded-Groups]   # cleared before the gate runs
    role_header: X-Forwarded-Groups              # what require_roles matches against
    user_header: X-Forwarded-User                # optional: named in the 403 body
    logout: "https://oauth-id.example/oauth2/sign_out"      # optional: offered in the 403 body
```

`endpoint`, `uri`, `redirect` and `copy_headers` are required, and `role_header` as soon as any
claim sets `require_roles`. A claim naming a profile that is missing or incomplete fails the run
before anything is written.

**`host` is needed whenever the endpoint is a different vhost.** Caddy sends the original `Host`
to the auth endpoint by default, so without it the request arrives asking for the app's hostname
and the authentication service's own ingress will not match it.

### What gets written, and why it looks like that

```caddy
route {
	request_header -X-Forwarded-User
	request_header -X-Forwarded-Groups

	forward_auth http://oauth2-proxy--main:4180 {
		uri /oauth2/auth
		copy_headers X-Auth-Request-Groups>X-Forwarded-Groups X-Auth-Request-Preferred-Username>X-Forwarded-User

		@unauth status 4xx
		handle_response @unauth {
			redir * https://oauth-id.example/oauth2/start?rd={scheme}://{host}{http.request.uri.path_escaped}?{http.request.uri.query_escaped}
		}
	}

	@denied not header_regexp X-Forwarded-Groups (^|,)(role:wiki\-admin)(,|$)
	respond @denied "Not authorised. Signed in as {http.request.header.X-Forwarded-User}." 403
}
```

Three parts of that are easy to get wrong by hand, which is most of the reason to generate it:

- **The strip has to be inside `route`.** Caddy's default directive order runs `forward_auth`
  *before* `request_header`, so an unwrapped `request_header -` deletes the identity the gate just
  established instead of the caller's forged copy of it. The gate still passes and every request
  reaches the app anonymous.
- **`rd=` is escaped in two parts.** Written as one `{uri}`, the first `&` of a query string ends
  the parameter and the rest is read as parameters of the login URL, so a caller signing in from a
  deep link silently comes back to a truncated address.
- **The role matcher is anchored on the separators.** A substring match would let `wiki-admin-ro`
  satisfy a requirement for `wiki-admin`. An absent header matches nothing, so the gate denies a
  caller the service told it nothing about rather than letting them through.

Only the headers named in `copy_headers` are authoritative: Caddy sets each from the auth response
and **removes** it when the response omits it, so neither can be forged. Every other header the
caller sends arrives at the app untouched — that is what `strip_headers` is for, and it should
name every identity header the app behind it is willing to believe.

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
| `caddy_ingress_auth` | `{}` | authentication profiles a claim's `forward_auth` can name |

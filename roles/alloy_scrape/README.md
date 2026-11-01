Alloy Scrape
============

A provider for [`kriansa.quadlet`](https://github.com/kriansa/ansible-collection.quadlet)'s
`quadlet_stack` role. An app describes the metrics endpoint it serves in its own `application.yml`,
and this role writes a scrape for it into the Alloy app's config directory and reloads Alloy.
Nothing outside the claiming app is edited to start collecting it.

## Claiming a scrape

```yaml
# quadlet-apps/keycloak/application.yml
version: "v1"

resource_claims:
  kriansa.commons.alloy_scrape:
    - job: "keycloak"
      target: "main:9000"
```

| Field | | |
|---|---|---|
| `job` | required | the `job` label |
| `target` | required | `<quadlet>:<port>`, resolved to `<app>--<quadlet>:<port>` |
| `metrics_path` | optional, `/metrics` | |
| `scheme` | optional, `http` | `https` for a TLS endpoint |
| `scrape_interval` | optional, `60s` | |
| `bearer_token` | optional | sent as `Authorization: Bearer` - see [Credentials](#credentials) |
| `relabel` | optional | rules applied to the scraped series, in order - see below |
| `enabled` | optional, `true` | `false` leaves the scrape out - see [A scrape that waits for its credential](#a-scrape-that-waits-for-its-credential) |

Every scrape is labelled `instance` with the host's name, the same value the host's own metrics
carry, so an app's series can be lined up with its host's.

A field the role does not know fails the run. A misspelled `metric_path` would otherwise scrape
`/metrics` instead, and the only sign would be a job reading `up=0`.

### Rewriting what is scraped: `relabel`

Each rule is a mapping of the arguments of a `rule` block in Alloy's
[`prometheus.relabel`](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.relabel/),
written out as given:

```yaml
resource_claims:
  kriansa.commons.alloy_scrape:
    - job: "frigate"
      target: "main:5000"
      metrics_path: "/api/metrics"
      relabel:
        - action: "drop"
          source_labels: ["__name__", "type"]
          separator: ";"
          regex: "frigate_(cpu|mem)_usage_percent;Other"
        - action: "labeldrop"
          regex: "cmdline|pid"
```

The rules run in the order they are listed, which matters as soon as one depends on another - here,
dropping `pid` would make two `type="Other"` series identical, so those are discarded first. Alloy
checks the rules themselves when it validates; this role only checks that each one is a mapping.

### Credentials

A token in a claim is a secret in a manifest, and the manifest is not a private place:
`quadlet_stack` shows every claim when Ansible runs with `-v`, and hands every claim to every
provider. Look it up rather than writing it out, so at least the repository never holds it:

```yaml
      bearer_token: "{{ lookup('passwordstore', 'Services/Home-Assistant/Prometheus-Token') }}"
```

The role writes it into the generated file as it is, keeps that file readable by root only, and does
not show it in a diff or in its own checks, which read a copy of the claims with the token replaced.
One thing it cannot hide: when Alloy rejects the file, its error quotes the lines around the fault,
and a fault next to the token's line would show the token.

### A scrape that waits for its credential

Some tokens can only be made once the app is running - a long-lived token created in its UI, say.
A plain lookup cannot wait for that: it fails the run while the entry is missing, and because every
app is read whichever one is deployed, it fails *every* run on the host, including the one that
would deploy the app the token has to come from.

So let the lookup come back empty, and switch the claim off for as long as it does:

```yaml
      bearer_token: "{{ lookup('passwordstore', 'Services/Home-Assistant/Prometheus-Token missing=empty') }}"
      enabled: "{{ lookup('passwordstore', 'Services/Home-Assistant/Prometheus-Token missing=empty') | default('', true) | length > 0 }}"
```

`missing=empty` gives `None` rather than an empty string, which is what the `default('', true)` is
for: `length` fails on `None`.

A claim that is switched off goes through every check the others do, so a mistake in it shows up
before it is switched on, and is only left out of the file. Every run lists it, since a scrape that
is missing raises no error anywhere else - but it is a line in the output, not a failure, so it is
only noticed by someone reading it. Once the token exists, the next run on the host writes the
scrape in and reloads Alloy.

A claim that is switched on with an empty token is refused: it would only ever get `401`.

## What the Alloy app has to provide

The Alloy container has to mount its whole config directory, and run Alloy on the directory, which
then loads every `*.alloy` file in it as one configuration:

```ini
# quadlets/main.container
Volume=%config%:/etc/alloy
Exec=run /etc/alloy --storage.path=/var/lib/alloy
```

Mounting `config.alloy` on its own is not enough. Ansible writes a file by replacing it, and a
container that mounted the old file keeps seeing the old one - and a file it was never given, it
does not see at all. The role checks for this and fails rather than reload an Alloy that would not
see any scrape.

Its base configuration has to define the receiver every declared scrape forwards to, under this
exact name:

```alloy
// config/main/config.alloy
prometheus.remote_write "default" {
  endpoint {
    url = "https://metrics.example/api/v1/write"
  }
}
```

And it has to keep Alloy's HTTP server, which the reload goes through, at its default address.

Apps that claim a scrape join the Alloy app's network, `o11y--main` by default, the way they join
the ingress network to be published. The role refuses a claim from an app that does not: Alloy
could not reach it, and the scrape would only ever read `up=0`.

## What it does

1. Checks every claim: the fields, that `target` names one of the app's own containers, and that the
   app joins Alloy's network.
2. Skips the host if Alloy is not deployed on it. Every provider is handed every claim, including
   from apps whose consumer lives elsewhere.
3. Writes `declared.alloy` into Alloy's config directory, whole, readable by root only, with
   `setype: container_file_t`. Components are named `<app>_<job>`, so two apps can each have a job
   called `api`.
4. If that changed, runs `alloy validate` on the directory **inside the running container**, so the
   declared scrapes are checked together with the base configuration they forward to.
5. Reloads Alloy, and puts the previous file back and fails if Alloy rejects it.

### Why it reloads, and how

A restart would make Alloy flush its send queue on the way down and lose the scrapes in progress; a
reload swaps the scrape components and leaves the rest running.

The reload is a `POST` to Alloy's `/-/reload` rather than a `SIGHUP`, because Alloy answers a
configuration it cannot load with an error, which fails the run. A `SIGHUP` would fail silently -
and that is worse than it looks. Alloy keeps running the last configuration it accepted, so nothing
seems wrong, but the file it rejected is still on disk, and the next time anything restarts Alloy it
refuses to start at all. On the host that also means no logs: the journal stops being shipped.

Alloy's image ships no `curl`, and its HTTP server listens on its own container's loopback, so the
request is made from the host, entering the container's network namespace with `nsenter`. The host
needs `curl` and `nsenter`, which a minimal EL install has.

### Removing a scrape

The file is rewritten whole, so an app that stops claiming a scrape loses it on the next run. No
state is kept between runs and nothing has to be cleaned up by hand.

### Coexisting with hand-written scrapes

A scrape that no app on the host owns, such as one of a different machine, has nowhere to be claimed
from and stays in the Alloy app's own `config.alloy`. The two files load as one configuration, so a
component name used in both is refused - but declared ones all start with the claiming app's name.

The one rule when moving a scrape from `config.alloy` to a claim: **remove it from `config.alloy`
in the same change.** The two would not collide, so nothing fails, but the target would be scraped
twice.

## Variables

| | default | |
|---|---|---|
| `alloy_scrape_app` | `o11y` | the app running Alloy |
| `alloy_scrape_quadlet` | `main` | which of its quadlets is Alloy |
| `alloy_scrape_network` | `<app>--main` | the network a claiming app has to join |
| `alloy_scrape_instance` | `{{ hostname }}` | the `instance` label on every scrape |
| `alloy_scrape_config_path` | `/etc/alloy` | where the container mounts its config directory |
| `alloy_scrape_reload_url` | `http://127.0.0.1:12345/-/reload` | Alloy's reload endpoint, inside its container |
| `alloy_scrape_filename` | `declared.alloy` | the file written into the config directory |

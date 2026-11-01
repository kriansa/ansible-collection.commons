OPNsense DNS Alias
=================

A provider for [`kriansa.quadlet`](https://github.com/kriansa/ansible-collection.quadlet)'s
`quadlet_stack` role. An app names the DNS names it needs in its own `application.yml`, and this
role publishes each as an Unbound **host override alias** on an OPNsense router - an extra name for
the override of the host running the app - then restarts Unbound so it answers for them. Nothing
outside the claiming app is edited to publish a name.

## Claiming a name

```yaml
# quadlet-apps/git-server/application.yml
version: "v1"

resource_claims:
  kriansa.commons.opnsense_dns_alias:
    - name: "git.{{ public_dns_search }}"
      parent: "atlas.{{ public_dns_search }}"
```

| Field | | |
|---|---|---|
| `name` | required | the full name to publish |
| `parent` | required | the full name of the host override it becomes an alias of |

The name answers with whatever address its parent has, so `parent` picks which of the host's
addresses clients reach: on a host with one override per network, the one for the network the
service listens on. Any other key is refused, so a typo fails the run instead of being ignored.

A name does not have to be under the parent's domain: `chat.lan.cx` can hang from
`gaia.svc.lan.cx`.

## Ownership

Every alias this role writes has its description set to a marker:

```
Managed by Ansible: <app> on <owner>
```

`owner` is the first label of the `hostname` variable (`atlas`, `gaia`). The marker is the only
record of what this role owns; nothing is stored anywhere else. On each run, for the host it runs
on:

| On the router | Claimed | Not claimed |
|---|---|---|
| No alias with that name | created | - |
| Alias marked by this host | updated if it differs | **deleted** |
| Hand-made alias under the claimed parent | **adopted**: its description becomes the marker | left alone |
| Hand-made alias under another parent | run fails | left alone |
| Alias marked by another host | run fails | left alone |

A run that fails for any of these reasons writes nothing, and reports every problem at once. The
same goes for a name that is a host override itself, a name appearing on more than one alias, a
name claimed twice on the host, and a parent that does not exist or is disabled. **Host overrides
are never created, changed or deleted** - they are made by hand, and this only manages the aliases
under them.

Moving an app to another host is therefore two runs: deploy the old host without the claim, which
deletes its alias, then the new one with it.

## How a change is applied

Saving an alias changes nothing Unbound answers. After any change, the role calls
`POST /api/unbound/service/reconfigure` once - what the GUI's **Apply** button calls. That
regenerates Unbound's configuration and **restarts** it: a full stop and start, which drops its
cache. This was checked against the router rather than assumed (OPNsense 2026-10-31):

| Change | Lookup after saving | Lookup after reconfigure | Unbound uptime after |
|---|---|---|---|
| Alias added | not found | resolves | 0s |
| Alias deleted | still resolves | not found | 0s |

`service/restart` behaves the same way, so calling it as well would only restart Unbound twice.

If a write fails after earlier ones succeeded, what was saved is still applied before the run
fails, so no saved change is left waiting for someone else's Apply to go live.

## What it does

On the `before` hook only, so a name resolves by the time the app serving it starts:

1. Checks every claim has `name` and `parent` and nothing else, naming the app when not.
2. On the control node (`delegate_to: localhost`, with `become` off), reads every host override and
   alias from the router, works out what to create, update and delete, and fails without writing if
   any claim cannot be satisfied.
3. Writes the changes, then restarts Unbound once if there were any.

In check mode it reads the router and reports what it would change; with `--diff` it shows this
host's aliases, and any it would adopt, before and after.

## Things to know

- **Every change restarts Unbound.** That is a brief gap in DNS for the whole network, and a cold
  cache after it. A run that changes nothing does not restart it.
- **The router API is part of every deploy.** The `before` hook runs whichever app is being
  deployed, so with the router unreachable, every deploy on the host fails - even of an app with no
  DNS claim.
- **Removing the last claim on a host does not delete its alias.** `quadlet_stack` only runs a
  provider some app names, so once no app on the host claims a name, this role is not run at all
  there. Delete that last alias by hand.
- **Changing `opnsense_dns_alias_owner` orphans every alias marked with the old value**: no host
  deletes them, and claiming them again fails as owned by someone else.

## Router setup

The API user needs the **Services: Unbound** privilege. The narrower *Services: Unbound DNS: Edit
Host and Domain Override* covers the aliases but not restarting Unbound.

## Variables

| | default | |
|---|---|---|
| `opnsense_dns_alias_api_url` | required | the router's base URL, without `/api` |
| `opnsense_dns_alias_api_key` | required | |
| `opnsense_dns_alias_api_secret` | required | |
| `opnsense_dns_alias_owner` | first label of `hostname` | the name in this host's markers |
| `opnsense_dns_alias_validate_certs` | `true` | verify the router's TLS certificate |
| `opnsense_dns_alias_timeout` | `30` | seconds per request |

The work is done by the `kriansa.commons.opnsense_unbound_aliases` module, which can be used on its
own.

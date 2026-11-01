# Copyright (c) 2026 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
OPNsense's Unbound host-override aliases, as one host's apps claim them.

An alias is an extra name for a host override: `git.svc.lan.cx` hanging from `atlas.svc.lan.cx`
answers with atlas's address. Each alias this code manages carries a marker in its description
naming the app that claimed it and the host that app runs on. The marker is the only record of
ownership: no state is kept anywhere else, so a host can tell the aliases it created from the ones
written by hand, or by another host, just by reading the router.
"""

import json
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError

from ansible.module_utils.urls import open_url

MARKER_PREFIX = "Managed by Ansible: "

_MARKER = re.compile(r"^Managed by Ansible: (?P<app>\S+) on (?P<owner>\S+)$")


class OPNsenseError(Exception):
    """A request to the router failed, or the router refused what it was asked to do."""


def marker(app, owner):
    """The description written on every alias this code manages."""
    return f"{MARKER_PREFIX}{app} on {owner}"


def parse_marker(description):
    """
    Read who owns an alias from its description.

    Returns:
        (app, owner), or None for a description this code did not write
    """
    match = _MARKER.match(description or "")
    return (match["app"], match["owner"]) if match else None


def normalize(name):
    """DNS names compare without case and without a trailing dot."""
    return name.strip().rstrip(".").lower()


def fqdn(hostname, domain):
    """The full name of an override or alias, from the two fields OPNsense keeps it in."""
    return normalize(f"{hostname}.{domain}" if hostname else domain)


def split_name(name):
    """
    Split a full name the way OPNsense stores an alias: the first label, then the rest.

    Returns:
        (hostname, domain), or None when the name has no domain to split off
    """
    hostname, _, domain = name.partition(".")
    if not hostname or not domain:
        return None
    return hostname, domain


@dataclass
class Change:
    """One write to the router: an alias to create, update or delete."""

    action: str
    name: str
    parent: str
    app: str = None
    uuid: str = None
    fields: dict = None

    def describe(self):
        owner = f" ({self.app})" if self.app else ""
        return f"{self.name} → {self.parent}{owner}"


@dataclass
class Plan:
    """What it takes to make the router match the claims, or why it cannot be done."""

    creates: list = field(default_factory=list)
    updates: list = field(default_factory=list)
    deletes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)

    @property
    def changes(self):
        return self.updates + self.creates + self.deletes

    @property
    def changed(self):
        return bool(self.changes)


def _line(name, parent, description, enabled):
    """One alias, as the diff shows it."""
    disabled = "" if enabled == "1" else " (disabled)"
    return f"{name} → {parent}: {description}{disabled}"


def _index(rows):
    """Rows by full name. A name can appear on more than one row: nothing on the router stops it."""
    by_name = {}
    for row in rows:
        by_name.setdefault(fqdn(row.get("hostname", ""), row.get("domain", "")), []).append(row)
    return by_name


def plan(claims, overrides, aliases, owner):
    """
    Work out the writes that bring the aliases `owner` manages in line with `claims`.

    Every problem is collected rather than stopping at the first, so a run reports all of them at
    once. When there are errors the other lists are not to be acted on.

    Args:
        claims: the aliases wanted, as dicts of name, parent and app
        overrides: every host override on the router, as search_host_override returns them
        aliases: every host alias on the router, as search_host_alias returns them
        owner: the name of the host the claims come from, as written in the marker
    """
    result = Plan()
    overrides_by_name = _index(overrides)
    aliases_by_name = _index(aliases)
    parent_names = {row["uuid"]: name for name, rows in overrides_by_name.items() for row in rows}

    wanted = {}
    for claim in claims:
        name = normalize(claim["name"])
        if name in wanted:
            result.errors.append(
                f"{name} is claimed by both {wanted[name]['app']} and {claim['app']} on {owner}"
            )
            continue
        wanted[name] = {**claim, "name": name, "parent": normalize(claim["parent"])}

    for name, claim in sorted(wanted.items()):
        app, parent = claim["app"], claim["parent"]
        split = split_name(name)
        if split is None:
            result.errors.append(f"{app}: {name} is not a full name, as git.svc.lan.cx is")
            continue

        if name in overrides_by_name:
            result.errors.append(
                f"{app}: {name} is a host override of its own on the router, so it cannot also be"
                " an alias. Delete the override by hand if the app is meant to own the name."
            )
            continue

        parents = overrides_by_name.get(parent, [])
        if not parents:
            result.errors.append(
                f"{app}: {name} hangs from {parent}, which is not a host override on the router."
                " Overrides are created by hand; this only manages the aliases under them."
            )
            continue
        if len(parents) > 1:
            result.errors.append(
                f"{app}: {name} hangs from {parent}, and the router has {len(parents)} host"
                " overrides with that name, so which one to use is ambiguous"
            )
            continue
        if parents[0].get("enabled") != "1":
            result.errors.append(
                f"{app}: {name} hangs from {parent}, which is disabled on the router, so the name"
                " would not resolve"
            )
            continue

        existing = aliases_by_name.get(name, [])
        if len(existing) > 1:
            result.errors.append(
                f"{app}: the router has {len(existing)} aliases called {name}. Delete all but one"
                " by hand."
            )
            continue

        fields = {
            "enabled": "1",
            "host": parents[0]["uuid"],
            "hostname": split[0],
            "domain": split[1],
            "description": marker(app, owner),
        }
        change = Change("create", name, parent, app=app, fields=fields)

        if not existing:
            result.creates.append(change)
            result.after.append(_line(name, parent, fields["description"], "1"))
            continue

        row = existing[0]
        current_parent = parent_names.get(row.get("host"), row.get("%host", "?"))
        claimed_by = parse_marker(row.get("description"))
        if claimed_by and claimed_by[1] != owner:
            result.errors.append(
                f"{app}: {name} is already claimed by {claimed_by[0]} on {claimed_by[1]}. Remove"
                f" that claim and deploy {claimed_by[1]} first."
            )
            continue
        if not claimed_by and row.get("host") != fields["host"]:
            result.errors.append(
                f"{app}: {name} already exists by hand under {current_parent}, not {parent}. Move"
                " or delete it by hand, or claim it under the parent it has."
            )
            continue

        # Either this host's own alias, or a hand-made one under the claimed parent, which is
        # adopted: the marker written on it is what makes it this host's from now on
        result.before.append(
            _line(name, current_parent, row.get("description", ""), row.get("enabled"))
        )
        result.after.append(_line(name, parent, fields["description"], "1"))
        current = {key: row.get(key) for key in fields}
        if current != fields:
            change.action, change.uuid = "update", row["uuid"]
            result.updates.append(change)

    for name, rows in sorted(aliases_by_name.items()):
        for row in rows:
            claimed_by = parse_marker(row.get("description"))
            if not claimed_by or claimed_by[1] != owner or name in wanted:
                continue
            parent = parent_names.get(row.get("host"), row.get("%host", "?"))
            result.before.append(
                _line(name, parent, row.get("description", ""), row.get("enabled"))
            )
            result.deletes.append(
                Change("delete", name, parent, app=claimed_by[0], uuid=row["uuid"])
            )

    result.before.sort()
    result.after.sort()
    return result


class Client:
    """The few calls of OPNsense's Unbound API this needs."""

    def __init__(self, api_url, key, secret, validate_certs=True, timeout=30):
        self.api_url = api_url.rstrip("/")
        self.key = key
        self.secret = secret
        self.validate_certs = validate_certs
        self.timeout = timeout

    def _request(self, method, path, body=None):
        url = f"{self.api_url}/api/{path}"
        headers = {"Accept": "application/json"}
        data = None
        # OPNsense only acts on a POST that carries a JSON body, even an empty one
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"

        try:
            response = open_url(
                url,
                method=method,
                data=data,
                headers=headers,
                url_username=self.key,
                url_password=self.secret,
                force_basic_auth=True,
                validate_certs=self.validate_certs,
                timeout=self.timeout,
            )
            raw = response.read()
        except HTTPError as error:
            hint = {
                401: " - the API key or secret is wrong",
                403: " - the API user needs the 'Services: Unbound' privilege",
            }.get(error.code, "")
            raise OPNsenseError(f"{method} {url} answered HTTP {error.code}{hint}") from error
        except (URLError, OSError) as error:
            raise OPNsenseError(f"{method} {url} failed: {error}") from error

        try:
            return json.loads(raw)
        except ValueError as error:
            raise OPNsenseError(
                f"{method} {url} did not answer with JSON: {raw[:200]!r}"
            ) from error

    def _search(self, what):
        path = f"unbound/settings/search_{what}/?rowCount=-1"
        answer = self._request("GET", path)
        rows = answer.get("rows") if isinstance(answer, dict) else None
        if not isinstance(rows, list):
            raise OPNsenseError(f"search_{what} answered without rows: {answer}")
        # Acting on part of the list would delete what it did not see as claimed
        if answer.get("total", len(rows)) != len(rows):
            raise OPNsenseError(
                f"search_{what} returned {len(rows)} of {answer.get('total')} rows; refusing to"
                " act on a partial list"
            )
        return rows

    def search_host_overrides(self):
        return self._search("host_override")

    def search_host_aliases(self):
        return self._search("host_alias")

    def _write(self, path, body, expected):
        # A refused write still answers HTTP 200, with the reason in `validations`
        answer = self._request("POST", path, body)
        if not isinstance(answer, dict) or answer.get("result") != expected:
            raise OPNsenseError(f"{path} was refused: {answer}")
        return answer

    def add_alias(self, fields):
        return self._write("unbound/settings/add_host_alias/", {"alias": fields}, "saved")["uuid"]

    def set_alias(self, uuid, fields):
        self._write(f"unbound/settings/set_host_alias/{uuid}", {"alias": fields}, "saved")

    def del_alias(self, uuid):
        self._write(f"unbound/settings/del_host_alias/{uuid}", None, "deleted")

    def apply(self):
        """
        Make Unbound serve what was saved: saving an alias alone changes nothing it answers.

        reconfigure is what the GUI's Apply button calls. It regenerates Unbound's configuration and
        restarts it - a full stop and start, not a reload - which drops its cache, so a name a
        client was just told does not exist resolves straight away. Calling restart as well would
        only restart Unbound a second time.
        """
        answer = self._request("POST", "unbound/service/reconfigure")
        if not isinstance(answer, dict) or answer.get("status") != "ok":
            raise OPNsenseError(f"unbound/service/reconfigure failed: {answer}")

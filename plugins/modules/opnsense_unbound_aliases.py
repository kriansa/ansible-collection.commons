# Copyright (c) 2026 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

DOCUMENTATION = r"""
---
module: opnsense_unbound_aliases
short_description: Keep the Unbound host-override aliases one host owns on OPNsense as claimed
version_added: "4.2.0"
description:
    - Makes the Unbound host-override aliases on an OPNsense router that belong to O(owner) match
      O(aliases) exactly - creating, updating and deleting them - then restarts Unbound once if
      anything changed. Saving an alias alone changes nothing Unbound answers.
    - Ownership is recorded in each alias's description, as
      C(Managed by Ansible: <app> on <owner>). No state is kept anywhere else.
    - An alias written by hand, with the claimed name and under the claimed parent, is adopted - its
      description is replaced by the marker. Hand-made aliases nothing claims are never touched.
    - An alias of this owner that nothing claims any more is deleted.
    - Fails, without writing anything, when a claim cannot be satisfied safely - the name belongs to
      another owner, exists by hand under another parent, is a host override itself, or its parent
      does not exist. Every such problem is reported at once.
    - The M(kriansa.commons.opnsense_dns_alias) role runs this for the apps of a host.
options:
    api_url:
        description:
            - The router's base URL, without C(/api).
        type: str
        required: true
    api_key:
        description:
            - The API key. The API user needs the C(Services: Unbound) privilege - the narrower
              override one does not cover restarting Unbound.
        type: str
        required: true
    api_secret:
        description:
            - The API secret.
        type: str
        required: true
    validate_certs:
        description:
            - Verify the router's TLS certificate.
        type: bool
        default: true
    timeout:
        description:
            - Seconds to wait for each request.
        type: int
        default: 30
    owner:
        description:
            - The name this host's aliases are marked with. Changing it orphans every alias already
              marked with the old one.
        type: str
        required: true
    aliases:
        description:
            - Every alias this owner should have. One left out is deleted if this owner made it.
        type: list
        elements: dict
        required: true
        suboptions:
            name:
                description:
                    - The full name to publish, as C(git.svc.lan.cx).
                type: str
                required: true
            parent:
                description:
                    - The full name of the host override it is an alias of, as
                      C(atlas.svc.lan.cx). It has to exist; overrides are created by hand.
                type: str
                required: true
            app:
                description:
                    - The app claiming it, written into the marker.
                type: str
                required: true
attributes:
    check_mode:
        support: full
        description: Reads the router and reports what it would change, without writing.
    diff_mode:
        support: full
        description: Shows this owner's aliases, and any it would adopt, before and after.
author:
    - Daniel Pereira (@kriansa)
notes:
    - Restarting Unbound drops its cache and briefly stops it answering for the whole network. It
      only happens on a run that changed something.
"""

EXAMPLES = r"""
- name: Publish this host's names
  kriansa.commons.opnsense_unbound_aliases:
    api_url: https://router.example.lan
    api_key: "{{ lookup('passwordstore', 'Router/API subkey=key') }}"
    api_secret: "{{ lookup('passwordstore', 'Router/API') }}"
    owner: atlas
    aliases:
      - { name: git.svc.example.lan, parent: atlas.svc.example.lan, app: git-server }
  delegate_to: localhost
"""

RETURN = r"""
created:
    description: The aliases created, as C(name → parent (app)).
    returned: always
    type: list
    elements: str
updated:
    description: The aliases updated or adopted, as C(name → parent (app)).
    returned: always
    type: list
    elements: str
deleted:
    description: The aliases deleted, as C(name → parent (app)).
    returned: always
    type: list
    elements: str
applied:
    description: Whether Unbound was restarted to serve the changes.
    returned: always
    type: bool
errors:
    description: Every claim that could not be satisfied, when the module fails because of them.
    returned: failure
    type: list
    elements: str
"""

import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.kriansa.commons.plugins.module_utils.opnsense_unbound import (
    Client,
    OPNsenseError,
    plan,
)

ARGUMENT_SPEC = {
    "api_url": {"type": "str", "required": True},
    "api_key": {"type": "str", "required": True, "no_log": True},
    "api_secret": {"type": "str", "required": True, "no_log": True},
    "validate_certs": {"type": "bool", "default": True},
    "timeout": {"type": "int", "default": 30},
    "owner": {"type": "str", "required": True},
    "aliases": {
        "type": "list",
        "elements": "dict",
        "required": True,
        "options": {
            "name": {"type": "str", "required": True},
            "parent": {"type": "str", "required": True},
            "app": {"type": "str", "required": True},
        },
    },
}

# The marker is read back by splitting on spaces, so neither may contain one
_WORD = re.compile(r"^\S+$")


def _write(client, change):
    if change.action == "create":
        client.add_alias(change.fields)
    elif change.action == "update":
        client.set_alias(change.uuid, change.fields)
    else:
        client.del_alias(change.uuid)


def run(module, client):
    params = module.params
    for value in [params["owner"]] + [alias["app"] for alias in params["aliases"]]:
        if not _WORD.match(value):
            module.fail_json(
                msg=f"owner and app names cannot be empty or contain spaces: {value!r}"
            )

    try:
        result = plan(
            params["aliases"],
            client.search_host_overrides(),
            client.search_host_aliases(),
            params["owner"],
        )
    except OPNsenseError as error:
        module.fail_json(msg=str(error))

    if result.errors:
        module.fail_json(
            msg="Nothing was changed. " + " | ".join(result.errors), errors=result.errors
        )

    report = {
        "changed": result.changed,
        "created": [change.describe() for change in result.creates],
        "updated": [change.describe() for change in result.updates],
        "deleted": [change.describe() for change in result.deletes],
        "applied": False,
    }
    if module._diff:
        report["diff"] = {
            "before": "".join(f"{line}\n" for line in result.before),
            "after": "".join(f"{line}\n" for line in result.after),
        }

    if module.check_mode or not result.changed:
        module.exit_json(**report)

    done, failure = [], None
    for change in result.changes:
        try:
            _write(client, change)
        except OPNsenseError as error:
            failure = error
            break
        done.append(change)
    report["changed"] = bool(done)

    # A write saved on the router but never applied would go live whenever anyone next applies
    # Unbound's configuration, far from this run. So whatever was saved is applied, even when a
    # later write failed.
    apply_error = None
    if done:
        try:
            client.apply()
            report["applied"] = True
        except OPNsenseError as error:
            apply_error = error

    if failure or apply_error:
        problems = " | ".join(str(error) for error in (failure, apply_error) if error)
        if not done:
            outcome = "Nothing was saved."
        elif report["applied"]:
            outcome = "Saved and applied before that: "
        else:
            outcome = "Saved but NOT applied, so Unbound still serves the old names: "
        if done:
            outcome += ", ".join(change.describe() for change in done) + "."
        module.fail_json(msg=f"{problems}. {outcome}", **report)

    module.exit_json(**report)


def main():
    module = AnsibleModule(argument_spec=ARGUMENT_SPEC, supports_check_mode=True)
    params = module.params
    client = Client(
        params["api_url"],
        params["api_key"],
        params["api_secret"],
        validate_certs=params["validate_certs"],
        timeout=params["timeout"],
    )
    run(module, client)


if __name__ == "__main__":
    main()

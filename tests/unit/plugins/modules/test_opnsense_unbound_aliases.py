"""
The module through a real AnsibleModule: check mode, diff, what it writes, and how it fails.

The router is a fake client holding override and alias rows the way OPNsense returns them.
"""

import json

import pytest
from ansible.module_utils.testing import patch_module_args

from ansible_collections.kriansa.commons.plugins.module_utils.opnsense_unbound import (
    OPNsenseError,
    marker,
)
from ansible_collections.kriansa.commons.plugins.modules import opnsense_unbound_aliases

SVC = "11111111-0000-0000-0000-000000000000"
SECRET = "s3cr3t-value-that-must-not-be-printed"


class FakeClient:
    def __init__(self, aliases=(), fail_on=None, apply_fails=False):
        self.overrides = [
            {"uuid": SVC, "enabled": "1", "hostname": "web", "domain": "svc.example.lan"}
        ]
        self.aliases = list(aliases)
        self.fail_on = fail_on
        self.apply_fails = apply_fails
        self.calls = []

    def search_host_overrides(self):
        return self.overrides

    def search_host_aliases(self):
        return self.aliases

    def _call(self, *call):
        if self.fail_on == call[0]:
            raise OPNsenseError(f"{call[0]} was refused")
        self.calls.append(call)

    def add_alias(self, fields):
        self._call("add", fields["hostname"])

    def set_alias(self, uuid, fields):
        self._call("set", uuid)

    def del_alias(self, uuid):
        self._call("del", uuid)

    def apply(self):
        if self.apply_fails:
            raise OPNsenseError("restart failed")
        self.calls.append(("apply",))


def row(uuid, name, description):
    hostname, _, domain = name.partition(".")
    return {
        "uuid": uuid,
        "enabled": "1",
        "host": SVC,
        "%host": "web.svc.example.lan",
        "hostname": hostname,
        "domain": domain,
        "description": description,
    }


def args(*names, **extra):
    return {
        "api_url": "https://router.example.lan",
        "api_key": "KEY",
        "api_secret": SECRET,
        "owner": "web",
        "aliases": [{"name": n, "parent": "web.svc.example.lan", "app": "blog"} for n in names],
        **extra,
    }


@pytest.fixture
def router(monkeypatch):
    """Installs a fake client in place of the real one; set .client before calling main()."""

    class Holder:
        client = FakeClient()

    holder = Holder()
    monkeypatch.setattr(opnsense_unbound_aliases, "Client", lambda *a, **k: holder.client)
    return holder


def call_main(capsys, module_args):
    with patch_module_args(module_args), pytest.raises(SystemExit):
        opnsense_unbound_aliases.main()
    out = capsys.readouterr().out
    assert SECRET not in out
    return json.loads(out)


def test_writes_then_applies_once(capsys, router):
    router.client = FakeClient(
        [
            row("a1", "old.svc.example.lan", "Old"),
            row("a2", "gone.svc.example.lan", marker("x", "web")),
        ]
    )
    result = call_main(capsys, args("old.svc.example.lan", "new.svc.example.lan"))
    assert result["changed"] is True
    assert result["applied"] is True
    assert router.client.calls == [("set", "a1"), ("add", "new"), ("del", "a2"), ("apply",)]
    assert result["created"] == ["new.svc.example.lan → web.svc.example.lan (blog)"]
    assert result["updated"] == ["old.svc.example.lan → web.svc.example.lan (blog)"]
    assert result["deleted"] == ["gone.svc.example.lan → web.svc.example.lan (x)"]


def test_nothing_to_do_writes_and_applies_nothing(capsys, router):
    router.client = FakeClient([row("a1", "blog.svc.example.lan", marker("blog", "web"))])
    result = call_main(capsys, args("blog.svc.example.lan"))
    assert result["changed"] is False
    assert result["applied"] is False
    assert router.client.calls == []


def test_check_mode_reports_without_writing(capsys, router):
    router.client = FakeClient([row("a1", "blog.svc.example.lan", "Blog")])
    result = call_main(
        capsys, args("blog.svc.example.lan", _ansible_check_mode=True, _ansible_diff=True)
    )
    assert result["changed"] is True
    assert router.client.calls == []
    assert result["diff"] == {
        "before": "blog.svc.example.lan → web.svc.example.lan: Blog\n",
        "after": "blog.svc.example.lan → web.svc.example.lan: Managed by Ansible: blog on web\n",
    }


def test_claim_errors_fail_before_any_write(capsys, router):
    router.client = FakeClient([row("a1", "taken.svc.example.lan", marker("x", "other"))])
    result = call_main(capsys, args("taken.svc.example.lan", "new.svc.example.lan"))
    assert result["failed"] is True
    assert result["msg"].startswith("Nothing was changed.")
    assert result["errors"] == [
        (
            "blog: taken.svc.example.lan is already claimed by x on other. Remove that claim and"
            " deploy other first."
        )
    ]
    assert router.client.calls == []


def test_a_failed_write_still_applies_what_was_saved(capsys, router):
    router.client = FakeClient([row("a1", "old.svc.example.lan", "Old")], fail_on="add")
    result = call_main(capsys, args("old.svc.example.lan", "new.svc.example.lan"))
    assert result["failed"] is True
    assert result["changed"] is True
    assert result["applied"] is True
    assert router.client.calls == [("set", "a1"), ("apply",)]
    assert "add was refused" in result["msg"]
    assert "Saved and applied before that: old.svc.example.lan" in result["msg"]


def test_a_first_write_failing_saves_and_applies_nothing(capsys, router):
    router.client = FakeClient(fail_on="add")
    result = call_main(capsys, args("new.svc.example.lan"))
    assert result["failed"] is True
    assert result["changed"] is False
    assert router.client.calls == []
    assert "Nothing was saved." in result["msg"]


def test_a_failed_apply_says_the_changes_are_not_live(capsys, router):
    router.client = FakeClient(apply_fails=True)
    result = call_main(capsys, args("new.svc.example.lan"))
    assert result["failed"] is True
    assert result["applied"] is False
    assert "NOT applied" in result["msg"]


def test_a_read_failure_fails_cleanly(capsys, router):
    class Unreachable(FakeClient):
        def search_host_overrides(self):
            raise OPNsenseError("GET https://router.example.lan/api/... failed: timed out")

    router.client = Unreachable()
    result = call_main(capsys, args("new.svc.example.lan"))
    assert result["failed"] is True
    assert "timed out" in result["msg"]


@pytest.mark.parametrize("owner", ["", "two words"])
def test_refuses_an_owner_the_marker_could_not_be_read_back_with(capsys, router, owner):
    result = call_main(capsys, args("new.svc.example.lan", owner=owner))
    assert result["failed"] is True
    assert "cannot be empty or contain spaces" in result["msg"]


def test_the_secret_is_never_printed(capsys, router):
    router.client = FakeClient(fail_on="add")
    call_main(capsys, args("new.svc.example.lan", _ansible_diff=True))

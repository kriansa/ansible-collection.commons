"""
The planner that turns claims into writes, and the client that talks to the router.

The rows below have the shape OPNsense's search_host_override and search_host_alias return.
"""

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from ansible_collections.kriansa.commons.plugins.module_utils import opnsense_unbound
from ansible_collections.kriansa.commons.plugins.module_utils.opnsense_unbound import (
    Client,
    OPNsenseError,
    marker,
    parse_marker,
    plan,
)

SVC = "11111111-0000-0000-0000-000000000000"
ADMIN = "22222222-0000-0000-0000-000000000000"
OTHER = "33333333-0000-0000-0000-000000000000"

OVERRIDES = [
    {"uuid": SVC, "enabled": "1", "hostname": "web", "domain": "svc.example.lan"},
    {"uuid": ADMIN, "enabled": "1", "hostname": "web", "domain": "admin.example.lan"},
    {"uuid": OTHER, "enabled": "1", "hostname": "db", "domain": "svc.example.lan"},
]


def alias(uuid, host, name, description, enabled="1"):
    hostname, _, domain = name.partition(".")
    return {
        "uuid": uuid,
        "enabled": enabled,
        "host": host,
        "%host": {SVC: "web.svc.example.lan", ADMIN: "web.admin.example.lan"}.get(host, "?"),
        "hostname": hostname,
        "domain": domain,
        "description": description,
    }


def claim(name, parent="web.svc.example.lan", app="blog"):
    return {"name": name, "parent": parent, "app": app}


def fields(name, host=SVC, app="blog", owner="web"):
    hostname, _, domain = name.partition(".")
    return {
        "enabled": "1",
        "host": host,
        "hostname": hostname,
        "domain": domain,
        "description": marker(app, owner),
    }


class TestMarker:
    def test_round_trip(self):
        assert parse_marker(marker("git-server", "atlas")) == ("git-server", "atlas")

    @pytest.mark.parametrize(
        "description",
        ["", None, "Git Server", "Managed by Ansible: two words on atlas", "Managed by Ansible: x"],
    )
    def test_a_description_this_did_not_write_has_no_owner(self, description):
        assert parse_marker(description) is None


class TestPlan:
    def test_nothing_claimed_and_nothing_owned_changes_nothing(self):
        result = plan([], OVERRIDES, [alias("a1", SVC, "hand.svc.example.lan", "by hand")], "web")
        assert not result.changed
        assert result.errors == []

    def test_creates_a_name_nobody_has(self):
        result = plan([claim("blog.svc.example.lan")], OVERRIDES, [], "web")
        assert [(c.action, c.name, c.fields) for c in result.creates] == [
            ("create", "blog.svc.example.lan", fields("blog.svc.example.lan"))
        ]
        assert result.after == [
            "blog.svc.example.lan → web.svc.example.lan: Managed by Ansible: blog on web"
        ]

    def test_names_compare_without_case_or_trailing_dot(self):
        existing = alias("a1", SVC, "blog.svc.example.lan", marker("blog", "web"))
        result = plan(
            [claim("Blog.SVC.example.lan.", parent="WEB.svc.example.lan.")],
            OVERRIDES,
            [existing],
            "web",
        )
        assert result.errors == []
        assert not result.changed

    def test_splits_at_the_first_label(self):
        result = plan([claim("chat.example.lan")], OVERRIDES, [], "web")
        assert result.creates[0].fields["hostname"] == "chat"
        assert result.creates[0].fields["domain"] == "example.lan"

    def test_adopts_a_hand_made_alias_under_the_claimed_parent(self):
        existing = alias("a1", SVC, "blog.svc.example.lan", "Blog", enabled="0")
        result = plan([claim("blog.svc.example.lan")], OVERRIDES, [existing], "web")
        assert [(c.action, c.uuid, c.fields) for c in result.updates] == [
            ("update", "a1", fields("blog.svc.example.lan"))
        ]
        assert result.before == ["blog.svc.example.lan → web.svc.example.lan: Blog (disabled)"]

    def test_leaves_an_owned_alias_that_already_matches(self):
        existing = alias("a1", SVC, "blog.svc.example.lan", marker("blog", "web"))
        result = plan([claim("blog.svc.example.lan")], OVERRIDES, [existing], "web")
        assert not result.changed
        assert result.before == result.after

    def test_moves_an_owned_alias_to_another_parent_of_the_same_owner(self):
        existing = alias("a1", SVC, "blog.svc.example.lan", marker("blog", "web"))
        result = plan(
            [claim("blog.svc.example.lan", parent="web.admin.example.lan")],
            OVERRIDES,
            [existing],
            "web",
        )
        assert [(c.uuid, c.fields["host"]) for c in result.updates] == [("a1", ADMIN)]

    def test_rewrites_the_marker_when_another_app_of_the_same_owner_claims_the_name(self):
        existing = alias("a1", SVC, "blog.svc.example.lan", marker("old-blog", "web"))
        result = plan([claim("blog.svc.example.lan")], OVERRIDES, [existing], "web")
        assert result.updates[0].fields["description"] == marker("blog", "web")

    def test_deletes_an_owned_alias_nothing_claims(self):
        existing = alias("a1", SVC, "gone.svc.example.lan", marker("gone", "web"))
        result = plan([], OVERRIDES, [existing], "web")
        assert [(c.action, c.uuid, c.describe()) for c in result.deletes] == [
            ("delete", "a1", "gone.svc.example.lan → web.svc.example.lan (gone)")
        ]
        assert result.after == []

    def test_never_touches_another_owners_or_hand_made_aliases(self):
        rows = [
            alias("a1", SVC, "theirs.svc.example.lan", marker("x", "other-host")),
            alias("a2", SVC, "hand.svc.example.lan", "by hand"),
        ]
        result = plan([], OVERRIDES, rows, "web")
        assert not result.changed
        assert result.before == []

    def test_create_update_and_delete_together(self):
        rows = [
            alias("a1", SVC, "keep.svc.example.lan", "Keep"),
            alias("a2", SVC, "gone.svc.example.lan", marker("gone", "web")),
        ]
        result = plan(
            [claim("keep.svc.example.lan"), claim("new.svc.example.lan")], OVERRIDES, rows, "web"
        )
        assert [c.action for c in result.changes] == ["update", "create", "delete"]

    def test_reports_every_error_at_once(self):
        rows = [
            alias("a1", SVC, "taken.svc.example.lan", marker("x", "other-host")),
            alias("a2", ADMIN, "elsewhere.svc.example.lan", "by hand"),
            alias("a3", SVC, "twice.svc.example.lan", "one"),
            alias("a4", SVC, "twice.svc.example.lan", "two"),
        ]
        claims = [
            claim("taken.svc.example.lan"),
            claim("elsewhere.svc.example.lan"),
            claim("twice.svc.example.lan"),
            claim("db.svc.example.lan"),
            claim("orphan.svc.example.lan", parent="missing.svc.example.lan"),
            claim("toplevel"),
            claim("dup.svc.example.lan", app="one"),
            claim("dup.svc.example.lan", app="two"),
        ]
        result = plan(claims, OVERRIDES, rows, "web")
        messages = "\n".join(result.errors)
        assert len(result.errors) == 7, messages
        assert "already claimed by x on other-host" in messages
        assert "already exists by hand under web.admin.example.lan" in messages
        assert "2 aliases called twice.svc.example.lan" in messages
        assert "db.svc.example.lan is a host override of its own" in messages
        assert "missing.svc.example.lan, which is not a host override" in messages
        assert "toplevel is not a full name" in messages
        assert "claimed by both one and two" in messages

    def test_refuses_an_ambiguous_parent(self):
        overrides = OVERRIDES + [dict(OVERRIDES[0], uuid="dup")]
        result = plan([claim("blog.svc.example.lan")], overrides, [], "web")
        assert "2 host overrides with that name" in result.errors[0]

    def test_refuses_a_disabled_parent(self):
        overrides = [dict(OVERRIDES[0], enabled="0")]
        result = plan([claim("blog.svc.example.lan")], overrides, [], "web")
        assert "which is disabled" in result.errors[0]


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


@pytest.fixture
def router(monkeypatch):
    """Answers open_url with the queued bodies, and records every request."""

    class Router:
        def __init__(self):
            self.requests = []
            self.answers = []

        def queue(self, *answers):
            self.answers.extend(answers)

        def open_url(self, url, **kwargs):
            self.requests.append((kwargs["method"], url, kwargs.get("data"), kwargs))
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return FakeResponse(answer if isinstance(answer, bytes) else json.dumps(answer))

    fake = Router()
    monkeypatch.setattr(opnsense_unbound, "open_url", fake.open_url)
    return fake


@pytest.fixture
def client():
    return Client("https://router.example.lan/", "KEY", "SECRET", validate_certs=False, timeout=5)


def http_error(code):
    return HTTPError("https://router.example.lan", code, "nope", {}, io.BytesIO(b""))


class TestClient:
    def test_searches_every_row_with_basic_auth(self, client, router):
        router.queue({"rows": [{"uuid": "a"}], "total": 1})
        assert client.search_host_aliases() == [{"uuid": "a"}]
        method, url, data, kwargs = router.requests[0]
        assert (method, url, data) == (
            "GET",
            "https://router.example.lan/api/unbound/settings/search_host_alias/?rowCount=-1",
            None,
        )
        assert kwargs["url_username"] == "KEY"
        assert kwargs["url_password"] == "SECRET"
        assert kwargs["force_basic_auth"] is True
        assert kwargs["validate_certs"] is False
        assert kwargs["timeout"] == 5

    def test_refuses_a_partial_list(self, client, router):
        router.queue({"rows": [{"uuid": "a"}], "total": 2})
        with pytest.raises(OPNsenseError, match="partial list"):
            client.search_host_overrides()

    def test_refuses_an_answer_without_rows(self, client, router):
        router.queue({"status": 403})
        with pytest.raises(OPNsenseError, match="without rows"):
            client.search_host_overrides()

    @pytest.mark.parametrize(
        ("code", "hint"), [(401, "key or secret is wrong"), (403, "Services: Unbound"), (500, "")]
    )
    def test_explains_http_errors(self, client, router, code, hint):
        router.queue(http_error(code))
        with pytest.raises(OPNsenseError, match=f"HTTP {code}.*{hint}"):
            client.search_host_overrides()

    def test_reports_an_unreachable_router(self, client, router):
        router.queue(URLError("connection refused"))
        with pytest.raises(OPNsenseError, match="connection refused"):
            client.search_host_overrides()

    def test_reports_an_answer_that_is_not_json(self, client, router):
        router.queue(b"<html>login</html>")
        with pytest.raises(OPNsenseError, match="did not answer with JSON"):
            client.search_host_overrides()

    def test_adds_an_alias_and_returns_its_uuid(self, client, router):
        router.queue({"result": "saved", "uuid": "new"})
        assert client.add_alias({"hostname": "x"}) == "new"
        method, url, data, kwargs = router.requests[0]
        assert method == "POST"
        assert url.endswith("/api/unbound/settings/add_host_alias/")
        assert json.loads(data) == {"alias": {"hostname": "x"}}
        assert kwargs["headers"]["Content-Type"] == "application/json"

    def test_a_refused_write_answers_200_and_still_fails(self, client, router):
        router.queue({"result": "failed", "validations": {"alias.domain": "bad"}})
        with pytest.raises(OPNsenseError, match="alias.domain"):
            client.set_alias("u1", {"domain": "!"})

    def test_deletes_with_an_empty_json_body(self, client, router):
        router.queue({"result": "deleted"})
        client.del_alias("u1")
        _, url, data, _ = router.requests[0]
        assert url.endswith("/api/unbound/settings/del_host_alias/u1")
        assert data == b"{}"

    def test_apply_reconfigures_unbound_once(self, client, router):
        router.queue({"status": "ok"})
        client.apply()
        assert [(m, url.rsplit("/api/", 1)[1], data) for m, url, data, _ in router.requests] == [
            ("POST", "unbound/service/reconfigure", b"{}")
        ]

    def test_a_failed_reconfigure_fails(self, client, router):
        router.queue({"status": "failed"})
        with pytest.raises(OPNsenseError, match="reconfigure failed"):
            client.apply()

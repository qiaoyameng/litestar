from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

import pytest

from litestar import Controller, Litestar, Router, get
from litestar._openapi.plugin import OpenAPIPlugin
from litestar._openapi.utils import prune_unreferenced_components
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.spec import Components, Example, SecurityScheme
from litestar.testing import create_test_client


@dataclass
class Node:
    """A recursive model shared by all audiences."""

    name: str
    children: list[Node] = field(default_factory=list)


@dataclass
class InternalReport:
    secret: str


@dataclass
class PartnerReport:
    public: str


def _make_user(module: str, field_name: str) -> type:
    cls = dataclasses.make_dataclass("User", [(field_name, str)])
    cls.__module__ = module
    return cls


InternalUser = _make_user("internal.models", "internal_field")
PartnerUser = _make_user("partner.models", "partner_field")


@get("/internal/report", audience="internal", security=[{"InternalAuth": []}], sync_to_thread=False)
def internal_report() -> InternalReport: ...


@get("/partner/report", audience="partner", security=[{"PartnerAuth": []}], sync_to_thread=False)
def partner_report() -> PartnerReport: ...


@get("/shared/nodes", audience=["internal", "partner"], sync_to_thread=False)
def shared_nodes() -> Node: ...


@get("/internal/user", audience="internal", sync_to_thread=False)
def internal_user() -> InternalUser: ...


@get("/partner/user", audience="partner", sync_to_thread=False)
def partner_user() -> PartnerUser: ...


@get("/untagged", sync_to_thread=False)
def untagged() -> str: ...


ALL_HANDLERS = [internal_report, partner_report, shared_nodes, internal_user, partner_user, untagged]


def create_openapi_config(**kwargs: Any) -> OpenAPIConfig:
    return OpenAPIConfig(
        title="Audience API",
        version="1.0.0",
        audiences=["internal", "partner"],
        components=Components(
            security_schemes={
                "InternalAuth": SecurityScheme(type="http", scheme="bearer"),
                "PartnerAuth": SecurityScheme(type="apiKey", name="x-api-key", security_scheme_in="header"),
            },
            examples={
                "InternalExample": Example(value={"internal": True}),
                "PartnerExample": Example(value={"partner": True}),
            },
        ),
        **kwargs,
    )


def create_app(**kwargs: Any) -> Litestar:
    return Litestar(route_handlers=list(ALL_HANDLERS), openapi_config=create_openapi_config(**kwargs))


def get_schema(app: Litestar, audience: str | None = None) -> dict[str, Any]:
    return app.plugins.get(OpenAPIPlugin).provide_openapi_schema(audience)


def collect_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref":
                refs.add(item)
            else:
                refs.update(collect_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(collect_refs(item))
    return refs


def assert_all_refs_resolvable(schema: dict[str, Any]) -> None:
    for ref in collect_refs(schema):
        assert ref.startswith("#/"), f"unexpected external ref {ref!r}"
        node: Any = schema
        for part in ref[2:].split("/"):
            assert part in node, f"unresolvable ref {ref!r}"
            node = node[part]


def test_audience_schemas_contain_only_their_operations() -> None:
    app = create_app()

    internal = get_schema(app, "internal")
    partner = get_schema(app, "partner")

    assert sorted(internal["paths"]) == ["/internal/report", "/internal/user", "/shared/nodes"]
    assert sorted(partner["paths"]) == ["/partner/report", "/partner/user", "/shared/nodes"]


def test_default_schema_is_unfiltered() -> None:
    app = create_app()

    default = get_schema(app)

    assert sorted(default["paths"]) == [
        "/internal/report",
        "/internal/user",
        "/partner/report",
        "/partner/user",
        "/shared/nodes",
        "/untagged",
    ]
    # untagged handlers are excluded from audience schemas but remain in the default schema
    assert "/untagged" not in get_schema(app, "internal")["paths"]
    assert "/untagged" not in get_schema(app, "partner")["paths"]
    # components of the default schema are not pruned
    assert sorted(default["components"]["securitySchemes"]) == ["InternalAuth", "PartnerAuth"]
    assert sorted(default["components"]["examples"]) == ["InternalExample", "PartnerExample"]


def test_audience_schemas_prune_unreferenced_components() -> None:
    app = create_app()

    internal = get_schema(app, "internal")
    partner = get_schema(app, "partner")

    # the 'User' models live in different namespaces; within a single audience there is no
    # name collision, so the plain model name is used
    assert sorted(internal["components"]["schemas"]) == ["InternalReport", "Node", "User"]
    assert sorted(partner["components"]["schemas"]) == ["Node", "PartnerReport", "User"]

    # security schemes only used by excluded operations are removed
    assert sorted(internal["components"]["securitySchemes"]) == ["InternalAuth"]
    assert sorted(partner["components"]["securitySchemes"]) == ["PartnerAuth"]

    # examples not referenced by any retained operation are removed
    assert "examples" not in internal["components"]
    assert "examples" not in partner["components"]


def test_audience_openapi_object_components_match_schema() -> None:
    app = create_app()
    plugin = app.plugins.get(OpenAPIPlugin)

    openapi = plugin.provide_openapi("internal")
    schema = plugin.provide_openapi_schema("internal")

    assert sorted(openapi.components.schemas) == sorted(schema["components"]["schemas"])
    assert sorted(openapi.components.security_schemes or ()) == sorted(schema["components"]["securitySchemes"])
    assert not openapi.components.examples


def test_shared_recursive_model_defined_once_and_refs_resolvable() -> None:
    app = create_app()

    for audience in ("internal", "partner"):
        schema = get_schema(app, audience)
        node = schema["components"]["schemas"]["Node"]
        assert node["properties"]["children"] == {"items": {"$ref": "#/components/schemas/Node"}, "type": "array"}
        assert_all_refs_resolvable(schema)


def test_same_name_models_in_different_namespaces() -> None:
    app = create_app()

    internal = get_schema(app, "internal")
    partner = get_schema(app, "partner")
    default = get_schema(app)

    # within a single audience there is no name collision, so the plain name is used
    # and it refers to the correct model
    assert set(internal["components"]["schemas"]["User"]["properties"]) == {"internal_field"}
    assert set(partner["components"]["schemas"]["User"]["properties"]) == {"partner_field"}

    # the operations reference the schema of their own namespace
    internal_ref = internal["paths"]["/internal/user"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    partner_ref = partner["paths"]["/partner/user"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert internal_ref == "#/components/schemas/User"
    assert partner_ref == "#/components/schemas/User"

    # in the default schema both models are present under disambiguated names
    default_schemas = default["components"]["schemas"]
    assert set(default_schemas["internal_models_User"]["properties"]) == {"internal_field"}
    assert set(default_schemas["partner_models_User"]["properties"]) == {"partner_field"}
    assert_all_refs_resolvable(default)


def test_audience_endpoints_serve_alternating_requests() -> None:
    with create_test_client(route_handlers=list(ALL_HANDLERS), openapi_config=create_openapi_config()) as client:
        internal_first = client.get("/schema/internal/openapi.json")
        partner_first = client.get("/schema/partner/openapi.json")
        internal_second = client.get("/schema/internal/openapi.json")
        partner_second = client.get("/schema/partner/openapi.json")
        default = client.get("/schema/openapi.json")

    assert internal_first.status_code == 200
    assert partner_first.status_code == 200
    assert internal_first.json() == internal_second.json()
    assert partner_first.json() == partner_second.json()

    internal = internal_first.json()
    partner = partner_first.json()
    assert sorted(internal["paths"]) == ["/internal/report", "/internal/user", "/shared/nodes"]
    assert sorted(partner["paths"]) == ["/partner/report", "/partner/user", "/shared/nodes"]
    assert sorted(internal["components"]["securitySchemes"]) == ["InternalAuth"]
    assert sorted(partner["components"]["securitySchemes"]) == ["PartnerAuth"]
    assert len(default.json()["paths"]) == 6


def test_audience_inherited_from_routers_mounted_on_app() -> None:
    @get("/report", sync_to_thread=False)
    def internal_handler() -> InternalReport: ...

    @get("/report", sync_to_thread=False)
    def partner_handler() -> PartnerReport: ...

    internal_router = Router("/internal", route_handlers=[internal_handler], audience="internal")
    partner_router = Router("/partner", route_handlers=[partner_handler], audience="partner")

    app = Litestar(
        route_handlers=[internal_router, partner_router],
        openapi_config=create_openapi_config(),
    )

    internal = get_schema(app, "internal")
    partner = get_schema(app, "partner")

    assert sorted(internal["paths"]) == ["/internal/report"]
    assert sorted(partner["paths"]) == ["/partner/report"]
    assert sorted(internal["components"]["schemas"]) == ["InternalReport"]
    assert sorted(partner["components"]["schemas"]) == ["PartnerReport"]


def test_audience_inherited_from_controller() -> None:
    class InternalController(Controller):
        path = "/internal"
        audience = "internal"

        @get("/report", sync_to_thread=False)
        def report(self) -> InternalReport: ...

    app = Litestar(route_handlers=[InternalController], openapi_config=create_openapi_config())

    assert sorted(get_schema(app, "internal")["paths"]) == ["/internal/report"]
    assert get_schema(app, "partner")["paths"] == {}


def test_audience_merges_additively_across_layers() -> None:
    @get("/report", audience=["handler-audience"], sync_to_thread=False)
    def handler() -> InternalReport: ...

    router = Router("/router", route_handlers=[handler], audience=["router-audience"])
    app = Litestar(
        route_handlers=[router],
        audience=["app-audience"],
        openapi_config=OpenAPIConfig(
            title="t",
            version="1",
            audiences=["handler-audience", "router-audience", "app-audience", "other"],
        ),
    )

    for audience in ("handler-audience", "router-audience", "app-audience"):
        assert sorted(get_schema(app, audience)["paths"]) == ["/router/report"]
    assert get_schema(app, "other")["paths"] == {}


def test_no_audiences_configured_serves_no_audience_endpoints() -> None:
    @get("/report", audience="internal", sync_to_thread=False)
    def handler() -> InternalReport: ...

    with create_test_client(
        route_handlers=[handler], openapi_config=OpenAPIConfig(title="t", version="1")
    ) as client:
        assert client.get("/schema/openapi.json").status_code == 200
        assert client.get("/schema/internal/openapi.json").status_code == 404
        # the default schema is unaffected by audience tags
        assert "/report" in client.get("/schema/openapi.json").json()["paths"]


def test_prune_unreferenced_components_keeps_transitively_reachable_entries() -> None:
    schema: dict[str, Any] = {
        "paths": {
            "/a": {
                "get": {
                    "security": [{"ApiKey": []}],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/A"},
                                    "examples": {"a": {"$ref": "#/components/examples/AExample"}},
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "object"},
                "C": {"type": "object"},
            },
            "securitySchemes": {
                "ApiKey": {"type": "apiKey"},
                "Bearer": {"type": "http"},
            },
            "examples": {
                "AExample": {"value": 1},
                "BExample": {"value": 2},
            },
        },
    }

    pruned = prune_unreferenced_components(schema)

    assert sorted(pruned["components"]["schemas"]) == ["A", "B"]
    assert sorted(pruned["components"]["securitySchemes"]) == ["ApiKey"]
    assert sorted(pruned["components"]["examples"]) == ["AExample"]


def test_prune_unreferenced_components_drops_empty_components() -> None:
    schema: dict[str, Any] = {"paths": {}, "components": {"schemas": {"A": {"type": "object"}}}}

    pruned = prune_unreferenced_components(schema)

    assert "components" not in pruned


def test_prune_unreferenced_components_without_components() -> None:
    schema: dict[str, Any] = {"paths": {}}
    assert prune_unreferenced_components(schema) is schema
    assert schema == {"paths": {}}


@pytest.mark.parametrize("audience", ["internal", "partner"])
def test_audience_schema_cached_and_invalidated(audience: str) -> None:
    app = create_app()
    plugin = app.plugins.get(OpenAPIPlugin)

    first = plugin.provide_openapi_schema(audience)
    assert plugin.provide_openapi_schema(audience) is first
    plugin.invalidate_schema_cache()
    second = plugin.provide_openapi_schema(audience)
    assert second is not first
    assert second == first

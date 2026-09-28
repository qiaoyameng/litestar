from __future__ import annotations

from typing import TYPE_CHECKING, Any

from litestar._openapi.datastructures import OpenAPIContext
from litestar._openapi.path_item import create_path_item_for_route, merge_path_item_operations, path_item_has_operations
from litestar._openapi.utils import prune_unreferenced_components
from litestar.constants import OPENAPI_JSON_HANDLER_NAME
from litestar.enums import MediaType
from litestar.exceptions import ImproperlyConfiguredException, NotFoundException
from litestar.handlers import get
from litestar.openapi.plugins import JsonRenderPlugin
from litestar.plugins import InitPlugin
from litestar.plugins.base import ReceiveRoutePlugin
from litestar.response import Response
from litestar.router import Router
from litestar.routes import HTTPRoute
from litestar.status_codes import HTTP_404_NOT_FOUND

if TYPE_CHECKING:
    from litestar.app import Litestar
    from litestar.config.app import AppConfig
    from litestar.connection import Request
    from litestar.handlers import HTTPRouteHandler
    from litestar.openapi.config import OpenAPIConfig
    from litestar.openapi.plugins import OpenAPIRenderPlugin
    from litestar.openapi.spec import OpenAPI, PathItem
    from litestar.params import FromPath
    from litestar.routes import BaseRoute


def handle_schema_path_not_found(path: FromPath[str] = "/") -> Response:
    """Handler for returning HTML formatted errors from not-found schema paths.

    This preserves backward compatibility with the Controller-based OpenAPI implementation.
    """
    if path.endswith((".json", ".yaml", ".yml")):
        raise NotFoundException

    content = b"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>404 Not found</title>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1">
        </head>
        <body>
            <h1>Error 404</h1>
        </body>
    </html>
    """
    return Response(content, media_type=MediaType.HTML, status_code=HTTP_404_NOT_FOUND)


class OpenAPIPlugin(InitPlugin, ReceiveRoutePlugin):
    __slots__ = (
        "_openapi",
        "_openapi_config",
        "_openapi_schema",
        "app",
        "included_routes",
    )

    def __init__(self, app: Litestar) -> None:
        self.app = app
        self.included_routes: dict[str, HTTPRoute] = {}
        self._openapi_config: OpenAPIConfig | None = None
        self._openapi: dict[str | None, OpenAPI] = {}
        self._openapi_schema: dict[str | None, dict[str, Any]] = {}

    def _build_openapi(self, audience: str | None = None) -> OpenAPI:
        openapi_config = self.openapi_config

        if openapi_config.create_examples:
            from litestar._openapi.schema_generation.examples import ExampleFactory

            ExampleFactory.seed_random(openapi_config.random_seed)

        openapi = openapi_config.to_openapi_schema()
        context = OpenAPIContext(openapi_config=openapi_config, plugins=self.app.plugins.openapi)
        path_items: dict[str, PathItem] = {}
        for route in self.included_routes.values():
            path = route.path_format or "/"
            path_item = create_path_item_for_route(context, route, audience=audience)
            if audience is not None and not path_item_has_operations(path_item):
                continue
            if existing_path_item := path_items.get(path):
                path_item = merge_path_item_operations(existing_path_item, path_item, for_path=path)
            path_items[path] = path_item

        openapi.paths = path_items
        openapi.components.schemas = context.schema_registry.generate_components_schemas()

        if audience is not None:
            # prune components that are only referenced by operations excluded for this audience
            self._apply_component_pruning(openapi, prune_unreferenced_components(openapi.to_schema()))

        return openapi

    @staticmethod
    def _apply_component_pruning(openapi: OpenAPI, pruned_schema: dict[str, Any]) -> None:
        """Align the components of an ``OpenAPI`` instance with a pruned serialized schema."""
        components = openapi.components
        pruned_components = pruned_schema.get("components", {})
        for field_name, section_name in (
            ("schemas", "schemas"),
            ("responses", "responses"),
            ("parameters", "parameters"),
            ("examples", "examples"),
            ("request_bodies", "requestBodies"),
            ("headers", "headers"),
            ("security_schemes", "securitySchemes"),
            ("links", "links"),
            ("callbacks", "callbacks"),
            ("path_items", "pathItems"),
        ):
            section = pruned_components.get(section_name) or {}
            if (entries := getattr(components, field_name)) is not None:
                retained = {k: v for k, v in entries.items() if k in section}
                setattr(components, field_name, retained if field_name == "schemas" else retained or None)

    def invalidate_schema_cache(self) -> None:
        if self._openapi_config is None:
            raise ImproperlyConfiguredException("OpenAPIConfig not initialized")
        self._openapi = {}
        self._openapi_schema = {}

    def provide_openapi(self, audience: str | None = None) -> OpenAPI:
        """Provide the OpenAPI schema for the given audience.

        Args:
            audience: If given, only include operations of route handlers tagged with this audience,
                and prune components that are not referenced by any of the included operations.

        Returns:
            An OpenAPI instance.
        """
        if audience not in self._openapi:
            self._openapi[audience] = self._build_openapi(audience=audience)
        return self._openapi[audience]

    def provide_openapi_schema(self, audience: str | None = None) -> dict[str, Any]:
        """Provide the serialized OpenAPI schema for the given audience.

        Args:
            audience: If given, only include operations of route handlers tagged with this audience,
                and prune components that are not referenced by any of the included operations.

        Returns:
            A serialized OpenAPI schema.
        """
        if audience not in self._openapi_schema:
            schema = self.provide_openapi(audience).to_schema()
            if audience is not None:
                # components of the OpenAPI instance are already pruned, so this only
                # removes component sections that were emptied by the pruning
                schema = prune_unreferenced_components(schema)
            self._openapi_schema[audience] = schema
        return self._openapi_schema[audience]

    def _create_audience_schema_handler(self, audience: str) -> HTTPRouteHandler:
        """Create a handler serving the OpenAPI schema filtered for the given audience."""

        @get(
            f"/{audience}/openapi.json",
            media_type=MediaType.JSON,
            sync_to_thread=False,
            name=f"openapi_json_{audience}",
        )
        def _handler(request: Request) -> dict[str, Any]:
            return self.provide_openapi_schema(audience)

        return _handler

    def create_openapi_router(self) -> Router:
        """Create a router for serving OpenAPI documentation and schema files.

        For each OpenAPI render plugin, a route is created to serve the plugin's
        documentation site.

        A handler is added for serving a 404 page for any schema path that is not
        configured by a plugin.

        A handler is added for serving the JSON OpenAPI schema file if it is not configured.

        For each plugin, the plugin's `receive_router` method is called with the router
        instance.

        Returns:
            The router.
        """
        if (router := self.openapi_config.openapi_router) is None:
            router = Router(
                self.openapi_config.path or "/schema",
                route_handlers=[],
                include_in_schema=False,
                dto=None,
                return_dto=None,
            )

        root_configured = False
        openapi_json_found = False

        def create_handler(plugin_: OpenAPIRenderPlugin) -> HTTPRouteHandler:
            """Create a handler for serving the plugin's documentation site.

            If the plugin is the default plugin, a handler is created for the root path in addition
            to the plugin's configured paths.

            If the plugin has a path for serving the OpenAPI schema file, the `openapi_json_found`
            flag is set to `True`, so that we don't create a handler for serving the JSON schema file.

            Args:
                plugin_: The plugin to create the handler for.

            Returns:
                The handler.
            """
            paths = list(plugin_.paths)
            if plugin_ is self.openapi_config.default_plugin:
                if not plugin_.has_path("/"):
                    paths.append("/")
                nonlocal root_configured
                root_configured = True

            handler_name = None
            if plugin_.has_path("/openapi.json"):
                nonlocal openapi_json_found
                openapi_json_found = True
                handler_name = OPENAPI_JSON_HANDLER_NAME

            @get(paths, media_type=plugin_.media_type, sync_to_thread=False, name=handler_name)
            def _handler(request: Request) -> bytes:
                return plugin_.render(request, self.provide_openapi_schema())

            return _handler

        for plugin in self.openapi_config.render_plugins:
            router.register(create_handler(plugin))

        not_found_handler_paths = ["/{path:str}"]
        if not root_configured:
            not_found_handler_paths.append("/")

        not_found_handler = get(not_found_handler_paths, media_type=MediaType.HTML, sync_to_thread=False)(
            handle_schema_path_not_found
        )
        router.register(not_found_handler)

        if not openapi_json_found:
            router.register(create_handler(JsonRenderPlugin()))

        for audience in self.openapi_config.audiences or ():
            router.register(self._create_audience_schema_handler(audience))

        for plugin in self.openapi_config.render_plugins:
            plugin.receive_router(router)

        return router

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        if app_config.openapi_config:
            self._openapi_config = app_config.openapi_config
            app_config.route_handlers.append(self.create_openapi_router())
        return app_config

    @property
    def openapi_config(self) -> OpenAPIConfig:
        if not self._openapi_config:
            raise ImproperlyConfiguredException("OpenAPIConfig not initialized")
        return self._openapi_config

    def receive_route(self, route: BaseRoute) -> None:
        if not isinstance(route, HTTPRoute):
            return

        if any(route_handler.include_in_schema for route_handler in route.route_handler_map.values()):
            # Force recompute the schema if a new route is added
            self._openapi = {}
            self._openapi_schema = {}
            self.included_routes[route.path] = route

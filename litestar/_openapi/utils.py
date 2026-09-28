from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from litestar.types.internal_types import PathParameterDefinition

if TYPE_CHECKING:
    from litestar.handlers.http_handlers import HTTPRouteHandler
    from litestar.types import Method


__all__ = ("SEPARATORS_CLEANUP_PATTERN", "default_operation_id_creator", "prune_unreferenced_components")

SEPARATORS_CLEANUP_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~:]+")


def default_operation_id_creator(
    route_handler: HTTPRouteHandler,
    http_method: Method,
    path_components: list[str | PathParameterDefinition],
) -> str:
    """Create a unique 'operationId' for an OpenAPI PathItem entry.

    Args:
        route_handler: The HTTP Route Handler instance.
        http_method: The HTTP method for the given PathItem.
        path_components: A list of path components.

    Returns:
        A camelCased operationId created from the handler function name,
        http method and path components.
    """

    handler_namespace = (
        http_method.title() + route_handler.handler_name.title()
        if len(route_handler.http_methods) > 1
        else route_handler.handler_name.title()
    )

    components_namespace = ""
    for component in (c.name if isinstance(c, PathParameterDefinition) else c for c in path_components):
        if component.title() not in components_namespace:
            components_namespace += component.title()

    return SEPARATORS_CLEANUP_PATTERN.sub("", components_namespace + handler_namespace)


_COMPONENTS_REF_PREFIX = "#/components/"


def _collect_component_references(value: Any, references: set[str], security_scheme_names: set[str]) -> None:
    """Collect ``#/components/...`` references and security scheme names from a serialized schema fragment."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith(_COMPONENTS_REF_PREFIX):
                references.add(item)
            elif key == "security" and isinstance(item, list):
                for requirement in item:
                    if isinstance(requirement, dict):
                        security_scheme_names.update(requirement)
            else:
                _collect_component_references(item, references, security_scheme_names)
    elif isinstance(value, list):
        for item in value:
            _collect_component_references(item, references, security_scheme_names)


def prune_unreferenced_components(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove component entries that are not referenced from the document.

    Components are retained if they are reachable - directly or transitively through other retained
    components - from any part of the document outside of ``components`` itself. Security schemes are
    retained if they are named by a ``security`` requirement.

    Args:
        schema: A serialized OpenAPI schema. Modified in place.

    Returns:
        The modified schema.
    """
    components = schema.get("components")
    if not isinstance(components, dict) or not components:
        return schema

    reachable_refs: set[str] = set()
    security_scheme_names: set[str] = set()
    for key, value in schema.items():
        if key != "components":
            _collect_component_references(value, reachable_refs, security_scheme_names)

    processed_refs: set[str] = set()
    processed_security_names: set[str] = set()
    pending_refs = list(reachable_refs)

    def queue_security_scheme_refs() -> None:
        security_schemes = components.get("securitySchemes")
        if isinstance(security_schemes, dict):
            for scheme_name in security_scheme_names - processed_security_names:
                if scheme_name in security_schemes:
                    pending_refs.append(f"{_COMPONENTS_REF_PREFIX}securitySchemes/{scheme_name}")
        processed_security_names.update(security_scheme_names)

    queue_security_scheme_refs()
    while pending_refs:
        ref = pending_refs.pop()
        if ref in processed_refs:
            continue
        processed_refs.add(ref)
        reachable_refs.add(ref)
        section, _, name = ref[len(_COMPONENTS_REF_PREFIX) :].partition("/")
        entry = components.get(section, {}).get(name) if isinstance(components.get(section), dict) else None
        if entry is None:
            continue
        nested_refs: set[str] = set()
        _collect_component_references(entry, nested_refs, security_scheme_names)
        pending_refs.extend(nested_refs - processed_refs)
        queue_security_scheme_refs()

    reachable_components: dict[str, set[str]] = {}
    for ref in reachable_refs:
        section, _, name = ref[len(_COMPONENTS_REF_PREFIX) :].partition("/")
        reachable_components.setdefault(section, set()).add(name)

    for section in list(components):
        entries = components[section]
        if not isinstance(entries, dict):
            continue
        retained = {name: value for name, value in entries.items() if name in reachable_components.get(section, set())}
        if retained:
            components[section] = retained
        else:
            del components[section]

    if not components:
        del schema["components"]

    return schema

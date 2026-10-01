"""Tests for positional-only parameters in handlers and dependencies.

External names (the python parameter name and any parameter alias) keep working for
annotation based validation, defaults and error reporting; the values are only turned
into positional arguments when the underlying callable is actually invoked.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from functools import partial
from typing import Annotated, Any

import pytest

from litestar import Litestar, get
from litestar.di import Provide
from litestar.params import Parameter
from litestar.testing import create_test_client


def test_to_call_args_splits_positional_only_parameters() -> None:
    from litestar.utils.signature import ParsedSignature

    def fn(a: int, /, b: str, *, c: bool) -> None: ...

    parsed_signature = ParsedSignature.from_fn(fn, {})
    args, kwargs = parsed_signature.to_call_args({"a": 1, "b": "x", "c": True})

    assert args == (1,)
    assert kwargs == {"b": "x", "c": True}


def test_positional_only_provider_is_called_positionally() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def provider(key: str, /, *, region: str) -> dict[str, str]:
        calls.append((key, region))
        return {"key": key, "region": region}

    @get(
        "/",
        dependencies={"config": Provide(provider, sync_to_thread=False)},
    )
    async def handler(config: dict[str, str], key: str, region: str) -> dict[str, str]:
        return {"config": config, "key": key, "region": region}

    with create_test_client([handler]) as client:
        response = client.get("/?key=abc&region=eu")

    assert response.status_code == 200
    assert response.json() == {
        "config": {"key": "abc", "region": "eu"},
        "key": "abc",
        "region": "eu",
    }
    assert calls == [("abc", "eu")]


def test_positional_only_bound_method_provider() -> None:
    class ConfigClient:
        def load(self, key: str, /, *, region: str) -> dict[str, str]:
            return {"bound": True, "key": key, "region": region}

    instance = ConfigClient()

    @get(
        "/",
        dependencies={"config": Provide(instance.load, sync_to_thread=False)},
    )
    async def handler(config: dict[str, str], key: str, region: str) -> dict[str, Any]:
        return {"config": config}

    with create_test_client([handler]) as client:
        response = client.get("/?key=abc&region=eu")

    assert response.status_code == 200
    assert response.json() == {"config": {"bound": True, "key": "abc", "region": "eu"}}


def test_positional_only_partial_provider() -> None:
    def provider(key: str, /, *, region: str) -> dict[str, str]:
        return {"key": key, "region": region}

    # the positional-only parameter is bound by the partial and must not be
    # treated as a value that still needs to be injected
    provider_partial = partial(provider, "bound-key")

    @get(
        "/",
        dependencies={"config": Provide(provider_partial, sync_to_thread=False)},
    )
    async def handler(config: dict[str, str], region: str) -> dict[str, Any]:
        return {"config": config}

    with create_test_client([handler]) as client:
        response = client.get("/?region=eu")

    assert response.status_code == 200
    assert response.json() == {"config": {"key": "bound-key", "region": "eu"}}


def test_partial_bound_keyword_argument_provider() -> None:
    def provider(key: str, /, *, region: str) -> dict[str, str]:
        return {"key": key, "region": region}

    # binding the keyword-only argument leaves the positional-only one to inject
    provider_partial = partial(provider, region="us")

    @get(
        "/",
        dependencies={"config": Provide(provider_partial, sync_to_thread=False)},
    )
    async def handler(config: dict[str, str], key: str) -> dict[str, Any]:
        return {"config": config}

    with create_test_client([handler]) as client:
        response = client.get("/?key=abc")

    assert response.status_code == 200
    assert response.json() == {"config": {"key": "abc", "region": "us"}}


def test_sync_generator_provider_with_positional_only_parameter() -> None:
    entered: list[int] = []
    exited: list[int] = []

    def generator_provider(value: int, /) -> Generator[int, None, None]:
        entered.append(value)
        try:
            yield value * 2
        finally:
            exited.append(value)

    @get(
        "/",
        dependencies={"doubled": Provide(generator_provider)},
    )
    async def handler(doubled: int, value: int) -> dict[str, int]:
        return {"doubled": doubled}

    with create_test_client([handler]) as client:
        response = client.get("/?value=21")

    assert response.status_code == 200
    assert response.json() == {"doubled": 42}
    assert entered == [21]
    assert exited == [21]


def test_async_generator_provider_with_positional_only_parameter() -> None:
    entered: list[int] = []
    exited: list[int] = []

    async def generator_provider(value: int, /) -> AsyncGenerator[int, None]:
        entered.append(value)
        try:
            yield value * 2
        finally:
            exited.append(value)

    @get(
        "/",
        dependencies={"doubled": Provide(generator_provider)},
    )
    async def handler(doubled: int, value: int) -> dict[str, int]:
        return {"doubled": doubled}

    with create_test_client([handler]) as client:
        response = client.get("/?value=21")

    assert response.status_code == 200
    assert response.json() == {"doubled": 42}
    assert entered == [21]
    assert exited == [21]


def test_handler_positional_only_alias_default_and_validation() -> None:
    @get("/")
    async def handler(
        count: Annotated[int, Parameter(query="total", ge=0)],
        /,
        label: str = "default-label",
    ) -> dict[str, Any]:
        return {"count": count, "label": label}

    with create_test_client([handler]) as client:
        assert client.get("/?total=3").json() == {"count": 3, "label": "default-label"}
        assert client.get("/?total=3&label=x").json() == {"count": 3, "label": "x"}

        bad_type = client.get("/?total=nope")
        assert bad_type.status_code == 400
        error = bad_type.json()["extra"][0]
        assert error["key"] == "count"
        assert error["source"] == "query"

        constraint = client.get("/?total=-1")
        assert constraint.status_code == 400
        assert constraint.json()["extra"][0]["key"] == "count"


def test_downstream_provider_not_called_when_validation_fails() -> None:
    upstream_entered: list[None] = []
    upstream_exited: list[None] = []
    downstream_calls: list[tuple[Any, Any]] = []

    def upstream() -> Generator[None, None, None]:
        upstream_entered.append(None)
        try:
            yield None
        finally:
            upstream_exited.append(None)

    def downstream(_: None, /, *, number: int) -> int:
        downstream_calls.append((_, number))
        return number

    @get(
        "/",
        dependencies={
            "upstream_value": Provide(upstream),
            "downstream_value": Provide(downstream, sync_to_thread=False),
        },
    )
    async def handler(upstream_value: None, downstream_value: int) -> dict[str, int]:
        return {"downstream_value": downstream_value}

    with create_test_client([handler]) as client:
        response = client.get("/?number=not-an-int")

    assert response.status_code == 400
    assert downstream_calls == []
    # the already-entered upstream generator is shut down cleanly
    assert upstream_entered == [None]
    assert upstream_exited == [None]


def test_app_builds_with_positional_only_partial_handler() -> None:
    def handler_impl(message: str, /, *, suffix: str = "") -> dict[str, str]:
        return {"message": message + suffix}

    handler = get("/")(partial(handler_impl, suffix="!"))

    with create_test_client([handler]) as client:
        response = client.get("/?message=hello")

    assert response.status_code == 200
    assert response.json() == {"message": "hello!"}


def test_partial_generator_dependency_detection() -> None:
    from collections.abc import AsyncGenerator as AsyncGen
    from collections.abc import Generator as Gen

    from litestar.di import Provide as DepProvide

    def sync_generator(value: int, /) -> Gen[int, None]:
        yield value

    async def async_generator(value: int, /) -> AsyncGen[int, None]:
        yield value

    sync_provider = DepProvide(partial(sync_generator, 1), sync_to_thread=None)
    async_provider = DepProvide(partial(async_generator, 1), sync_to_thread=None)

    assert sync_provider.has_sync_generator_dependency is True
    assert async_provider.has_async_generator_dependency is True


def test_partial_excludes_positional_arguments_from_signature() -> None:
    from litestar.utils.signature import ParsedSignature

    def provider(key: str, /, *, region: str) -> dict[str, str]:
        return {"key": key, "region": region}

    bound = ParsedSignature.from_fn(partial(provider, "bound-key"), {})
    assert set(bound.parameters) == {"region"}
    assert bound.positional_only_parameters == ()

    unbound = ParsedSignature.from_fn(partial(provider), {})
    assert set(unbound.parameters) == {"key", "region"}
    assert unbound.positional_only_parameters == ("key",)

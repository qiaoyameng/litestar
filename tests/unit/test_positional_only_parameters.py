"""Tests for positional-only parameter support in handlers and dependencies.

Externally, parameters are still addressed by their declared field name (annotations,
defaults, aliases and validation errors), while at call time positional-only parameters are
passed positionally.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from functools import partial
from typing import Annotated

import pytest

from litestar import Litestar, get
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery, QueryParameter
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient


async def test_positional_only_dependency_parameter() -> None:
    def load(key: FromQuery[str], /, *, region: FromQuery[str] = "cn") -> str:
        return f"{key}:{region}"

    @get("/", dependencies={"thing": Provide(load, sync_to_thread=False)})
    async def handler(thing: NamedDependency[str]) -> dict:
        return {"thing": thing}

    async with AsyncTestClient(app=Litestar([handler])) as client:
        response = await client.get("/?key=hello&region=eu")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"thing": "hello:eu"}


async def test_positional_only_handler_parameter() -> None:
    @get("/")
    async def handler(q: FromQuery[int], /, flag: FromQuery[bool] = True) -> dict:
        return {"q": q, "flag": flag}

    async with AsyncTestClient(app=Litestar([handler])) as client:
        response = await client.get("/?q=5")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"q": 5, "flag": True}


async def test_positional_only_parameter_alias() -> None:
    @get("/")
    async def handler(region: Annotated[str, QueryParameter(name="r")] = "cn", /) -> dict:
        return {"region": region}

    async with AsyncTestClient(app=Litestar([handler])) as client:
        response = await client.get("/?r=eu")
        assert response.status_code == HTTP_200_OK
        assert response.json() == {"region": "eu"}

        response = await client.get("/")
        assert response.status_code == HTTP_200_OK
        assert response.json() == {"region": "cn"}


async def test_positional_only_validation_error_uses_external_name() -> None:
    @get("/")
    async def handler(q: FromQuery[int], /) -> dict:
        return {"q": q}

    async with AsyncTestClient(app=Litestar([handler])) as client:
        missing = await client.get("/")
        invalid = await client.get("/?q=not-an-int")

    assert missing.status_code == HTTP_400_BAD_REQUEST
    assert "q" in missing.text
    assert invalid.status_code == HTTP_400_BAD_REQUEST
    extra = invalid.json()["extra"]
    assert extra[0]["key"] == "q"
    assert extra[0]["source"] == "query"


async def test_positional_only_bound_method() -> None:
    class Client:
        def __init__(self, base: str) -> None:
            self.base = base

        def load(self, key: FromQuery[str], /, *, region: FromQuery[str] = "cn") -> str:
            return f"{key}:{region}:{self.base}"

    client = Client(base="instance")

    @get("/", dependencies={"thing": Provide(client.load, sync_to_thread=False)})
    async def handler(thing: NamedDependency[str]) -> dict:
        return {"thing": thing}

    async with AsyncTestClient(app=Litestar([handler])) as c:
        response = await c.get("/?key=k&region=r")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"thing": "k:r:instance"}


async def test_positional_only_partial_bound_positional() -> None:
    def loader(base: str, key: FromQuery[str], /, *, region: FromQuery[str] = "cn") -> str:
        return f"{base}:{key}:{region}"

    provider = Provide(partial(loader, "bound"), sync_to_thread=False)

    @get("/", dependencies={"thing": provider})
    async def handler(thing: NamedDependency[str]) -> dict:
        return {"thing": thing}

    async with AsyncTestClient(app=Litestar([handler])) as c:
        response = await c.get("/?key=k&region=r")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"thing": "bound:k:r"}


def sync_generator_loader(key: FromQuery[str], /) -> Generator[str, None, None]:
    yield f"gen:{key}"


async def test_positional_only_sync_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITESTAR_WARN_SYNC_TO_THREAD_WITH_GENERATOR", "0")
    provider = Provide(sync_generator_loader, sync_to_thread=False)

    @get("/", dependencies={"thing": provider})
    async def handler(thing: NamedDependency[str]) -> dict:
        return {"thing": thing}

    async with AsyncTestClient(app=Litestar([handler])) as c:
        response = await c.get("/?key=k")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"thing": "gen:k"}


async def test_positional_only_async_generator() -> None:
    async def loader(key: FromQuery[str], /) -> AsyncGenerator[str]:
        yield f"agen:{key}"

    @get("/", dependencies={"thing": Provide(loader)})
    async def handler(thing: NamedDependency[str]) -> dict:
        return {"thing": thing}

    async with AsyncTestClient(app=Litestar([handler])) as c:
        response = await c.get("/?key=k")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"thing": "agen:k"}


async def test_validation_failure_does_not_execute_dependencies() -> None:
    events: list[str] = []

    async def leaf(x: FromQuery[int], /) -> int:
        events.append("leaf")
        return x

    async def parent(leaf: NamedDependency[int], /) -> int:
        events.append("parent")
        return leaf + 1

    @get("/", dependencies={"leaf": Provide(leaf), "parent": Provide(parent)})
    async def handler(parent: NamedDependency[int], extra: FromQuery[int], /) -> dict:
        return {"parent": parent}

    async with AsyncTestClient(app=Litestar([handler])) as c:
        response = await c.get("/?x=not-an-int&extra=1")

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert events == []


async def test_runtime_failure_closes_entered_generators() -> None:
    events: list[str] = []

    async def generator_dep(g: FromQuery[str], /) -> AsyncGenerator[str]:
        events.append("enter")
        try:
            yield g
        finally:
            events.append("exit")

    async def failing_dep(q: FromQuery[str], /) -> str:
        events.append("fail")
        raise RuntimeError("boom")

    @get("/", dependencies={"gen": Provide(generator_dep), "fail": Provide(failing_dep)})
    async def handler(gen: NamedDependency[str], fail: NamedDependency[str]) -> dict:
        return {"gen": gen}

    async with AsyncTestClient(app=Litestar([handler])) as c:
        response = await c.get("/?g=g&q=q")

    assert response.status_code == 500
    assert "enter" in events
    assert events.index("enter") < events.index("exit")


async def test_positional_only_websocket_handler_and_dependency() -> None:
    from litestar import websocket as websocket_decorator
    from litestar.connection import WebSocket

    async def dep(q: FromQuery[int], /) -> int:
        return q + 1

    @websocket_decorator("/ws/{item:str}", dependencies={"d": Provide(dep)})
    async def ws_handler(
        socket: WebSocket[dict, dict, dict], item: FromPath[str], d: NamedDependency[int], /
    ) -> None:
        await socket.accept()
        await socket.send_json({"item": item, "d": d})
        await socket.close()

    async with AsyncTestClient(app=Litestar([ws_handler])) as client:
        async with await client.websocket_connect("/ws/hello?q=10") as ws:
            assert await ws.receive_json() == {"item": "hello", "d": 11}

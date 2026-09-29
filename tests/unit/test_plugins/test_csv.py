from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from litestar import Litestar, Request, post
from litestar.di import NamedDependency
from litestar.dto import DataclassDTO, DTOConfig
from litestar.plugins.csv import (
    CSVConfig,
    CSVDuplicateHeaderError,
    CSVError,
    CSVHandler,
    CSVPlugin,
    CSVRecordError,
)
from litestar.response import Response
from litestar.testing import AsyncTestClient


@dataclass
class Item:
    name: str
    unit_price: float
    note: str | None = None
    secret: str = field(default="hidden")


class ItemDTO(DataclassDTO[Item]):
    config = DTOConfig(exclude={"secret"}, rename_strategy="camel")


csv_handler = CSVHandler()


@post("/items", dto=ItemDTO, return_dto=ItemDTO)
async def bulk_items(data: list[Item], request: Request, csv_handler: NamedDependency[CSVHandler]) -> Response[bytes]:
    return csv_handler.encode_response(data, ItemDTO(request))


@post("/item", dto=ItemDTO, return_dto=ItemDTO)
async def single_item(data: Item, request: Request, csv_handler: NamedDependency[CSVHandler]) -> Response[bytes]:
    return csv_handler.encode_response(data, ItemDTO(request))


def create_app(handler: CSVHandler | None = None) -> Litestar:
    return Litestar(
        route_handlers=[bulk_items, single_item],
        plugins=[CSVPlugin(handler or CSVHandler())],
        debug=True,
    )


def test_config_validates_single_characters() -> None:
    with pytest.raises(ValueError, match="delimiter"):
        CSVConfig(delimiter=";;")
    with pytest.raises(ValueError, match="quotechar"):
        CSVConfig(quotechar="^^")


def test_parse_quoted_newline_is_single_record() -> None:
    handler = CSVHandler()
    records = handler.parse('name,note\n"widget\npro",hello\ngadget,world\n')
    assert records == [
        {"name": "widget\npro", "note": "hello"},
        {"name": "gadget", "note": "world"},
    ]


def test_parse_duplicate_header() -> None:
    handler = CSVHandler()
    with pytest.raises(CSVDuplicateHeaderError) as exc_info:
        handler.parse("name,note,name\na,b,c\n")
    assert exc_info.value.column == "name"
    assert exc_info.value.status_code == 400


def test_parse_custom_delimiter() -> None:
    handler = CSVHandler(CSVConfig(delimiter=";"))
    records = handler.parse("name;note\nwidget;hello\n")
    assert records == [{"name": "widget", "note": "hello"}]


def test_parse_distinguishes_missing_empty_and_null() -> None:
    handler = CSVHandler()
    records = handler.parse("name,note\nwidget,\n")
    # "unitPrice" is missing from the header, "note" is an empty string
    assert records == [{"name": "widget", "note": ""}]
    assert "unitPrice" not in records[0]

    records = handler.parse("name,note\nwidget,null\n")
    assert records == [{"name": "widget", "note": None}]


def test_parse_short_row_treats_missing_cells_as_missing() -> None:
    handler = CSVHandler()
    records = handler.parse("name,unitPrice,note\nwidget\n")
    assert records == [{"name": "widget"}]


def test_parse_too_many_cells() -> None:
    handler = CSVHandler()
    with pytest.raises(CSVRecordError) as exc_info:
        handler.parse('name,note\n"wide\nget",hello,extra\n')
    # the multi-line field above is a single logical record
    assert exc_info.value.record == 1


def test_parse_empty_body() -> None:
    handler = CSVHandler()
    assert handler.parse(b"") == []
    assert handler.parse("") == []
    assert handler.parse("   \n") == []


def test_parse_malformed_csv() -> None:
    handler = CSVHandler()
    with pytest.raises(CSVError, match="malformed CSV"):
        handler.parse('name,note\n"unterminated,hello\n')


async def test_round_trip() -> None:
    async with AsyncTestClient(create_app()) as client:
        body = 'name,unitPrice,note\n"widget\npro",9.99,null\ngadget,1.5,\n'
        response = await client.post("/items", content=body, headers={"content-type": "text/csv"})

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        # column names are the DTO's external names, the excluded field is not leaked
        assert "secret" not in response.text
        assert "hidden" not in response.text
        assert response.text == 'name,unitPrice,note\n"widget\npro",9.99,null\ngadget,1.5,\n'

        # the response body can be posted back as-is
        second = await client.post("/items", content=response.content, headers={"content-type": "text/csv"})
        assert second.status_code == 200
        assert second.content == response.content


async def test_middle_record_validation_failure() -> None:
    async with AsyncTestClient(create_app()) as client:
        body = (
            "name,unitPrice,note\n"
            '"wide\nget",1.0,x\n'  # record 1 spans two physical lines
            "bad,notafloat,y\n"  # record 2 fails validation
            'later,2.0,"multi\nline"\n'  # record 3 also spans two lines
        )
        response = await client.post("/items", content=body, headers={"content-type": "text/csv"})

        assert response.status_code == 400
        payload = response.json()
        assert payload["status_code"] == 400
        # the multi-line fields are not counted as extra records
        assert "record 2" in payload["detail"]
        assert "column 'unitPrice'" in payload["detail"]


async def test_duplicate_header_request() -> None:
    async with AsyncTestClient(create_app()) as client:
        response = await client.post(
            "/items",
            content="name,name,unitPrice\na,b,1.0\n",
            headers={"content-type": "text/csv"},
        )
        assert response.status_code == 400
        assert "column 'name'" in response.json()["detail"]


async def test_missing_optional_column_uses_default() -> None:
    async with AsyncTestClient(create_app()) as client:
        response = await client.post(
            "/items",
            content="name,unitPrice\nwidget,1.0\n",
            headers={"content-type": "text/csv"},
        )
        assert response.status_code == 200
        assert response.text == "name,unitPrice,note\nwidget,1.0,null\n"


async def test_missing_required_column_is_validation_error() -> None:
    async with AsyncTestClient(create_app()) as client:
        response = await client.post(
            "/items",
            content="name,note\nwidget,x\n",
            headers={"content-type": "text/csv"},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "record 1" in detail
        assert "column 'unitPrice'" in detail


async def test_custom_delimiter_round_trip() -> None:
    handler = CSVHandler(CSVConfig(delimiter=";"))
    async with AsyncTestClient(create_app(handler)) as client:
        response = await client.post(
            "/items",
            content="name;unitPrice;note\nwidget;1.5;null\n",
            headers={"content-type": "text/csv"},
        )
        assert response.status_code == 200
        assert response.text == "name;unitPrice;note\nwidget;1.5;null\n"


async def test_single_model_endpoint() -> None:
    async with AsyncTestClient(create_app()) as client:
        response = await client.post(
            "/item",
            content="name,unitPrice,note\nwidget,1.5,null\n",
            headers={"content-type": "text/csv"},
        )
        assert response.status_code == 200
        assert response.text == "name,unitPrice,note\nwidget,1.5,null\n"


async def test_json_still_works() -> None:
    async with AsyncTestClient(create_app()) as client:
        response = await client.post(
            "/items",
            json=[{"name": "widget", "unitPrice": 1.5, "note": None}],
        )
        assert response.status_code == 200
        assert response.text == "name,unitPrice,note\nwidget,1.5,null\n"

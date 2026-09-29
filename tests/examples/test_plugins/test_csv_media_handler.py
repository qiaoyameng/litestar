from __future__ import annotations

from docs.examples.plugins.csv_media_handler import app

from litestar.testing import AsyncTestClient


async def test_csv_media_handler_round_trip() -> None:
    async with AsyncTestClient(app) as client:
        # a quoted field may contain newlines and counts as a single record;
        # "null" decodes to None, an empty cell decodes to an empty string
        body = 'name,unitPrice,note\n"widget\npro",9.99,null\ngadget,1.5,\n'
        response = await client.post("/items", content=body, headers={"content-type": "text/csv"})

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        # the excluded "secret" field is not leaked
        assert "secret" not in response.text
        assert response.text == body


async def test_csv_media_handler_record_validation_failure() -> None:
    async with AsyncTestClient(app) as client:
        body = (
            "name,unitPrice,note\n"
            '"wide\nget",1.0,x\n'  # record 1 spans two physical lines
            "bad,notafloat,y\n"  # record 2 fails validation
            'later,2.0,"multi\nline"\n'
        )
        response = await client.post("/items", content=body, headers={"content-type": "text/csv"})

        assert response.status_code == 400
        assert response.json() == {
            "status_code": 400,
            "detail": "CSV error at record 2, column 'unitPrice': Expected `float`, got `str`",
        }

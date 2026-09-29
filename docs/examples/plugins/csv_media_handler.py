"""Read and write CSV request/response bodies with Litestar DTOs.

Registering :class:`CSVPlugin <litestar.plugins.csv.CSVPlugin>` on the application
does two things:

- requests with a ``text/csv`` content type are decoded by the plugin's
  :class:`CSVHandler <litestar.plugins.csv.CSVHandler>` for any route that uses a
  data DTO, and
- the handler is made available as a dependency, so routes can encode CSV
  responses with the same DTO rules.

Column names are the DTO's external (serialization) names. A column missing from
the header is treated as a missing value, an empty cell decodes to an empty
string, and a cell equal to a configured null marker (``"null"`` by default)
decodes to ``None``. Fields containing newlines must be quoted and count as a
single logical record.
"""

from dataclasses import dataclass, field

from litestar import Litestar, Request, post
from litestar.di import NamedDependency
from litestar.dto import DataclassDTO, DTOConfig
from litestar.plugins.csv import CSVHandler, CSVPlugin
from litestar.response import Response


@dataclass
class Item:
    name: str
    unit_price: float
    note: str | None = None
    secret: str = field(default="hidden")


class ItemDTO(DataclassDTO[Item]):
    config = DTOConfig(exclude={"secret"}, rename_strategy="camel")


@post("/items", dto=ItemDTO, return_dto=ItemDTO)
async def bulk_create_items(
    data: list[Item],
    request: Request,
    csv_handler: NamedDependency[CSVHandler],
) -> Response[bytes]:
    # ``data`` is decoded from the CSV request body by the registered handler.
    # ...
    # store the items, then return them as CSV, encoded with the same DTO rules:
    # excluded fields (``secret``) are not leaked, and column names are the DTO's
    # external names (``unitPrice``)
    return csv_handler.encode_response(data, ItemDTO(request))


app = Litestar(
    route_handlers=[bulk_create_items],
    plugins=[CSVPlugin(CSVHandler())],
)

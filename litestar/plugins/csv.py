# pyright: reportUnnecessaryTypeIgnoreComment=false

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import msgspec

from litestar.di import Provide
from litestar.exceptions import ClientException
from litestar.plugins.base import InitPlugin
from litestar.response import Response
from litestar.serialization import decode_json, encode_json
from litestar.typing import FieldDefinition

if TYPE_CHECKING:
    from litestar.config.app import AppConfig
    from litestar.connection import ASGIConnection
    from litestar.dto import AbstractDTO
    from litestar.dto._backend import DTOBackend

__all__ = (
    "CSVConfig",
    "CSVDuplicateHeaderError",
    "CSVError",
    "CSVHandler",
    "CSVPlugin",
    "CSVRecordError",
    "CSVValidationError",
)

_PATH_RECORD_RE = re.compile(r"\$\[(\d+)\]")
_PATH_COLUMN_RE = re.compile(r"\$\[\d+\]\.([A-Za-z0-9_]+)")
_FIELD_NAME_RE = re.compile(r"field `([A-Za-z0-9_]+)`")


@dataclass(frozen=True)
class CSVConfig:
    """Configuration for :class:`CSVHandler`."""

    delimiter: str = ","
    """Field delimiter. Must be a single character."""
    quotechar: str = '"'
    """Character used to quote fields, e.g. fields containing newlines or the delimiter."""
    encoding: str = "utf-8"
    """Text encoding of request bodies and responses."""
    null_markers: tuple[str, ...] = ("null",)
    """Cell values that decode to ``None``. The first marker is used to encode ``None``."""
    lineterminator: str = "\n"
    """Line terminator used when encoding records."""

    def __post_init__(self) -> None:
        for name in ("delimiter", "quotechar"):
            if len(getattr(self, name)) != 1:
                raise ValueError(f"'{name}' must be a single character")


class CSVError(ClientException):
    """Base exception for CSV media handling errors.

    Carries the logical record number (1-based, excluding the header) and the column
    (DTO external field name) that caused the error, when known.
    """

    def __init__(
        self,
        detail: str,
        *,
        record: int | None = None,
        column: str | None = None,
    ) -> None:
        self.record = record
        self.column = column
        location = []
        if record is not None:
            location.append(f"record {record}")
        if column is not None:
            location.append(f"column {column!r}")
        super().__init__(detail=f"CSV error at {', '.join(location)}: {detail}" if location else f"CSV error: {detail}")


class CSVDuplicateHeaderError(CSVError):
    """Raised when the CSV header contains a duplicate column name."""

    def __init__(self, column: str) -> None:
        super().__init__("duplicate header column", column=column)


class CSVRecordError(CSVError):
    """Raised when a CSV record is structurally invalid, e.g. has too many cells."""


class CSVValidationError(CSVError):
    """Raised when a CSV record fails DTO validation."""


class CSVHandler:
    """Media handler that reads and writes ``text/csv`` bodies using Litestar DTOs.

    Column names are the DTO's external (serialization) names. Cell values follow
    these conventions:

    - a column missing from the header is omitted from the record, so DTO defaults
      apply (or a required-field validation error is raised),
    - an empty cell decodes to an empty string,
    - a cell equal to one of :attr:`CSVConfig.null_markers` decodes to ``None``.

    Fields containing newlines must be quoted; a quoted multi-line field counts as
    a single logical record.
    """

    media_type: ClassVar[str] = "text/csv"

    def __init__(self, config: CSVConfig | None = None) -> None:
        self.config = config or CSVConfig()

    def parse(self, body: bytes | str) -> list[dict[str, Any]]:
        """Parse a finite CSV body into a list of records keyed by column name.

        Args:
            body: The raw request body.

        Raises:
            CSVDuplicateHeaderError: If the header contains a duplicate column.
            CSVRecordError: If a record has more cells than the header.
            CSVError: If the body cannot be parsed as CSV.

        Returns:
            A list of records, one per logical CSV record.
        """
        text = body.decode(self.config.encoding) if isinstance(body, bytes) else body
        if not text.strip():
            return []

        reader = csv.reader(
            io.StringIO(text),
            delimiter=self.config.delimiter,
            quotechar=self.config.quotechar,
            strict=True,
        )
        try:
            rows = [row for row in reader if row]
        except csv.Error as exc:
            raise CSVError(f"malformed CSV near line {reader.line_num}: {exc}") from exc

        if not rows:
            return []

        header = rows[0]
        seen: set[str] = set()
        for name in header:
            if name in seen:
                raise CSVDuplicateHeaderError(name)
            seen.add(name)

        records: list[dict[str, Any]] = []
        for record_number, row in enumerate(rows[1:], start=1):
            if len(row) > len(header):
                raise CSVRecordError(
                    f"expected at most {len(header)} cells, got {len(row)}",
                    record=record_number,
                )
            record: dict[str, Any] = {}
            # a short row is allowed: missing cells are treated as missing values,
            # so ``strict=False`` is intentional here
            for name, value in zip(header, row, strict=False):
                record[name] = None if value in self.config.null_markers else value
            records.append(record)
        return records

    def decode_request_body(self, body: bytes, backend: DTOBackend, connection: ASGIConnection) -> Any:
        """Decode a request body into the DTO backend's transfer model type.

        Called by the DTO backend for requests whose content type is handled by this
        media handler.
        """
        records = self.parse(body)
        payload: Any = records if _expects_collection(backend) else (records[0] if records else {})
        try:
            return backend.parse_builtins(payload, connection)
        except msgspec.ValidationError as exc:
            raise self._to_validation_error(exc) from exc

    def decode(self, body: bytes | str, dto: AbstractDTO) -> Any:
        """Decode a CSV body into the DTO's model type.

        Args:
            body: The raw request body.
            dto: The DTO instance for the current connection, e.g. ``ItemDTO(request)``.

        Returns:
            The decoded and validated data.
        """
        backend = dto._dto_backends[dto.asgi_connection.route_handler.handler_id]["data_backend"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
        records = self.parse(body)
        payload: Any = records if _expects_collection(backend) else (records[0] if records else {})
        try:
            return backend.populate_data_from_builtins(payload, dto.asgi_connection)
        except msgspec.ValidationError as exc:
            raise self._to_validation_error(exc) from exc

    def encode(self, data: Any, dto: AbstractDTO) -> bytes:
        """Encode data into CSV bytes using the DTO's return rules.

        Excluded fields are not emitted, and column names are the DTO's external
        (serialization) names.

        Args:
            data: The data to encode, e.g. a list of model instances.
            dto: The return DTO instance for the current connection, e.g. ``ItemDTO(request)``.

        Returns:
            The CSV encoded body.
        """
        backend = self._return_backend(dto, is_collection=isinstance(data, (list, tuple, set, frozenset)))
        encodable = backend.encode_data(data)
        builtins = decode_json(encode_json(encodable))
        rows = builtins if isinstance(builtins, list) else [builtins]
        columns = list(backend.transfer_model_type.__struct_encode_fields__)

        buffer = io.StringIO()
        writer = csv.writer(
            buffer,
            delimiter=self.config.delimiter,
            quotechar=self.config.quotechar,
            lineterminator=self.config.lineterminator,
        )
        writer.writerow(columns)
        for row in rows:
            writer.writerow([self._encode_cell(row.get(column), column) for column in columns])
        return buffer.getvalue().encode(self.config.encoding)

    def encode_response(self, data: Any, dto: AbstractDTO, status_code: int = 200) -> Response[bytes]:
        """Encode data into a CSV :class:`Response <litestar.response.Response>`."""
        return Response(
            content=self.encode(data, dto),
            media_type=self.media_type,
            status_code=status_code,
        )

    @staticmethod
    def _return_backend(dto: AbstractDTO, is_collection: bool) -> DTOBackend:
        """Return the DTO backend used to encode response data, creating it on demand.

        The backend is keyed separately from the route handler's own DTO backends, so
        encoding works regardless of the handler's return annotation.
        """
        handler_id = dto.asgi_connection.route_handler.handler_id
        backend_key = f"{handler_id}::csv::{'collection' if is_collection else 'single'}"
        backend = dto._dto_backends.get(backend_key, {}).get("return_backend")
        if backend is None:
            annotation = list[dto.model_type] if is_collection else dto.model_type
            dto.create_for_field_definition(FieldDefinition.from_annotation(annotation), backend_key)
            backend = dto._dto_backends[backend_key]["return_backend"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
        return backend

    def _encode_cell(self, value: Any, column: str) -> str:
        if value is None:
            return self.config.null_markers[0] if self.config.null_markers else ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (dict, list)):
            raise CSVError("nested structures cannot be represented in CSV", column=column)
        return str(value)

    @staticmethod
    def _to_validation_error(exc: msgspec.ValidationError) -> CSVValidationError:
        """Locate a DTO validation failure to a logical record number and column."""
        message = str(exc)
        record_match = _PATH_RECORD_RE.search(message)
        record = int(record_match.group(1)) + 1 if record_match else None
        column_match = _PATH_COLUMN_RE.search(message) or _FIELD_NAME_RE.search(message)
        column = column_match.group(1) if column_match else None
        detail = message.split(" - at ", maxsplit=1)[0]
        return CSVValidationError(detail, record=record, column=column)


def _expects_collection(backend: DTOBackend) -> bool:
    """Return whether the handler's ``data`` annotation is a collection of models."""
    field_definition = backend.field_definition
    if backend.dto_data_type is not None:
        inner_types = field_definition.inner_types
        return bool(inner_types and inner_types[0].is_non_string_iterable)
    return field_definition.is_non_string_iterable


class CSVPlugin(InitPlugin):
    """Register a :class:`CSVHandler` on the application.

    Once registered, requests with a ``text/csv`` content type are decoded by the
    handler for any route that uses a data DTO, and the handler is available as a
    dependency for encoding responses.
    """

    def __init__(self, handler: CSVHandler | None = None, *, dependency_key: str = "csv_handler") -> None:
        self.handler = handler or CSVHandler()
        self.dependency_key = dependency_key
        self.request_media_handlers: dict[str, CSVHandler] = {self.handler.media_type: self.handler}

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        handler = self.handler

        def provide_csv_handler() -> CSVHandler:
            return handler

        app_config.dependencies[self.dependency_key] = Provide(provide_csv_handler, sync_to_thread=False)
        return app_config

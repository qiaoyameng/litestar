from __future__ import annotations

import logging
from functools import wraps
from inspect import Parameter, Signature
from typing import TYPE_CHECKING, Any

import anyio
from msgspec.json import Encoder as JsonEncoder

from litestar._kwargs import KwargsModel
from litestar.di import NamedDependency, Provide
from litestar.exceptions import WebSocketDisconnect
from litestar.serialization import decode_json
from litestar.types.builtin_types import NoneType
from litestar.utils import ensure_async_callable
from litestar.utils.helpers import unwrap_partial
from litestar.utils.signature import ParsedSignature, get_fn_type_hints

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from litestar import WebSocket
    from litestar.handlers.websocket_handlers.listener import WebsocketListenerRouteHandler
    from litestar.types import AnyCallable

logger = logging.getLogger("litestar.websocket.listener")


class MessageConcurrencyLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._condition = anyio.Condition()

    async def __aenter__(self) -> MessageConcurrencyLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.release_async()

    async def acquire(self) -> None:
        async with self._condition:
            while self._active >= self._limit:
                await self._condition.wait()
            self._active += 1

    async def release_async(self) -> None:
        async with self._condition:
            self._release()

    def _release(self) -> None:
        if self._active:
            self._active -= 1
        self._condition.notify_all()


def parse_received_data(
    listener: WebsocketListenerRouteHandler, socket: WebSocket, received_data: str | bytes
) -> Any:
    if data_dto := listener.data_dto:
        return data_dto(socket).decode_bytes(
            received_data.encode("utf-8") if isinstance(received_data, str) else received_data
        )

    data_field = listener.parsed_data_field
    if data_field and data_field.annotation is str:
        return received_data.decode("utf-8") if isinstance(received_data, bytes) else received_data

    if data_field and data_field.annotation is bytes:
        return received_data.encode("utf-8") if isinstance(received_data, str) else received_data

    return decode_json(value=received_data, type_decoders=socket.route_handler.type_decoders)


def create_handle_receive_raw(
    listener: WebsocketListenerRouteHandler,
) -> Callable[[WebSocket], Coroutine[Any, Any, str | bytes]]:
    async def handle_receive_raw(socket: WebSocket) -> str | bytes:
        return await socket.receive_data(mode=listener._receive_mode)

    return handle_receive_raw


def create_handle_receive(listener: WebsocketListenerRouteHandler) -> Callable[[WebSocket], Coroutine[Any, None, None]]:
    handle_receive_raw = create_handle_receive_raw(listener)

    async def handle_receive(socket: WebSocket) -> Any:
        return parse_received_data(listener, socket, await handle_receive_raw(socket))

    return handle_receive


def create_handle_send(
    listener: WebsocketListenerRouteHandler,
) -> Callable[[WebSocket, Any], Coroutine[None, None, None]]:
    json_encoder = JsonEncoder(enc_hook=listener.default_serializer)

    if return_dto := listener.return_dto:

        async def handle_send(socket: WebSocket, data: Any) -> None:
            encoded_data = return_dto(socket).data_to_encodable_type(data)
            data = json_encoder.encode(encoded_data)
            await socket.send_data(data=data, mode=listener._send_mode)

    elif listener.parsed_return_field.is_subclass_of((str, bytes)) or (
        listener.parsed_return_field.is_optional and listener.parsed_return_field.has_inner_subclass_of((str, bytes))
    ):

        async def handle_send(socket: WebSocket, data: Any) -> None:
            if data is not None:
                await socket.send_data(data=data, mode=listener._send_mode)

    else:

        async def handle_send(socket: WebSocket, data: Any) -> None:
            if data is not None:
                data = json_encoder.encode(data)
                await socket.send_data(data=data, mode=listener._send_mode)

    return handle_send


class ListenerHandler:
    __slots__ = (
        "_can_send_data",
        "_fn",
        "_listener",
        "_fn_type_hints",
        "_message_kwargs_model",
        "_message_signature_model",
        "_parsed_signature",
        "_pass_message_id",
        "_pass_socket",
        "_wrapped_fn",
    )

    def __init__(
        self,
        listener: WebsocketListenerRouteHandler,
        fn: AnyCallable,
        parsed_signature: ParsedSignature,
        namespace: dict[str, Any],
        fn_type_hints: dict[str, Any],
    ) -> None:
        self._can_send_data = not parsed_signature.return_type.is_subclass_of(NoneType)
        self._wrapped_fn = fn
        self._fn = ensure_async_callable(fn)
        self._listener = listener
        self._fn_type_hints = fn_type_hints
        self._parsed_signature = parsed_signature
        self._pass_socket = "socket" in parsed_signature.parameters
        self._pass_message_id = "message_id" in parsed_signature.parameters
        self._message_kwargs_model: KwargsModel | None = None
        self._message_signature_model: Any = None

    def initialize_message_scope(self) -> None:
        from litestar._signature import SignatureModel

        parsed_message_signature = ParsedSignature.from_signature(
            self._parsed_signature.original_signature,
            self._fn_type_hints,
        )
        self._message_signature_model = SignatureModel.create(
            dependency_name_set=set(self._listener.dependencies),
            fn=self._wrapped_fn,
            parsed_signature=parsed_message_signature,
            data_dto=self._listener.data_dto,
            type_decoders=self._listener.type_decoders,
        )
        self._message_kwargs_model = KwargsModel.create_for_signature_model(
            signature_model=self._message_signature_model,
            parsed_signature=parsed_message_signature,
            dependencies=self._listener.dependencies,
            path_parameters=self._listener.path_parameter_names,
            layered_parameters=self._listener.parameter_field_definitions,
            ctx=self._listener,
        )

    async def __call__(
        self,
        *args: Any,
        socket: WebSocket,
        connection_lifespan_dependencies: NamedDependency[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        lifespan_manager = self._listener._connection_lifespan or self._listener.default_connection_lifespan
        handle_send = self._listener.resolve_send_handler() if self._can_send_data else None
        handle_receive_raw = create_handle_receive_raw(self._listener)

        if self._pass_socket:
            kwargs["socket"] = socket

        async with lifespan_manager(**connection_lifespan_dependencies):
            await self._listen(
                *args,
                socket=socket,
                connection_kwargs=kwargs,
                handle_receive_raw=handle_receive_raw,
                handle_send=handle_send,
            )

    async def _listen(
        self,
        *args: Any,
        socket: WebSocket,
        connection_kwargs: dict[str, Any],
        handle_receive_raw: Callable[[WebSocket], Coroutine[Any, Any, str | bytes]],
        handle_send: Callable[[WebSocket, Any], Coroutine[None, None, None]] | None,
    ) -> None:
        if self._message_kwargs_model is None or self._message_signature_model is None:
            raise RuntimeError("Message scope has not been initialized")

        concurrency = self._listener.message_concurrency
        send_stream, receive_stream = anyio.create_memory_object_stream[str | bytes](concurrency)
        send_lock = anyio.Lock()
        capacity_limiter = MessageConcurrencyLimiter(concurrency)
        disconnected = anyio.Event()

        async def reader() -> None:
            async with send_stream:
                while True:
                    try:
                        await send_stream.send(await handle_receive_raw(socket))
                    except WebSocketDisconnect:
                        disconnected.set()
                        break

        async def worker() -> None:
            async with receive_stream:
                async for raw_data in receive_stream:
                    try:
                        await self._handle_message(
                            *args,
                            socket=socket,
                            raw_data=raw_data,
                            connection_kwargs=connection_kwargs,
                            handle_send=handle_send,
                            send_lock=send_lock,
                            capacity_limiter=capacity_limiter,
                        )
                    except Exception as exc:
                        logger.exception("Exception while handling WebSocket message", exc_info=exc)

        async def process_messages() -> None:
            with anyio.CancelScope() as cancel_scope:
                async def wait_for_disconnect() -> None:
                    await disconnected.wait()
                    cancel_scope.cancel()

                async with anyio.create_task_group() as worker_group:
                    worker_group.start_soon(wait_for_disconnect)
                    worker_group.start_soon(reader)
                    for _ in range(concurrency):
                        worker_group.start_soon(worker)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(process_messages)

    async def _handle_message(
        self,
        *args: Any,
        socket: WebSocket,
        raw_data: str | bytes,
        connection_kwargs: dict[str, Any],
        handle_send: Callable[[WebSocket, Any], Coroutine[None, None, None]] | None,
        send_lock: anyio.Lock,
        capacity_limiter: MessageConcurrencyLimiter,
    ) -> None:
        if self._message_kwargs_model is None or self._message_signature_model is None:
            raise RuntimeError("Message scope has not been initialized")

        data = parse_received_data(self._listener, socket, raw_data)
        message_id = get_message_id(data, self._listener.message_id_key)
        async with capacity_limiter:
            message_kwargs = dict(connection_kwargs)
            message_kwargs["data"] = data
            if self._pass_message_id:
                message_kwargs["message_id"] = message_id

            cleanup_group = await self._message_kwargs_model.resolve_dependencies(socket, message_kwargs)
            message_kwargs = self._message_signature_model.parse_values_from_connection_kwargs(
                connection=socket,
                kwargs=message_kwargs,
            )
            if not self._pass_socket:
                message_kwargs.pop("socket", None)

            async with cleanup_group:
                result = await self._fn(*args, **message_kwargs)
                if handle_send and result is not None:
                    result = add_message_id(result, message_id, self._listener.message_id_key)
                    async with send_lock:
                        await handle_send(socket, result)


def create_handler_signature(
    callback_signature: Signature,
    *,
    message_dependency_names: set[str] | None = None,
) -> Signature:
    """Creates a :class:`Signature` for the handler function for signature modelling.

    This is required for two reasons:

        1. the :class:`.handlers.WebsocketHandler` signature model cannot contain the ``data`` parameter, which is
            required for :class:`.handlers.websocket_listener` handlers.
        2. the :class;`.handlers.WebsocketHandler` signature model must include the ``socket`` parameter, which is
            optional for :class:`.handlers.websocket_listener` handlers.

    Args:
        callback_signature: The :class:`Signature` of the listener callback.

    Returns:
        The :class:`Signature` for the listener callback as required for signature modelling.
    """
    new_params = [
        parameter
        for parameter in callback_signature.parameters.values()
        if parameter.name not in {"data", "message_id", *(message_dependency_names or set())}
    ]
    if "socket" not in callback_signature.parameters:
        new_params.append(Parameter(name="socket", kind=Parameter.KEYWORD_ONLY, annotation="WebSocket"))

    new_params.append(
        Parameter(
            name="connection_lifespan_dependencies",
            kind=Parameter.KEYWORD_ONLY,
            annotation="NamedDependency[Dict[str, Any]]",
        )
    )

    return callback_signature.replace(parameters=new_params)


def create_stub_dependency(src: AnyCallable | None) -> Provide:
    """Create a stub dependency, accepting any kwargs defined in ``src``, and
    wrap it in ``Provide``
    """
    if src is None:

        async def empty_stub() -> None:
            return None

        return Provide(empty_stub)

    src = unwrap_partial(src)

    @wraps(src)
    async def stub(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return Provide(stub)
def get_message_id(data: Any, message_id_key: str | None) -> Any:
    if not message_id_key:
        return None
    if isinstance(data, dict):
        return data.get(message_id_key)
    return getattr(data, message_id_key, None)


def add_message_id(result: Any, message_id: Any, message_id_key: str | None) -> Any:
    if not message_id_key or message_id is None or not isinstance(result, dict) or message_id_key in result:
        return result
    return {message_id_key: message_id, **result}

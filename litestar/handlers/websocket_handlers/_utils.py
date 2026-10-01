from __future__ import annotations

import logging
from functools import wraps
from inspect import Parameter, Signature
from typing import TYPE_CHECKING, Any

import anyio
from msgspec.json import Encoder as JsonEncoder

from litestar.di import NamedDependency, Provide
from litestar.serialization import decode_json
from litestar.types.builtin_types import NoneType
from litestar.utils import ensure_async_callable
from litestar.utils.helpers import unwrap_partial

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from litestar import WebSocket
    from litestar._kwargs import KwargsModel
    from litestar.handlers.websocket_handlers.listener import WebsocketListenerRouteHandler
    from litestar.types import AnyCallable
    from litestar.utils.signature import ParsedSignature


__all__ = (
    "ListenerHandler",
    "create_handle_receive",
    "create_handle_send",
    "create_handler_signature",
    "create_message_handler_signature",
    "create_stub_dependency",
)


logger = logging.getLogger(__name__)


def create_handle_receive(listener: WebsocketListenerRouteHandler) -> Callable[[WebSocket], Coroutine[Any, None, None]]:
    message_id_key = listener.message_id_key

    if data_dto := listener.data_dto:

        async def handle_receive(socket: WebSocket) -> Any:
            received_data = await socket.receive_data(mode=listener._receive_mode)
            return data_dto(socket).decode_bytes(
                received_data.encode("utf-8") if isinstance(received_data, str) else received_data
            )

    elif listener.parsed_data_field and listener.parsed_data_field.annotation is str:

        async def handle_receive(socket: WebSocket) -> Any:
            received_data = await socket.receive_data(mode=listener._receive_mode)
            return received_data.decode("utf-8") if isinstance(received_data, bytes) else received_data

    elif listener.parsed_data_field and listener.parsed_data_field.annotation is bytes:

        async def handle_receive(socket: WebSocket) -> Any:
            received_data = await socket.receive_data(mode=listener._receive_mode)
            return received_data.encode("utf-8") if isinstance(received_data, str) else received_data

    else:

        async def handle_receive(socket: WebSocket) -> Any:
            received_data = await socket.receive_data(mode=listener._receive_mode)
            if isinstance(received_data, bytes):
                received_data = received_data.decode("utf-8")
            decoded = decode_json(value=received_data, type_decoders=socket.route_handler.type_decoders)
            if message_id_key and isinstance(decoded, dict) and message_id_key in decoded:
                message_id = decoded.pop(message_id_key)
                return message_id, decoded
            return None, decoded

    return handle_receive


def create_handle_send(
    listener: WebsocketListenerRouteHandler,
) -> Callable[[WebSocket, Any, Any], Coroutine[None, None, None]]:
    json_encoder = JsonEncoder(enc_hook=listener.default_serializer)
    message_id_key = listener.message_id_key

    def wrap_with_message_id(data: Any, message_id: Any) -> Any:
        if isinstance(data, dict):
            return {message_id_key: message_id, **data}
        return {message_id_key: message_id, "data": data}

    if return_dto := listener.return_dto:

        async def handle_send(socket: WebSocket, data: Any, message_id: Any = None) -> None:
            encoded_data = return_dto(socket).data_to_encodable_type(data)
            if message_id_key and message_id is not None:
                encoded_data = wrap_with_message_id(encoded_data, message_id)
            data = json_encoder.encode(encoded_data)
            await socket.send_data(data=data, mode=listener._send_mode)

    elif listener.parsed_return_field.is_subclass_of((str, bytes)) or (
        listener.parsed_return_field.is_optional and listener.parsed_return_field.has_inner_subclass_of((str, bytes))
    ):

        async def handle_send(socket: WebSocket, data: Any, message_id: Any = None) -> None:
            if data is not None:
                await socket.send_data(data=data, mode=listener._send_mode)

    else:

        async def handle_send(socket: WebSocket, data: Any, message_id: Any = None) -> None:
            if data is not None:
                if message_id_key and message_id is not None:
                    data = wrap_with_message_id(data, message_id)
                data = json_encoder.encode(data)
                await socket.send_data(data=data, mode=listener._send_mode)

    return handle_send


class ListenerHandler:
    __slots__ = ("_accepted_kwargs", "_can_send_data", "_fn", "_listener", "_pass_socket", "_wrapped_fn")

    def __init__(
        self,
        listener: WebsocketListenerRouteHandler,
        fn: AnyCallable,
        parsed_signature: ParsedSignature,
        namespace: dict[str, Any],
    ) -> None:
        self._can_send_data = not parsed_signature.return_type.is_subclass_of(NoneType)
        self._wrapped_fn = fn
        self._fn = ensure_async_callable(fn)
        self._listener = listener
        self._pass_socket = "socket" in parsed_signature.parameters
        self._accepted_kwargs = set(parsed_signature.parameters)

    async def __call__(
        self,
        *args: Any,
        socket: WebSocket,
        connection_lifespan_dependencies: NamedDependency[dict[str, Any]],
        **connection_kwargs: Any,
    ) -> None:
        listener = self._listener
        lifespan_manager = listener._connection_lifespan or listener.default_connection_lifespan
        handle_send = listener.resolve_send_handler() if self._can_send_data else None
        handle_receive = listener.resolve_receive_handler()
        message_kwargs_model: KwargsModel | None = listener._message_kwargs_model
        message_id_key = listener.message_id_key

        # connection scoped kwargs, resolved for the whole connection: everything
        # resolved by the connection kwargs model minus the internal stub values.
        connection_scope_kwargs = {
            key: value for key, value in connection_kwargs.items() if key not in listener._stub_dependency_keys
        }

        async with lifespan_manager(**connection_lifespan_dependencies):
            # The receive loop decouples reading from processing so that the connection can
            # be closed (and the in-flight messages cancelled) even while messages are being
            # processed. ``max_concurrency`` is enforced via a capacity limiter acquired
            # around each message; with the default of ``1`` messages are handled strictly
            # sequentially.
            message_send, message_receive = anyio.create_memory_object_stream(listener.max_concurrency)
            limiter = anyio.CapacityLimiter(listener.max_concurrency)

            async def process_message(message: Any) -> None:
                message_id = None
                payload = message
                if message_id_key:
                    message_id, payload = message

                async with limiter:
                    cleanup_group = None
                    message_kwargs = dict(connection_scope_kwargs)
                    try:
                        if message_kwargs_model is not None and message_kwargs_model.has_kwargs:
                            message_kwargs.update(await message_kwargs_model.to_kwargs(connection=socket))
                            if message_kwargs_model.dependency_batches:
                                cleanup_group = await message_kwargs_model.resolve_dependencies(
                                    connection=socket,
                                    kwargs=message_kwargs,
                                )
                        if self._pass_socket:
                            message_kwargs["socket"] = socket
                        handler_kwargs = {
                            key: value
                            for key, value in message_kwargs.items()
                            if key in self._accepted_kwargs
                        }
                        if cleanup_group is not None:
                            async with cleanup_group:
                                data = await self._fn(*args, data=payload, **handler_kwargs)
                        else:
                            data = await self._fn(*args, data=payload, **handler_kwargs)
                        if handle_send:
                            await handle_send(socket, data, message_id)
                    except anyio.get_cancelled_exc_class():
                        # the message was cancelled because the connection was closed; the
                        # ``async with cleanup_group`` above receives this cancellation and
                        # throws it into the message-scoped generator dependencies.
                        raise
                    except Exception:
                        # a failure in one message must not tear down the connection or affect
                        # other in-flight messages.
                        logger.exception("Exception while processing websocket listener message %r", message_id)

            async def receive_loop() -> None:
                async with message_send:
                    while True:
                        message = await handle_receive(socket)
                        await message_send.send(message)

            async def worker() -> None:
                async with message_receive:
                    async for message in message_receive:
                        task_group.start_soon(process_message, message)

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(worker)
                task_group.start_soon(receive_loop)


def create_handler_signature(callback_signature: Signature) -> Signature:
    """Creates a :class:`Signature` for the connection scope of the listener.

    This is required for two reasons:

        1. the :class:`.handlers.WebsocketHandler` signature model cannot contain the ``data`` parameter, which is
            required for :class:`.handlers.websocket_listener` handlers.
        2. the :class;`.handlers.WebsocketHandler` signature model must include the ``socket`` parameter, which is
            optional for :class:`.handlers.websocket_listener` handlers.

    Args:
        callback_signature: The :class:`Signature` of the listener callback.

    Returns:
        The :class:`Signature` for the handler callback as required by signature modelling.
    """
    new_params = [p for p in callback_signature.parameters.values() if p.name != "data"]
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


def create_message_handler_signature(callback_signature: Signature) -> Signature:
    """Create a :class:`Signature` for the per-message scope of a listener.

    Like :func:`create_handler_signature`, but without the internal connection-lifespan
    parameter: its fields represent the values that must be resolved independently for
    every received message.
    """
    new_params = [p for p in callback_signature.parameters.values() if p.name != "data"]
    if "socket" not in callback_signature.parameters:
        new_params.append(Parameter(name="socket", kind=Parameter.KEYWORD_ONLY, annotation="WebSocket"))

    return callback_signature.replace(parameters=new_params)


def create_connection_handler_signature(
    callback_signature: Signature,
    excluded_parameter_names: set[str],
) -> Signature:
    """Create a :class:`Signature` for the connection scope of a listener.

    Like :func:`create_handler_signature`, but parameters listed in
    ``excluded_parameter_names`` (the message-scoped dependencies) are dropped, so the
    connection scope neither resolves nor requires them.
    """
    new_params = [
        parameter
        for parameter in callback_signature.parameters.values()
        if parameter.name != "data" and parameter.name not in excluded_parameter_names
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

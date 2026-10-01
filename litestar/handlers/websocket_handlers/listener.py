from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    overload,
)

from litestar.connection import WebSocket
from litestar.di import NamedDependency
from litestar.exceptions import ImproperlyConfiguredException, WebSocketDisconnect
from litestar.types import (
    AnyCallable,
    Dependencies,
    Empty,
    EmptyType,
    ExceptionHandler,
    Guard,
    Middleware,
    TypeEncodersMap,
)
from litestar.utils import ensure_async_callable
from litestar.utils.signature import ParsedSignature, get_fn_type_hints

from ._utils import (
    ListenerHandler,
    create_connection_handler_signature,
    create_handle_receive,
    create_handle_send,
    create_handler_signature,
    create_message_handler_signature,
    create_stub_dependency,
)
from .route_handler import WebsocketRouteHandler

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping, Sequence

    from litestar import Litestar, Router
    from litestar._kwargs import KwargsModel
    from litestar.di import NamedDependency
    from litestar.dto import AbstractDTO
    from litestar.routes import BaseRoute
    from litestar.types.asgi_types import WebSocketMode
    from litestar.types.composite_types import ParametersMap, TypeDecodersSequence
    from litestar.utils.signature import ParsedSignature


__all__ = ("WebsocketListener", "WebsocketListenerRouteHandler", "websocket_listener")


class WebsocketListenerRouteHandler(WebsocketRouteHandler):
    """A websocket listener that automatically accepts a connection, handles disconnects,
    invokes a callback function every time new data is received and sends any data
    returned
    """

    __slots__ = {  # noqa: RUF023
        "connection_accept_handler": "Callback to accept a WebSocket connection. By default, calls WebSocket.accept",
        "max_concurrency": "Maximum number of messages processed concurrently within a single connection",
        "message_id_key": "Key used to extract a message identifier and correlate replies",
        "on_accept": "Callback invoked after a WebSocket connection has been accepted",
        "on_disconnect": "Callback invoked after a WebSocket connection has been closed",
        "_connection_lifespan": None,
        "_message_kwargs_model": None,
        "_receive_handler": None,
        "_receive_mode": None,
        "_send_handler": None,
        "_send_mode": None,
        "_stub_dependency_keys": None,
    }

    @overload
    def __init__(
        self,
        path: str | list[str] | None = None,
        *,
        fn: AnyCallable,
        connection_lifespan: Callable[..., AbstractAsyncContextManager[Any]] | None = None,
        dependencies: Dependencies | None = None,
        dto: type[AbstractDTO] | None | EmptyType = Empty,
        exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None,
        guards: Sequence[Guard] | None = None,
        max_concurrency: int = 1,
        message_id_key: str | None = None,
        middleware: Sequence[Middleware] | None = None,
        receive_mode: WebSocketMode = "text",
        send_mode: WebSocketMode = "text",
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        return_dto: type[AbstractDTO] | None | EmptyType = Empty,
        signature_namespace: Mapping[str, Any] | None = None,
        type_decoders: TypeDecodersSequence | None = None,
        type_encoders: TypeEncodersMap | None = None,
        websocket_class: type[WebSocket] | None = None,
        parameters: ParametersMap | None = None,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self,
        path: str | list[str] | None = None,
        *,
        fn: AnyCallable,
        connection_accept_handler: Callable[[WebSocket], Coroutine[Any, Any, None]] = WebSocket.accept,
        dependencies: Dependencies | None = None,
        dto: type[AbstractDTO] | None | EmptyType = Empty,
        exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None,
        guards: Sequence[Guard] | None = None,
        max_concurrency: int = 1,
        message_id_key: str | None = None,
        middleware: Sequence[Middleware] | None = None,
        receive_mode: WebSocketMode = "text",
        send_mode: WebSocketMode = "text",
        name: str | None = None,
        on_accept: AnyCallable | None = None,
        on_disconnect: AnyCallable | None = None,
        opt: dict[str, Any] | None = None,
        return_dto: type[AbstractDTO] | None | EmptyType = Empty,
        signature_namespace: Mapping[str, Any] | None = None,
        type_decoders: TypeDecodersSequence | None = None,
        type_encoders: TypeEncodersMap | None = None,
        websocket_class: type[WebSocket] | None = None,
        parameters: ParametersMap | None = None,
        **kwargs: Any,
    ) -> None: ...

    def __init__(
        self,
        path: str | list[str] | None = None,
        *,
        fn: AnyCallable,
        connection_accept_handler: Callable[[WebSocket], Coroutine[Any, Any, None]] = WebSocket.accept,
        connection_lifespan: Callable[..., AbstractAsyncContextManager[Any]] | None = None,
        dependencies: Dependencies | None = None,
        dto: type[AbstractDTO] | None | EmptyType = Empty,
        exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None,
        guards: Sequence[Guard] | None = None,
        max_concurrency: int = 1,
        message_id_key: str | None = None,
        middleware: Sequence[Middleware] | None = None,
        receive_mode: WebSocketMode = "text",
        send_mode: WebSocketMode = "text",
        name: str | None = None,
        on_accept: AnyCallable | None = None,
        on_disconnect: AnyCallable | None = None,
        opt: dict[str, Any] | None = None,
        return_dto: type[AbstractDTO] | None | EmptyType = Empty,
        signature_namespace: Mapping[str, Any] | None = None,
        type_decoders: TypeDecodersSequence | None = None,
        type_encoders: TypeEncodersMap | None = None,
        websocket_class: type[WebSocket] | None = None,
        parameters: ParametersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``WebsocketRouteHandler``

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments. If not given defaults
                to ``/``
            fn: The handler function
            connection_accept_handler: A callable that accepts a :class:`WebSocket <.connection.WebSocket>` instance
                and returns a coroutine that when awaited, will accept the connection. Defaults to ``WebSocket.accept``.
            connection_lifespan: An asynchronous context manager, handling the lifespan of the connection. By default,
                it calls the ``connection_accept_handler``, ``on_connect`` and ``on_disconnect``. Can request any
                dependencies, for example the :class:`WebSocket <.connection.WebSocket>` connection
            dependencies: A string keyed mapping of dependency :class:`Provider <.di.Provide>` instances.
            dto: :class:`AbstractDTO <.dto.base_dto.AbstractDTO>` to use for (de)serializing and
                validation of request data.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <.types.Guard>` callables.
            max_concurrency: Maximum number of messages that are processed concurrently within a single
                connection. Defaults to ``1``, meaning messages are handled strictly in order, one after another.
            message_id_key: When set, received messages are expected to be JSON objects containing a message
                identifier under this key. The identifier is removed from the payload passed to the handler and is
                added to the JSON reply under the same key, allowing replies to be correlated with their messages
                when messages are processed concurrently.
            middleware: A sequence of :class:`Middleware <.types.Middleware>`.
            receive_mode: Websocket mode to receive data in, either `text` or `binary`.
            send_mode: Websocket mode to receive data in, either `text` or `binary`.
            name: A string identifying the route handler.
            on_accept: Callback invoked after a connection has been accepted. Can request any dependencies, for example
                the :class:`WebSocket <.connection.WebSocket>` connection
            on_disconnect: Callback invoked after a connection has been closed. Can request any dependencies, for
                example the :class:`WebSocket <.connection.WebSocket>` connection
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <.types.Guard>` or
                wherever you have access to :class:`Request <.connection.Request>` or
                :class:`ASGI Scope <.types.Scope>`.
            return_dto: :class:`AbstractDTO <.dto.base_dto.AbstractDTO>` to use for serializing
                outbound response data.
            signature_namespace: A mapping of names to types for use in forward reference resolution during signature
                modelling.
            type_decoders: A sequence of tuples, each composed of a predicate testing for type identity and a msgspec
                hook for deserialization.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            websocket_class: A custom subclass of :class:`WebSocket <.connection.WebSocket>` to be used as route handler's
                default websocket class.
            parameters: A mapping of :func:`Parameter <.params.Parameter>` definitions
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if connection_lifespan and any([on_accept, on_disconnect, connection_accept_handler is not WebSocket.accept]):
            raise ImproperlyConfiguredException(
                "connection_lifespan can not be used with connection hooks "
                "(on_accept, on_disconnect, connection_accept_handler)",
            )

        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or max_concurrency < 1:
            raise ImproperlyConfiguredException("max_concurrency must be a positive integer")

        self.max_concurrency = max_concurrency
        self.message_id_key = message_id_key
        self._stub_dependency_keys: set[str] = set()
        self._message_kwargs_model: Any = Empty
        self._receive_mode: WebSocketMode = receive_mode
        self._send_mode: WebSocketMode = send_mode
        self._connection_lifespan = connection_lifespan
        self._send_handler: Callable[[WebSocket, Any], Coroutine[None, None, None]] | EmptyType = Empty
        self._receive_handler: Callable[[WebSocket], Any] | EmptyType = Empty

        self.connection_accept_handler = connection_accept_handler
        self.on_accept = ensure_async_callable(on_accept) if on_accept else None
        self.on_disconnect = ensure_async_callable(on_disconnect) if on_disconnect else None

        listener_dependencies = dict(dependencies or {})

        listener_dependencies["connection_lifespan_dependencies"] = create_stub_dependency(
            connection_lifespan or self.default_connection_lifespan
        )

        listener_dependencies["on_accept_dependencies"] = create_stub_dependency(self.on_accept)

        listener_dependencies["on_disconnect_dependencies"] = create_stub_dependency(self.on_disconnect)

        self._stub_dependency_keys = {
            "connection_lifespan_dependencies",
            "on_accept_dependencies",
            "on_disconnect_dependencies",
        }

        super().__init__(
            fn=fn,
            path=path,
            dependencies=listener_dependencies,
            exception_handlers=exception_handlers,
            guards=guards,
            middleware=middleware,
            name=name,
            opt=opt,
            signature_namespace=signature_namespace,
            dto=dto,
            return_dto=return_dto,
            type_decoders=type_decoders,
            type_encoders=type_encoders,
            websocket_class=websocket_class,
            parameters=parameters,
            **kwargs,
        )

    def _get_merge_opts(self, others: tuple[Router, ...]) -> dict[str, Any]:
        merge_opts = super()._get_merge_opts(others)
        merge_opts.update(
            receive_mode=self._receive_mode,
            send_mode=self._send_mode,
            connection_lifespan=self._connection_lifespan,
            connection_accept_handler=self.connection_accept_handler,
            max_concurrency=self.max_concurrency,
            message_id_key=self.message_id_key,
            on_accept=self.on_accept,
            on_disconnect=self.on_disconnect,
        )
        return merge_opts

    def on_registration(self, route: BaseRoute, app: Litestar) -> None:
        self.fn = self._prepare_fn()
        # finalize the dependencies without building the default connection kwargs model,
        # which is built explicitly below so message-scoped dependencies can be excluded.
        super(WebsocketRouteHandler, self).on_registration(route=route, app=app)
        self._message_kwargs_model = self._create_message_kwargs_model(
            path_parameters=route.path_parameters,
        )
        self._kwargs_model = self._create_connection_kwargs_model(
            path_parameters=route.path_parameters,
        )

    def _create_connection_kwargs_model(self, path_parameters: set[str]) -> KwargsModel:
        """Create the kwargs model for the connection scope.

        Only connection-scoped dependencies (requested by the connection lifespan / hooks)
        are resolved by this model; message-scoped dependencies are resolved independently
        for every message and are excluded here so they are not created once per connection.
        """
        from litestar._kwargs import KwargsModel
        from litestar._signature import SignatureModel
        from litestar.utils.signature import ParsedSignature

        message_dependency_keys = self._get_message_dependency_keys()

        connection_parsed_signature = ParsedSignature.from_signature(
            create_connection_handler_signature(
                self._original_callback_signature,
                excluded_parameter_names=message_dependency_keys,
            ),
            fn_type_hints={
                **self._original_callback_type_hints,
                **get_fn_type_hints(ListenerHandler.__call__, namespace=self.signature_namespace),
            },
        )

        self._connection_signature_model = SignatureModel.create(
            dependency_name_set=set(self.dependencies),
            fn=self.fn,
            parsed_signature=connection_parsed_signature,
            type_decoders=self.type_decoders,
        )

        return KwargsModel.create_for_signature_model(
            signature_model=self._connection_signature_model,
            parsed_signature=connection_parsed_signature,
            dependencies=self.dependencies,
            path_parameters=set(path_parameters),
            layered_parameters=self.parameter_field_definitions,
            ctx=self,
            excluded_dependency_keys=message_dependency_keys,
        )

    def _get_message_dependency_keys(self) -> set[str]:
        """Return the keys of message-scoped dependencies.

        A dependency is connection-scoped when it is (transitively) requested by the
        connection lifespan, ``on_accept`` or ``on_disconnect``. Every other handler
        dependency is message-scoped, i.e. resolved independently for every message.
        """
        return set(self.dependencies) - self._get_connection_dependency_keys()

    def _create_message_kwargs_model(self, path_parameters: set[str]) -> KwargsModel | None:
        """Create a kwargs model resolving dependencies independently for every message.

        Connection-scoped dependencies (those requested by the connection lifespan / hooks)
        are excluded: they are resolved once for the lifetime of the connection and passed
        through from the connection scope.
        """
        from litestar._kwargs import KwargsModel
        from litestar._signature import SignatureModel
        from litestar.utils.signature import ParsedSignature

        connection_dependency_keys = self._get_connection_dependency_keys()

        message_parsed_signature = ParsedSignature.from_signature(
            create_message_handler_signature(self._original_callback_signature),
            fn_type_hints={
                **self._original_callback_type_hints,
                **get_fn_type_hints(ListenerHandler.__call__, namespace=self.signature_namespace),
            },
        )

        message_signature_model = SignatureModel.create(
            dependency_name_set=set(self.dependencies),
            fn=self.fn,
            parsed_signature=message_parsed_signature,
            type_decoders=self.type_decoders,
        )

        model = KwargsModel.create_for_message_scope(
            signature_model=message_signature_model,
            parsed_signature=message_parsed_signature,
            dependencies=self.dependencies,
            excluded_dependency_keys=connection_dependency_keys,
            path_parameters=set(path_parameters),
            layered_parameters=self.parameter_field_definitions,
            ctx=self,
        )
        return model if model.has_kwargs else None

    def _get_connection_dependency_keys(self) -> set[str]:
        """Return the keys of all dependencies required by the connection lifespan/hooks.

        These dependencies are resolved once per connection and reused across messages.
        """
        keys: set[str] = set()

        def collect(key: str, seen: set[str]) -> None:
            if key in seen:
                return
            seen.add(key)
            provide = self.dependencies.get(key)
            if provide is None:
                return
            keys.add(key)
            for sub_key in provide.signature_model._fields:
                if sub_key in self.dependencies:
                    collect(sub_key, seen)

        for root_key in self._stub_dependency_keys:
            provide = self.dependencies.get(root_key)
            if provide is None:
                continue
            for requested_key in provide.signature_model._fields:
                if requested_key in self.dependencies:
                    collect(requested_key, set())
        return keys

    def _prepare_fn(self) -> ListenerHandler:
        if isinstance(self.fn, ListenerHandler):
            return self.fn
        original_fn = self.fn
        parsed_signature = ParsedSignature.from_fn(original_fn, self.signature_namespace)

        if "data" not in parsed_signature.parameters:
            raise ImproperlyConfiguredException("Websocket listeners must accept a 'data' parameter")

        for param in ("request", "body"):
            if param in parsed_signature.parameters:
                raise ImproperlyConfiguredException(f"The {param} kwarg is not supported with websocket listeners")

        # we are manipulating the signature of the decorated function below, so we must store the original values for
        # use elsewhere.
        self._original_callback_signature = parsed_signature.original_signature
        self._original_callback_type_hints = get_fn_type_hints(original_fn, namespace=self.signature_namespace)
        self._parsed_return_field = parsed_signature.return_type
        self._parsed_data_field = parsed_signature.parameters.get("data")
        self._parsed_fn_signature = ParsedSignature.from_signature(
            create_handler_signature(parsed_signature.original_signature),
            fn_type_hints={
                **self._original_callback_type_hints,
                **get_fn_type_hints(ListenerHandler.__call__, namespace=self.signature_namespace),
            },
        )

        return ListenerHandler(
            listener=self, fn=original_fn, parsed_signature=parsed_signature, namespace=self.signature_namespace
        )

    def _validate_handler_function(self) -> None:
        """Validate the route handler function once it's set by inspecting its return annotations."""
        # validation occurs in the call method

    @asynccontextmanager
    async def default_connection_lifespan(
        self,
        socket: WebSocket,
        on_accept_dependencies: NamedDependency[Optional[dict[str, Any]]] = None,  # noqa: UP045
        on_disconnect_dependencies: NamedDependency[Optional[dict[str, Any]]] = None,  # noqa: UP045
    ) -> AsyncGenerator[None, None]:
        """Handle the connection lifespan of a :class:`WebSocket <.connection.WebSocket>`.

        Args:
            socket: The :class:`WebSocket <.connection.WebSocket>` connection
            on_accept_dependencies: Dependencies requested by the :attr:`on_accept` hook
            on_disconnect_dependencies: Dependencies requested by the :attr:`on_disconnect` hook

        By, default this will

            - Call :attr:`connection_accept_handler` to accept a connection
            - Call :attr:`on_accept` if defined after a connection has been accepted
            - Call :attr:`on_disconnect` upon leaving the context
        """
        await self.connection_accept_handler(socket)

        if self.on_accept:
            await self.on_accept(**(on_accept_dependencies or {}))

        try:
            yield
        except WebSocketDisconnect:
            pass
        finally:
            if self.on_disconnect:
                await self.on_disconnect(**(on_disconnect_dependencies or {}))

    def resolve_receive_handler(self) -> Callable[[WebSocket], Any]:
        if self._receive_handler is Empty:
            self._receive_handler = create_handle_receive(self)
        return self._receive_handler

    def resolve_send_handler(self) -> Callable[[WebSocket, Any], Coroutine[None, None, None]]:
        if self._send_handler is Empty:
            self._send_handler = create_handle_send(self)
        return self._send_handler


class WebsocketListener(ABC):
    path: str | list[str] | None = None
    """A path fragment for the route handler function or a sequence of path fragments. If not given defaults to ``/``"""
    dependencies: Dependencies | None = None
    """A string keyed mapping of dependency :class:`Provider <.di.Provide>` instances."""
    dto: type[AbstractDTO] | None | EmptyType = Empty
    """:class:`AbstractDTO <.dto.base_dto.AbstractDTO>` to use for (de)serializing and validation of request data"""
    exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None
    """A mapping of status codes and/or exception types to handler functions."""
    guards: list[Guard] | None = None
    """A sequence of :class:`Guard <.types.Guard>` callables."""
    max_concurrency: int = 1
    """Maximum number of messages processed concurrently within a single connection."""
    message_id_key: str | None = None
    """Key of the message identifier used to correlate replies with requests."""
    middleware: list[Middleware] | None = None
    """A sequence of :class:`Middleware <.types.Middleware>`."""
    receive_mode: WebSocketMode = "text"
    """:class:`WebSocket <.connection.WebSocket>` mode to receive data in, either ``text`` or ``binary``."""
    send_mode: WebSocketMode = "text"
    """Websocket mode to send data in, either `text` or `binary`."""
    name: str | None = None
    """A string identifying the route handler."""
    opt: dict[str, Any] | None = None
    """
    A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <.types.Guard>` or wherever you
    have access to :class:`Request <.connection.Request>` or :class:`ASGI Scope <.types.Scope>`.
    """
    return_dto: type[AbstractDTO] | None | EmptyType = Empty
    """:class:`AbstractDTO <.dto.base_dto.AbstractDTO>` to use for serializing outbound response data."""
    signature_namespace: Mapping[str, Any] | None = None
    """
    A mapping of names to types for use in forward reference resolution during signature modelling.
    """
    type_decoders: TypeDecodersSequence | None = None
    """
    type_decoders: A sequence of tuples, each composed of a predicate testing for type identity and a msgspec
        hook for deserialization.
    """
    type_encoders: TypeEncodersMap | None = None
    """
    type_encoders: A mapping of types to callables that transform them into types supported for serialization.
    """
    websocket_class: type[WebSocket] | None = None
    """
    websocket_class: A custom subclass of :class:`WebSocket <.connection.WebSocket>` to be used as route handler's
    default websocket class.
    """

    def to_handler(self) -> WebsocketListenerRouteHandler:
        on_accept = self.on_accept if type(self).on_accept != WebsocketListener.on_accept else None
        on_disconnect = self.on_disconnect if type(self).on_disconnect != WebsocketListener.on_disconnect else None

        return WebsocketListenerRouteHandler(
            dependencies=self.dependencies,
            dto=self.dto,
            exception_handlers=self.exception_handlers,
            guards=self.guards,
            max_concurrency=self.max_concurrency,
            message_id_key=self.message_id_key,
            middleware=self.middleware,
            send_mode=self.send_mode,
            receive_mode=self.receive_mode,
            name=self.name,
            on_accept=on_accept,
            on_disconnect=on_disconnect,
            opt=self.opt,
            path=self.path,
            return_dto=self.return_dto,
            signature_namespace=self.signature_namespace,
            type_decoders=self.type_decoders,
            type_encoders=self.type_encoders,
            websocket_class=self.websocket_class,
            fn=self.on_receive,
        )

    def on_accept(self, *args: Any, **kwargs: Any) -> Any:
        """Called after a :class:`WebSocket <.connection.WebSocket>` connection
        has been accepted. Can receive any dependencies
        """

    def on_disconnect(self, *args: Any, **kwargs: Any) -> Any:
        """Called after a :class:`WebSocket <.connection.WebSocket>` connection
        has been disconnected. Can receive any dependencies
        """

    @abstractmethod
    def on_receive(self, *args: Any, **kwargs: Any) -> Any:
        """Called after data has been received from the WebSocket.

        This should take a ``data`` argument, receiving the processed WebSocket data,
        and can additionally include handler dependencies such as ``state``, or other
        regular dependencies.

        Data returned from this function will be serialized and sent via the socket
        according to handler configuration.
        """
        raise NotImplementedError


@overload
def websocket_listener(
    path: str | list[str] | None = None,
    *,
    connection_lifespan: Callable[..., AbstractAsyncContextManager[Any]] | None = None,
    dependencies: Dependencies | None = None,
    dto: type[AbstractDTO] | None | EmptyType = Empty,
    exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None,
    guards: list[Guard] | None = None,
    max_concurrency: int = 1,
    message_id_key: str | None = None,
    middleware: list[Middleware] | None = None,
    receive_mode: WebSocketMode = "text",
    send_mode: WebSocketMode = "text",
    name: str | None = None,
    opt: dict[str, Any] | None = None,
    return_dto: type[AbstractDTO] | None | EmptyType = Empty,
    signature_namespace: Mapping[str, Any] | None = None,
    type_decoders: TypeDecodersSequence | None = None,
    type_encoders: TypeEncodersMap | None = None,
    websocket_class: type[WebSocket] | None = None,
    **kwargs: Any,
) -> Callable[[AnyCallable], WebsocketListenerRouteHandler]: ...


@overload
def websocket_listener(
    path: str | list[str] | None = None,
    *,
    connection_accept_handler: Callable[[WebSocket], Coroutine[Any, Any, None]] = WebSocket.accept,
    dependencies: Dependencies | None = None,
    dto: type[AbstractDTO] | None | EmptyType = Empty,
    exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None,
    guards: list[Guard] | None = None,
    max_concurrency: int = 1,
    message_id_key: str | None = None,
    middleware: list[Middleware] | None = None,
    receive_mode: WebSocketMode = "text",
    send_mode: WebSocketMode = "text",
    name: str | None = None,
    on_accept: AnyCallable | None = None,
    on_disconnect: AnyCallable | None = None,
    opt: dict[str, Any] | None = None,
    return_dto: type[AbstractDTO] | None | EmptyType = Empty,
    signature_namespace: Mapping[str, Any] | None = None,
    type_decoders: TypeDecodersSequence | None = None,
    type_encoders: TypeEncodersMap | None = None,
    websocket_class: type[WebSocket] | None = None,
    **kwargs: Any,
) -> Callable[[AnyCallable], WebsocketListenerRouteHandler]: ...


def websocket_listener(
    path: str | list[str] | None = None,
    *,
    connection_accept_handler: Callable[[WebSocket], Coroutine[Any, Any, None]] = WebSocket.accept,
    connection_lifespan: Callable[..., AbstractAsyncContextManager[Any]] | None = None,
    dependencies: Dependencies | None = None,
    dto: type[AbstractDTO] | None | EmptyType = Empty,
    exception_handlers: dict[int | type[Exception], ExceptionHandler] | None = None,
    guards: list[Guard] | None = None,
    max_concurrency: int = 1,
    message_id_key: str | None = None,
    middleware: list[Middleware] | None = None,
    receive_mode: WebSocketMode = "text",
    send_mode: WebSocketMode = "text",
    name: str | None = None,
    on_accept: AnyCallable | None = None,
    on_disconnect: AnyCallable | None = None,
    opt: dict[str, Any] | None = None,
    return_dto: type[AbstractDTO] | None | EmptyType = Empty,
    signature_namespace: Mapping[str, Any] | None = None,
    type_decoders: TypeDecodersSequence | None = None,
    type_encoders: TypeEncodersMap | None = None,
    websocket_class: type[WebSocket] | None = None,
    **kwargs: Any,
) -> Callable[[AnyCallable], WebsocketListenerRouteHandler]:
    """Create a :class:`WebsocketListenerRouteHandler`.

    Args:
        path: A path fragment for the route handler function or a sequence of path fragments. If not given defaults
            to ``/``
        connection_accept_handler: A callable that accepts a :class:`WebSocket <.connection.WebSocket>` instance
            and returns a coroutine that when awaited, will accept the connection. Defaults to ``WebSocket.accept``.
        connection_lifespan: An asynchronous context manager, handling the lifespan of the connection. By default,
            it calls the ``connection_accept_handler``, ``on_connect`` and ``on_disconnect``. Can request any
            dependencies, for example the :class:`WebSocket <.connection.WebSocket>` connection
        dependencies: A string keyed mapping of dependency :class:`Provider <.di.Provide>` instances.
        dto: :class:`AbstractDTO <.dto.base_dto.AbstractDTO>` to use for (de)serializing and
            validation of request data.
        exception_handlers: A mapping of status codes and/or exception types to handler functions.
        guards: A sequence of :class:`Guard <.types.Guard>` callables.
        max_concurrency: Maximum number of messages processed concurrently within a single connection. Defaults
            to ``1`` (messages are handled strictly sequentially).
        message_id_key: Key of the message identifier in JSON messages, used to correlate replies with requests
            when messages are processed concurrently.
        middleware: A sequence of :class:`Middleware <.types.Middleware>`.
        receive_mode: Websocket mode to receive data in, either `text` or `binary`.
        send_mode: Websocket mode to receive data in, either `text` or `binary`.
        name: A string identifying the route handler.
        on_accept: Callback invoked after a connection has been accepted. Can request any dependencies, for example
            the :class:`WebSocket <.connection.WebSocket>` connection
        on_disconnect: Callback invoked after a connection has been closed. Can request any dependencies, for
            example the :class:`WebSocket <.connection.WebSocket>` connection
        opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <.types.Guard>` or
            wherever you have access to :class:`Request <.connection.Request>` or
            :class:`ASGI Scope <.types.Scope>`.
        return_dto: :class:`AbstractDTO <.dto.base_dto.AbstractDTO>` to use for serializing
            outbound response data.
        signature_namespace: A mapping of names to types for use in forward reference resolution during signature
            modelling.
        type_decoders: A sequence of tuples, each composed of a predicate testing for type identity and a msgspec
            hook for deserialization.
        type_encoders: A mapping of types to callables that transform them into types supported for serialization.
        **kwargs: Any additional kwarg - will be set in the opt dictionary.
        websocket_class: A custom subclass of :class:`WebSocket <.connection.WebSocket>` to be used as route handler's
            default websocket class.
    """

    def decorator(fn: AnyCallable) -> WebsocketListenerRouteHandler:
        return WebsocketListenerRouteHandler(
            fn=fn,
            path=path,
            connection_accept_handler=connection_accept_handler,
            connection_lifespan=connection_lifespan,
            dependencies=dependencies,
            dto=dto,
            exception_handlers=exception_handlers,
            guards=guards,
            max_concurrency=max_concurrency,
            message_id_key=message_id_key,
            middleware=middleware,
            receive_mode=receive_mode,
            send_mode=send_mode,
            name=name,
            on_accept=on_accept,
            on_disconnect=on_disconnect,
            opt=opt,
            return_dto=return_dto,
            signature_namespace=signature_namespace,
            type_decoders=type_decoders,
            type_encoders=type_encoders,
            websocket_class=websocket_class,
            **kwargs,
        )

    return decorator

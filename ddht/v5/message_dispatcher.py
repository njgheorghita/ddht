import contextlib
import logging
import random
from types import TracebackType
from typing import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Dict,
    Optional,
    Tuple,
    Type,
    TypeVar,
)

from async_service import Service
from eth_enr import ENRDatabaseAPI
from eth_typing import NodeID
from eth_utils import encode_hex
import trio
from trio.abc import ReceiveChannel, SendChannel

from ddht.base_message import (
    AnyInboundMessage,
    AnyOutboundMessage,
    InboundMessage,
    TBaseMessage,
)
from ddht.constants import IP_V4_ADDRESS_ENR_KEY, UDP_PORT_ENR_KEY
from ddht.endpoint import Endpoint
from ddht.exceptions import UnexpectedMessage
from ddht.v5.abc import ChannelHandlerSubscriptionAPI, MessageDispatcherAPI
from ddht.v5.constants import (
    MAX_NODES_MESSAGE_TOTAL,
    MAX_REQUEST_ID,
    MAX_REQUEST_ID_ATTEMPTS,
)
from ddht.v5.messages import BaseMessage, NodesMessage


def get_random_request_id() -> int:
    return random.randint(0, MAX_REQUEST_ID)


ChannelContentType = TypeVar("ChannelContentType")


class ChannelHandlerSubscription(ChannelHandlerSubscriptionAPI[ChannelContentType]):
    def __init__(
        self,
        send_channel: SendChannel[ChannelContentType],
        receive_channel: ReceiveChannel[ChannelContentType],
        remove_fn: Callable[[], None],
    ) -> None:
        self._send_channel = send_channel
        self.receive_channel = receive_channel
        self.remove_fn = remove_fn

    def cancel(self) -> None:
        self.remove_fn()

    async def __aenter__(self) -> "ChannelHandlerSubscription[ChannelContentType]":
        await self._send_channel.__aenter__()
        await self.receive_channel.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.remove_fn()
        await self._send_channel.__aexit__()
        await self.receive_channel.__aexit__()

    async def receive(self) -> ChannelContentType:
        return await self.receive_channel.receive()

    def __aiter__(self) -> AsyncIterator[ChannelContentType]:
        return self

    async def __anext__(self) -> ChannelContentType:
        try:
            return await self.receive()
        except trio.EndOfChannel:
            raise StopAsyncIteration


InboundMessageSubscription = ChannelHandlerSubscription[InboundMessage[TBaseMessage]]
AnyInboundMessageSubscription = ChannelHandlerSubscription[AnyInboundMessage]


class MessageDispatcher(Service, MessageDispatcherAPI):
    logger = logging.getLogger("ddht.v5.message_dispatcher.MessageDispatcher")

    def __init__(
        self,
        enr_db: ENRDatabaseAPI,
        inbound_message_receive_channel: ReceiveChannel[AnyInboundMessage],
        outbound_message_send_channel: SendChannel[AnyOutboundMessage],
    ) -> None:
        self.enr_db = enr_db

        self.inbound_message_receive_channel = inbound_message_receive_channel
        self.outbound_message_send_channel = outbound_message_send_channel

        self.request_handler_send_channels: Dict[
            int, SendChannel[AnyInboundMessage]
        ] = {}
        self.response_handler_send_channels: Dict[
            Tuple[NodeID, int], SendChannel[AnyInboundMessage]
        ] = {}

    async def run(self) -> None:
        async with self.inbound_message_receive_channel, self.outbound_message_send_channel:
            async for inbound_message in self.inbound_message_receive_channel:
                await self.handle_inbound_message(inbound_message)

    async def handle_inbound_message(self, inbound_message: AnyInboundMessage) -> None:
        sender_node_id = inbound_message.sender_node_id
        message_type = inbound_message.message.message_type
        request_id = inbound_message.message.request_id

        is_request = message_type in self.request_handler_send_channels
        is_response = (
            sender_node_id,
            request_id,
        ) in self.response_handler_send_channels

        if is_request and is_response:
            self.logger.debug(
                "%s from %s is both a response to an earlier request (id %d) and a request a "
                "handler is present for (message type %d). Message will be handled twice.",
                inbound_message,
                encode_hex(sender_node_id),
                request_id,
                message_type,
            )
        if not is_request and not is_response:
            self.logger.debug(
                "Dropping %s from %s (request id %d, message type %d) as neither a request nor a "
                "response handler is present",
                inbound_message,
                encode_hex(sender_node_id),
                request_id,
                message_type,
            )
            await trio.lowlevel.checkpoint()

        if is_request:
            self.logger.debug(
                "Received request %s with id %d from %s",
                inbound_message,
                request_id,
                encode_hex(sender_node_id),
            )
            send_channel = self.request_handler_send_channels[message_type]
            await send_channel.send(inbound_message)

        if is_response:
            self.logger.debug(
                "Received response %s for request with id %d from %s",
                inbound_message,
                request_id,
                encode_hex(sender_node_id),
            )
            send_channel = self.response_handler_send_channels[
                sender_node_id, request_id
            ]
            await send_channel.send(inbound_message)

    def get_free_request_id(self, node_id: NodeID) -> int:
        for _ in range(MAX_REQUEST_ID_ATTEMPTS):
            request_id = get_random_request_id()
            if (node_id, request_id) not in self.response_handler_send_channels:
                return request_id
        else:
            # this should be extremely unlikely to happen
            raise ValueError(
                f"Failed to get free request id ({len(self.response_handler_send_channels)} "
                f"handlers added right now)"
            )

    def add_request_handler(
        self, message_class: Type[TBaseMessage]
    ) -> InboundMessageSubscription[TBaseMessage]:
        message_type = message_class.message_type
        if message_type in self.request_handler_send_channels:
            raise ValueError(
                f"Request handler for {message_class.__name__} is already added"
            )

        request_channels = trio.open_memory_channel[InboundMessage[TBaseMessage]](0)
        self.request_handler_send_channels[message_type] = request_channels[0]

        self.logger.debug("Adding request handler for %s", message_class.__name__)

        def remove() -> None:
            try:
                self.request_handler_send_channels.pop(message_type)
            except KeyError:
                raise ValueError(
                    f"Request handler for {message_class.__name__} has already been removed"
                )
            else:
                self.logger.debug(
                    "Removing request handler for %s", message_class.__name__
                )

        return ChannelHandlerSubscription(
            send_channel=request_channels[0],
            receive_channel=request_channels[1],
            remove_fn=remove,
        )

    def add_response_handler(
        self, remote_node_id: NodeID, request_id: int
    ) -> AnyInboundMessageSubscription:
        if (remote_node_id, request_id) in self.response_handler_send_channels:
            raise ValueError(
                f"Response handler for node id {encode_hex(remote_node_id)} and request id "
                f"{request_id} has already been added"
            )

        self.logger.debug(
            "Adding response handler for peer %s and request id %d",
            encode_hex(remote_node_id),
            request_id,
        )

        response_channels = trio.open_memory_channel[AnyInboundMessage](0)
        self.response_handler_send_channels[
            (remote_node_id, request_id)
        ] = response_channels[0]

        def remove() -> None:
            try:
                self.response_handler_send_channels.pop((remote_node_id, request_id))
            except KeyError:
                raise ValueError(
                    f"Response handler for node id {encode_hex(remote_node_id)} and request id "
                    f"{request_id} has already been removed"
                )
            else:
                self.logger.debug(
                    "Removing response handler for peer %s and request id %d",
                    encode_hex(remote_node_id),
                    request_id,
                )

        return AnyInboundMessageSubscription(
            send_channel=response_channels[0],
            receive_channel=response_channels[1],
            remove_fn=remove,
        )

    async def get_endpoint_from_enr_db(self, receiver_node_id: NodeID) -> Endpoint:
        try:
            enr = self.enr_db.get_enr(receiver_node_id)
        except KeyError:
            raise ValueError(f"No ENR for peer {encode_hex(receiver_node_id)} known")

        try:
            ip_address = enr[IP_V4_ADDRESS_ENR_KEY]
        except KeyError:
            raise ValueError(
                f"ENR for peer {encode_hex(receiver_node_id)} does not contain an IP address"
            )

        try:
            udp_port = enr[UDP_PORT_ENR_KEY]
        except KeyError:
            raise ValueError(
                f"ENR for peer {encode_hex(receiver_node_id)} does not contain a UDP port"
            )

        return Endpoint(ip_address, udp_port)

    @contextlib.asynccontextmanager
    async def request_response_subscription(
        self,
        receiver_node_id: NodeID,
        message: BaseMessage,
        endpoint: Optional[Endpoint] = None,
    ) -> AsyncGenerator[AnyInboundMessageSubscription, None]:
        if endpoint is None:
            endpoint = await self.get_endpoint_from_enr_db(receiver_node_id)

        response_channels = trio.open_memory_channel[AnyInboundMessage](0)
        response_send_channel, response_receive_channel = response_channels

        async with self.add_response_handler(
            receiver_node_id, message.request_id
        ) as response_subscription:
            outbound_message = AnyOutboundMessage(
                message=message,
                receiver_node_id=receiver_node_id,
                receiver_endpoint=endpoint,
            )
            self.logger.debug(
                "Sending %s to %s with request id %d",
                outbound_message,
                encode_hex(receiver_node_id),
                message.request_id,
            )
            await self.outbound_message_send_channel.send(outbound_message)
            yield response_subscription

    async def request(
        self,
        receiver_node_id: NodeID,
        message: BaseMessage,
        endpoint: Optional[Endpoint] = None,
    ) -> AnyInboundMessage:
        async with self.request_response_subscription(
            receiver_node_id, message, endpoint
        ) as response_subscription:
            response = await response_subscription.receive()
            self.logger.debug(
                "Received %s from %s with request id %d",
                response,
                encode_hex(receiver_node_id),
                message.request_id,
            )
            return response

    async def request_nodes(
        self,
        receiver_node_id: NodeID,
        message: BaseMessage,
        endpoint: Optional[Endpoint] = None,
    ) -> Tuple[AnyInboundMessage, ...]:
        async with self.request_response_subscription(
            receiver_node_id, message, endpoint
        ) as response_subscription:
            first_response = await response_subscription.receive()
            self.logger.debug(
                "Received %s from %s with request id %d",
                first_response,
                encode_hex(receiver_node_id),
                message.request_id,
            )
            if not isinstance(first_response.message, NodesMessage):
                raise UnexpectedMessage(
                    f"Peer {encode_hex(receiver_node_id)} responded with "
                    f"{first_response.message.__class__.__name__} instead of Nodes message"
                )

            total = first_response.message.total
            if total > MAX_NODES_MESSAGE_TOTAL:
                raise UnexpectedMessage(
                    f"Peer {encode_hex(receiver_node_id)} sent nodes message with a total value of "
                    f"{total} which is too big"
                )
            self.logger.debug(
                "Received nodes response %d of %d from %s with request id %d",
                1,
                total,
                encode_hex(receiver_node_id),
                message.request_id,
            )

            responses = [first_response]
            for response_index in range(1, total):
                next_response = await response_subscription.receive()
                if not isinstance(first_response.message, NodesMessage):
                    raise UnexpectedMessage(
                        f"Peer {encode_hex(receiver_node_id)} responded with "
                        f"{next_response.message.__class__.__name__} instead of Nodes message"
                    )
                responses.append(next_response)
                self.logger.debug(
                    "Received nodes response %d of %d from %s with request id %d",
                    response_index + 1,
                    total,
                    encode_hex(receiver_node_id),
                    message.request_id,
                )
            return tuple(responses)

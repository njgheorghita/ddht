import logging

from async_service import Service, run_trio_service
from eth.db.backends.level import LevelDB
from eth_keys import keys
from eth_utils import encode_hex
import trio

from ddht._utils import generate_node_key_file, read_node_key_file
from ddht.boot_info import BootInfo
from ddht.constants import (
    DEFAULT_LISTEN,
    IP_V4_ADDRESS_ENR_KEY,
    NUM_ROUTING_TABLE_BUCKETS,
)
from ddht.enr_manager import ENRManager
from ddht.exceptions import OldSequenceNumber
from ddht.identity_schemes import default_identity_scheme_registry
from ddht.kademlia import KademliaRoutingTable
from ddht.node_db import NodeDB
from ddht.typing import AnyIPAddress
from ddht.upnp import UPnPService
from ddht.v5.channel_services import (
    DatagramReceiver,
    DatagramSender,
    IncomingDatagram,
    IncomingMessage,
    IncomingPacket,
    OutgoingDatagram,
    OutgoingMessage,
    OutgoingPacket,
    PacketDecoder,
    PacketEncoder,
)
from ddht.v5.endpoint_tracker import EndpointTracker, EndpointVote
from ddht.v5.message_dispatcher import MessageDispatcher
from ddht.v5.messages import v5_registry
from ddht.v5.packer import Packer
from ddht.v5.routing_table_manager import RoutingTableManager

logger = logging.getLogger("ddht.DDHT")


ENR_DATABASE_DIR_NAME = "enr-db"


def get_local_private_key(boot_info: BootInfo) -> keys.PrivateKey:
    if boot_info.private_key is None:
        # load from disk or generate
        node_key_file_path = boot_info.base_dir / "nodekey"
        if not node_key_file_path.exists():
            generate_node_key_file(node_key_file_path)
        return read_node_key_file(node_key_file_path)
    else:
        return boot_info.private_key


class Application(Service):
    logger = logger
    _boot_info: BootInfo

    def __init__(self, boot_info: BootInfo) -> None:
        self._boot_info = boot_info

    async def _update_enr_ip_from_upnp(
        self, enr_manager: ENRManager, upnp_service: UPnPService
    ) -> None:
        await upnp_service.get_manager().wait_started()

        with trio.move_on_after(10):
            _, external_ip = await upnp_service.get_ip_addresses()
            await enr_manager.async_update((IP_V4_ADDRESS_ENR_KEY, external_ip.packed))

        while self.manager.is_running:
            _, external_ip = await upnp_service.wait_ip_changed()
            await enr_manager.async_update((IP_V4_ADDRESS_ENR_KEY, external_ip.packed))

    async def run(self) -> None:
        identity_scheme_registry = default_identity_scheme_registry
        message_type_registry = v5_registry

        enr_database_dir = self._boot_info.base_dir / ENR_DATABASE_DIR_NAME
        enr_database_dir.mkdir(exist_ok=True)
        node_db = NodeDB(default_identity_scheme_registry, LevelDB(enr_database_dir))

        local_private_key = get_local_private_key(self._boot_info)

        enr_manager = ENRManager(node_db=node_db, private_key=local_private_key,)
        self.manager.run_daemon_child_service(enr_manager)

        port = self._boot_info.port

        if b"udp" not in enr_manager.enr:
            enr_manager.update((b"udp", port))

        listen_on: AnyIPAddress
        if self._boot_info.listen_on is None:
            listen_on = DEFAULT_LISTEN
        else:
            listen_on = self._boot_info.listen_on
            # Update the ENR if an explicit listening address was provided
            enr_manager.update((IP_V4_ADDRESS_ENR_KEY, listen_on.packed))

        if self._boot_info.is_upnp_enabled:
            upnp_service = UPnPService(port)
            self.manager.run_daemon_child_service(upnp_service)
            self.manager.run_daemon_task(
                self._update_enr_ip_from_upnp, enr_manager, upnp_service
            )

        routing_table = KademliaRoutingTable(
            enr_manager.enr.node_id, NUM_ROUTING_TABLE_BUCKETS
        )

        for enr in self._boot_info.bootnodes:
            try:
                node_db.set_enr(enr)
            except OldSequenceNumber:
                pass
            routing_table.update(enr.node_id)

        socket = trio.socket.socket(
            family=trio.socket.AF_INET, type=trio.socket.SOCK_DGRAM
        )
        outgoing_datagram_channels = trio.open_memory_channel[OutgoingDatagram](0)
        incoming_datagram_channels = trio.open_memory_channel[IncomingDatagram](0)
        outgoing_packet_channels = trio.open_memory_channel[OutgoingPacket](0)
        incoming_packet_channels = trio.open_memory_channel[IncomingPacket](0)
        outgoing_message_channels = trio.open_memory_channel[OutgoingMessage](0)
        incoming_message_channels = trio.open_memory_channel[IncomingMessage](0)
        endpoint_vote_channels = trio.open_memory_channel[EndpointVote](0)

        # types ignored due to https://github.com/ethereum/async-service/issues/5
        datagram_sender = DatagramSender(  # type: ignore
            outgoing_datagram_channels[1], socket
        )
        datagram_receiver = DatagramReceiver(  # type: ignore
            socket, incoming_datagram_channels[0]
        )

        packet_encoder = PacketEncoder(  # type: ignore
            outgoing_packet_channels[1], outgoing_datagram_channels[0]
        )
        packet_decoder = PacketDecoder(  # type: ignore
            incoming_datagram_channels[1], incoming_packet_channels[0]
        )

        packer = Packer(
            local_private_key=local_private_key.to_bytes(),
            local_node_id=enr_manager.enr.node_id,
            node_db=node_db,
            message_type_registry=message_type_registry,
            incoming_packet_receive_channel=incoming_packet_channels[1],
            incoming_message_send_channel=incoming_message_channels[0],
            outgoing_message_receive_channel=outgoing_message_channels[1],
            outgoing_packet_send_channel=outgoing_packet_channels[0],
        )

        message_dispatcher = MessageDispatcher(
            node_db=node_db,
            incoming_message_receive_channel=incoming_message_channels[1],
            outgoing_message_send_channel=outgoing_message_channels[0],
        )

        endpoint_tracker = EndpointTracker(
            local_private_key=local_private_key.to_bytes(),
            local_node_id=enr_manager.enr.node_id,
            node_db=node_db,
            identity_scheme_registry=identity_scheme_registry,
            vote_receive_channel=endpoint_vote_channels[1],
        )

        routing_table_manager = RoutingTableManager(
            local_node_id=enr_manager.enr.node_id,
            routing_table=routing_table,
            message_dispatcher=message_dispatcher,
            node_db=node_db,
            outgoing_message_send_channel=outgoing_message_channels[0],
            endpoint_vote_send_channel=endpoint_vote_channels[0],
        )

        logger.info(f"DDHT base dir: {self._boot_info.base_dir}")
        logger.info("Starting discovery service...")
        logger.info(f"Listening on {listen_on}:{port}")
        logger.info(f"Local Node ID: {encode_hex(enr_manager.enr.node_id)}")
        logger.info(f"Local ENR: {enr_manager.enr}")

        services = (
            datagram_sender,
            datagram_receiver,
            packet_encoder,
            packet_decoder,
            packer,
            message_dispatcher,
            endpoint_tracker,
            routing_table_manager,
        )
        await socket.bind((str(listen_on), port))
        with socket:
            async with trio.open_nursery() as nursery:
                for service in services:
                    nursery.start_soon(run_trio_service, service)
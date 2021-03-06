import pytest
import trio

from ddht.base_message import OutboundMessage
from ddht.v5_1.messages import PingMessage, PongMessage


@pytest.mark.trio
async def test_dispatcher_handles_incoming_envelopes(tester, driver, alice, bob):
    await driver.handshake()

    async with tester.dispatcher_pair(alice, bob) as (
        alice_dispatcher,
        bob_dispatcher,
    ):
        with trio.fail_after(1):
            async with alice.events.ping_received.subscribe_and_wait():
                await driver.recipient.send_ping(b"\x12")


@pytest.fixture
async def paired_dispatchers(tester, driver, alice, bob):
    async with tester.dispatcher_pair(alice, bob) as (
        alice_dispatcher,
        bob_dispatcher,
    ):
        yield (alice_dispatcher, bob_dispatcher)


@pytest.mark.trio
async def test_dispatcher_bidirectional_communication(
    driver, alice, bob, paired_dispatchers
):
    alice_dispatcher, bob_dispatcher = paired_dispatchers
    async with alice.events.session_handshake_complete.subscribe_and_wait():
        async with bob.events.session_handshake_complete.subscribe_and_wait():
            async with alice.events.ping_sent.subscribe_and_wait():
                async with bob.events.ping_received.subscribe_and_wait():
                    await alice_dispatcher.send_message(
                        OutboundMessage(
                            PingMessage(b"\x12", alice.enr.sequence_number),
                            bob.endpoint,
                            bob.node_id,
                        )
                    )


@pytest.mark.trio
async def test_dispatcher_handles_incoming_envelopes_with_multiple_sessions(
    tester, driver, alice, bob, paired_dispatchers
):
    driver_a = driver
    # setup another session (that hasn't been handshaked yet.
    driver_b = tester.session_pair(bob, alice)

    # now when we send a message to initiate the handshake from `driver_b`,
    # that message should also be routed to `driver_a` but will not be
    # decodable and should be discarded.
    with trio.fail_after(2):
        async with driver_a.initiator.events.packet_discarded.subscribe_and_wait():
            async with alice.events.ping_received.subscribe_and_wait():
                await driver_b.initiator.send_ping(b"\x12")


@pytest.mark.trio
async def test_dispatcher_send_message_with_existing_session(
    driver, alice, bob, paired_dispatchers
):
    alice_dispatcher, _ = paired_dispatchers

    async with bob.events.ping_received.subscribe_and_wait():
        await alice_dispatcher.send_message(
            OutboundMessage(
                PingMessage(b"\x12", alice.enr.sequence_number),
                bob.endpoint,
                bob.node_id,
            )
        )

    async with bob.events.ping_received.subscribe_and_wait():
        await alice_dispatcher.send_message(
            OutboundMessage(
                PingMessage(b"\x34", alice.enr.sequence_number),
                bob.endpoint,
                bob.node_id,
            )
        )


@pytest.mark.trio
async def test_dispatcher_send_message_creates_session(
    tester, driver, alice, paired_dispatchers
):
    carol = tester.node()
    alice_dispatcher, _ = paired_dispatchers

    async with alice.events.session_created.subscribe_and_wait():
        await alice_dispatcher.send_message(
            OutboundMessage(
                PingMessage(b"\x12", alice.enr.sequence_number),
                carol.endpoint,
                carol.node_id,
            )
        )


@pytest.mark.trio
async def test_dispatcher_subscribe_to_message_type(tester, alice, bob):
    carol = tester.node()
    driver_a = tester.session_pair(alice, bob)
    driver_b = tester.session_pair(carol, alice)

    await driver_a.handshake()
    await driver_b.handshake()

    async with tester.dispatcher_pair(alice, bob) as (alice_dispatcher, _):
        async with tester.dispatcher_pair(alice, carol):
            async with alice_dispatcher.subscribe(PingMessage) as ping_subscription:
                await driver_a.recipient.send_ping(b"\x12")
                await driver_a.initiator.send_pong(b"\x12")
                await driver_b.initiator.send_ping(b"\x34")
                await driver_b.recipient.send_pong(b"\x34")

                with trio.fail_after(1):
                    ping_message_a = await ping_subscription.receive()
                    ping_message_b = await ping_subscription.receive()

    assert ping_message_a.sender_node_id == bob.node_id
    assert ping_message_b.sender_node_id == carol.node_id


@pytest.mark.trio
async def test_dispatcher_subscribe_to_message_type_with_endpoint_filter(
    tester, alice, bob
):
    carol = tester.node()
    driver_a = tester.session_pair(alice, bob)
    driver_b = tester.session_pair(carol, alice)

    await driver_a.handshake()
    await driver_b.handshake()

    async with tester.dispatcher_pair(alice, bob) as (alice_dispatcher, _):
        async with tester.dispatcher_pair(alice, carol):
            async with alice_dispatcher.subscribe(
                PingMessage, endpoint=carol.endpoint
            ) as ping_subscription:
                await driver_a.recipient.send_ping(b"\x12")
                await driver_a.initiator.send_pong(b"\x12")
                await driver_b.initiator.send_ping(b"\x34")
                await driver_b.recipient.send_pong(b"\x34")

                with trio.fail_after(1):
                    ping_message_a = await ping_subscription.receive()

    assert ping_message_a.message.request_id == b"\x34"
    assert ping_message_a.sender_node_id == carol.node_id


@pytest.mark.trio
async def test_dispatcher_subscribe_to_message_type_with_node_id_filter(
    tester, alice, bob
):
    carol = tester.node()
    driver_a = tester.session_pair(alice, bob)
    driver_b = tester.session_pair(carol, alice)

    await driver_a.handshake()
    await driver_b.handshake()

    async with tester.dispatcher_pair(alice, bob) as (alice_dispatcher, _):
        async with tester.dispatcher_pair(alice, carol):
            async with alice_dispatcher.subscribe(
                PingMessage, node_id=carol.node_id
            ) as ping_subscription:
                await driver_a.recipient.send_ping(b"\x12")
                await driver_a.initiator.send_pong(b"\x12")
                await driver_b.initiator.send_ping(b"\x34")
                await driver_b.recipient.send_pong(b"\x34")

                with trio.fail_after(1):
                    ping_message_a = await ping_subscription.receive()

    assert ping_message_a.message.request_id == b"\x34"
    assert ping_message_a.sender_node_id == carol.node_id


@pytest.mark.trio
async def test_dispatcher_subscribe_request_response(tester, alice, bob):
    carol = tester.node()
    driver_a = tester.session_pair(alice, bob)
    driver_b = tester.session_pair(carol, alice)

    await driver_a.handshake()
    await driver_b.handshake()

    async with tester.dispatcher_pair(alice, bob) as (alice_dispatcher, _):
        async with tester.dispatcher_pair(alice, carol):
            request = OutboundMessage(
                PingMessage(b"\x12", alice.enr.sequence_number),
                bob.endpoint,
                bob.node_id,
            )
            async with alice_dispatcher.subscribe_request(
                request, PongMessage
            ) as subscription:
                await driver_a.initiator.send_ping(b"\x12")
                await driver_b.initiator.send_ping(b"\x34")
                await driver_b.recipient.send_pong(b"\x34")
                await driver_a.recipient.send_pong(b"\x12")

                with trio.fail_after(1):
                    response = await subscription.receive()

    assert response.sender_node_id == bob.node_id
    assert response.message.request_id == b"\x12"

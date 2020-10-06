from typing import Tuple

from ddht.constants import DISCOVERY_MAX_PACKET_SIZE

SESSION_IDLE_TIMEOUT = 60

ROUTING_TABLE_KEEP_ALIVE = 300

REQUEST_RESPONSE_TIMEOUT = 10

# safe upper bound on the size of the ENR list in a nodes message
FOUND_NODES_MAX_PAYLOAD_SIZE = DISCOVERY_MAX_PACKET_SIZE - 200


DEFAULT_BOOTNODES: Tuple[str, ...] = (
    "enr:-IS4QNIktXW8LPFA2B5n8jbF6fwScqUnO59gyZyg7CExFPHOO5z7nHBUjqbtbuS7Mk6Z2TL3eZiECpGmYCeGPlJzrLIDgmlkgnY0gmlwhC1PSnGJc2VjcDI1NmsxoQLvfEFi6FaFI7bp7Cw8yfZ17AdDwceRSQH7BxL5VhUNd4N1ZHCCdl8",  # noqa: E501
    "enr:-IS4QKcAHi77_OQBuGolVX-I1dmQxyuZAsSTh3Z7Jck3LrzbYQ2NXzMEKvpit0cyH2coB55ddVDvKA8p5IUcg7DLQj4DgmlkgnY0gmlwhC1PW26Jc2VjcDI1NmsxoQPNz0D8sSVKyNTZuGRTTnPabutpJ8IUxpAyMqrVosZ14IN1ZHCCdl8",  # noqa: E501
)


PACKET_VERSION_1 = b"\x00\x01"

ID_NONCE_SIGNATURE_PREFIX = b"discovery v5 identity proof"

HEADER_PACKET_SIZE = 23

PROTOCOL_ID = b"discv5"

WHO_ARE_YOU_PACKET_SIZE = 24

HANDSHAKE_HEADER_PACKET_SIZE = 34

MESSAGE_PACKET_SIZE = 32

from typing import Any, Dict, List, Optional, Union

from eth_abi.abi import encode_single
from eth_abi.packed import encode_single_packed
from eth_typing import ChecksumAddress as AddressType
from eth_typing import HexStr
from eth_utils import encode_hex, keccak
from ethpm_types import (
    ABI,
    Bytecode,
    Checksum,
    Compiler,
    ContractType,
    PackageManifest,
    PackageMeta,
    Source,
)
from ethpm_types.abi import EventABI
from hexbytes import HexBytes
from pydantic import BaseModel, root_validator, validator
from web3.types import FilterParams

from ape._compat import Literal

from .signatures import MessageSignature, SignableMessage, TransactionSignature

BlockID = Union[int, HexStr, HexBytes, Literal["earliest", "latest", "pending"]]
"""
An ID that can match a block, such as the literals ``"earliest"``, ``"latest"``, or ``"pending"``
as well as a block number or hash (HexBytes).
"""

SnapshotID = Union[str, int, bytes]
"""
An ID representing a point in time on a blockchain, as used in the
:meth:`~ape.managers.chain.ChainManager.snapshot` and
:meth:`~ape.managers.chain.ChainManager.snapshot` methods. Can be a ``str``, ``int``, or ``bytes``.
Providers will expect and handle snapshot IDs differently. There shouldn't be a need to change
providers when using this feature, so there should not be confusion over this type in practical use
cases.
"""

RawAddress = Union[str, int, HexBytes]
"""
A raw data-type representation of an address.
"""


TopicFilter = List[Union[Optional[HexStr], List[Optional[HexStr]]]]


class LogFilter(BaseModel):
    addresses: List[AddressType] = []
    events: List[EventABI] = []
    topic_filter: TopicFilter = []
    start_block: int = 0
    stop_block: Optional[int] = None  # Use block height
    selectors: Dict[str, EventABI] = {}

    @root_validator()
    def compute_selectors(cls, values):
        values["selectors"] = {
            encode_hex(keccak(text=event.selector)): event for event in values["events"]
        }

        return values

    @validator("start_block", pre=True)
    def validate_start_block(cls, value):
        return value or 0

    @validator("addresses", pre=True, each_item=True)
    def validate_addresses(cls, value):
        from ape import convert

        return convert(value, AddressType)

    def dict(self, client=None):
        return FilterParams(
            address=self.addresses,
            fromBlock=hex(self.start_block),
            toBlock=hex(self.stop_block),
            topics=self.topic_filter,  # type: ignore
        )

    @classmethod
    def from_event(
        cls,
        event: EventABI,
        search_topics: Optional[Dict[str, Any]] = None,
        addresses: List[AddressType] = None,
        start_block=None,
        stop_block=None,
    ):
        """
        Construct a log filter from an event topic query.
        """
        from ape import convert
        from ape.utils.abi import LogInputABICollection, is_dynamic_sized_type

        if hasattr(event, "abi"):
            event = event.abi  # type: ignore

        search_topics = search_topics or {}
        topic_filter: List[Optional[HexStr]] = [encode_hex(keccak(text=event.selector))]
        abi_inputs = LogInputABICollection(event)

        def encode_topic_value(abi_type, value):
            if isinstance(value, (list, tuple)):
                return [encode_topic_value(abi_type, v) for v in value]
            elif is_dynamic_sized_type(abi_type):
                return encode_hex(keccak(encode_single_packed(str(abi_type)), value))
            elif abi_type == "address":
                value = convert(value, AddressType)
            return encode_hex(encode_single(abi_type, value))

        for topic in abi_inputs.topics:
            if topic.name in search_topics:
                encoded_value = encode_topic_value(topic.type, search_topics[topic.name])
                topic_filter.append(encoded_value)
            else:
                topic_filter.append(None)

        topic_names = [i.name for i in abi_inputs.topics if i.name]
        invalid_topics = set(search_topics) - set(topic_names)
        if invalid_topics:
            raise ValueError(
                f"{event.name} defines {', '.join(topic_names)} as indexed topics, "
                f"but you provided {', '.join(invalid_topics)}"
            )

        # remove trailing wildcards since they have no effect
        while topic_filter[-1] is None:
            topic_filter.pop()

        return cls(
            addresses=addresses or [],
            events=[event],
            topic_filter=topic_filter,
            start_block=start_block,
            stop_block=stop_block,
        )


class ContractLog(BaseModel):
    """
    An instance of a log from a contract.
    """

    name: str
    """The name of the event."""

    contract_address: AddressType
    """The contract responsible for emitting the log."""

    event_arguments: Dict[str, Any]
    """The arguments to the event, including both indexed and non-indexed data."""

    transaction_hash: Any
    """The hash of the transaction containing this log."""

    block_number: int
    """The number of the block containing the transaction that produced this log."""

    block_hash: Any
    """The hash of the block containing the transaction that produced this log."""

    log_index: int
    """The index of the log on the transaction."""

    def __str__(self) -> str:
        args = " ".join(f"{key}={val}" for key, val in self.event_arguments.items())
        return f"{self.name} {args}"

    def __getattr__(self, item: str) -> Any:
        """
        Access properties from the log via ``.`` access.

        Args:
            item (str): The name of the property.
        """

        try:
            normal_attribute = self.__getattribute__(item)
            return normal_attribute
        except AttributeError:
            pass

        if item not in self.event_arguments:
            raise AttributeError(f"{self.__class__.__name__} has no attribute '{item}'.")

        return self.event_arguments[item]

    def __contains__(self, item: str) -> bool:
        return item in self.event_arguments

    def __getitem__(self, item: str) -> Any:
        return self.event_arguments[item]

    def get(self, item: str, default: Optional[Any] = None) -> Any:
        return self.event_arguments.get(item, default)


__all__ = [
    "ABI",
    "AddressType",
    "BlockID",
    "Bytecode",
    "Checksum",
    "Compiler",
    "ContractLog",
    "ContractType",
    "MessageSignature",
    "PackageManifest",
    "PackageMeta",
    "SignableMessage",
    "SnapshotID",
    "Source",
    "TransactionSignature",
]

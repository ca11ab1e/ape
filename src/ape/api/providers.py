import atexit
import ctypes
import logging
import platform
import shutil
import sys
import time
from abc import ABC
from concurrent.futures import ThreadPoolExecutor
from logging import FileHandler, Formatter, Logger, getLogger
from pathlib import Path
from signal import SIGINT, SIGTERM, signal
from subprocess import PIPE, Popen
from typing import Any, Iterator, List, Optional

from eth_typing import HexStr
from eth_utils import add_0x_prefix
from evm_trace import CallTreeNode, TraceFrame
from hexbytes import HexBytes
from pydantic import Field, root_validator, validator
from web3 import Web3
from web3.exceptions import ContractLogicError as Web3ContractLogicError

from ape.api.config import PluginConfig
from ape.api.networks import LOCAL_NETWORK_NAME, NetworkAPI
from ape.api.query import BlockTransactionQuery
from ape.api.transactions import ReceiptAPI, TransactionAPI
from ape.exceptions import (
    APINotImplementedError,
    ContractLogicError,
    ProviderError,
    ProviderNotConnectedError,
    RPCTimeoutError,
    SubprocessError,
    SubprocessTimeoutError,
    TransactionError,
    VirtualMachineError,
)
from ape.logging import logger
from ape.types import AddressType, BlockID, ContractLog, LogFilter, SnapshotID
from ape.utils import (
    EMPTY_BYTES32,
    BaseInterfaceModel,
    JoinableQueue,
    abstractmethod,
    cached_property,
    gas_estimation_error_message,
    raises_not_implemented,
    spawn,
)


class BlockAPI(BaseInterfaceModel):
    """
    An abstract class representing a block and its attributes.
    """

    num_transactions: int = 0
    hash: Optional[Any] = None  # NOTE: pending block does not have a hash
    number: Optional[int] = None
    parent_hash: Any = Field(
        EMPTY_BYTES32, alias="parentHash"
    )  # NOTE: genesis block has no parent hash
    size: int
    timestamp: int

    @root_validator(pre=True)
    def convert_parent_hash(cls, data):
        if "parent_hash" in data:
            parent_hash = data["parent_hash"]
        elif "parentHash" in data:
            parent_hash = data["parentHash"]
        else:
            parent_hash = EMPTY_BYTES32

        data["parentHash"] = parent_hash or EMPTY_BYTES32
        return data

    @validator("hash", "parent_hash", pre=True)
    def validate_hexbytes(cls, value):
        # NOTE: pydantic treats these values as bytes and throws an error
        if value and not isinstance(value, HexBytes):
            raise ValueError(f"Hash `{value}` is not a valid Hexbyte.")
        return value

    @cached_property
    def transactions(self) -> List[TransactionAPI]:
        query = BlockTransactionQuery(columns=["*"], block_id=self.hash)
        return list(self.query_manager.query(query))  # type: ignore


class ProviderAPI(BaseInterfaceModel):
    """
    An abstraction of a connection to a network in an ecosystem. Example ``ProviderAPI``
    implementations include the `ape-infura <https://github.com/ApeWorX/ape-infura>`__
    plugin or the `ape-hardhat <https://github.com/ApeWorX/ape-hardhat>`__ plugin.
    """

    name: str
    """The name of the provider (should be the plugin name)."""

    network: NetworkAPI
    """A reference to the network this provider provides."""

    provider_settings: dict
    """The settings for the provider, as overrides to the configuration."""

    data_folder: Path
    """The path to the  ``.ape`` directory."""

    request_header: dict
    """A header to set on HTTP/RPC requests."""

    cached_chain_id: Optional[int] = None
    """Implementation providers may use this to cache and re-use chain ID."""

    block_page_size: int = 100
    """
    The amount of blocks to fetch in a response, as a default.
    This is particularly useful for querying logs across a block range.
    """

    concurrency: int = 4
    """
    How many parallel threads to use when fetching logs.
    """

    @abstractmethod
    def connect(self):
        """
        Connect a to a provider, such as start-up a process or create an HTTP connection.
        """

    @abstractmethod
    def disconnect(self):
        """
        Disconnect from a provider, such as tear-down a process or quit an HTTP session.
        """

    @abstractmethod
    def update_settings(self, new_settings: dict):
        """
        Change a provider's setting, such as configure a new port to run on.
        May require a reconnect.

        Args:
            new_settings (dict): The new provider settings.
        """

    @property
    @abstractmethod
    def chain_id(self) -> int:
        """
        The blockchain ID.
        See `ChainList <https://chainlist.org/>`__ for a comprehensive list of IDs.
        """

    @abstractmethod
    def get_balance(self, address: str) -> int:
        """
        Get the balance of an account.

        Args:
            address (str): The address of the account.

        Returns:
            int: The account balance.
        """

    @abstractmethod
    def get_code(self, address: str) -> bytes:
        """
        Get the bytes a contract.

        Args:
            address (str): The address of the contract.

        Returns:
            bytes: The contract byte-code.
        """

    @raises_not_implemented
    def get_storage_at(self, address: str, slot: int) -> bytes:
        """
        Gets the raw value of a storage slot of a contract.

        Args:
            address (str): The address of the contract.
            slot (int): Storage slot to read the value of.

        Returns:
            bytes: The value of the storage slot.
        """

    @abstractmethod
    def get_nonce(self, address: str) -> int:
        """
        Get the number of times an account has transacted.

        Args:
            address (str): The address of the account.

        Returns:
            int
        """

    @abstractmethod
    def estimate_gas_cost(self, txn: TransactionAPI) -> int:
        """
        Estimate the cost of gas for a transaction.

        Args:
            txn (:class:`~ape.api.transactions.TransactionAPI`):
                The transaction to estimate the gas for.

        Returns:
            int: The estimated cost of gas.
        """

    @property
    @abstractmethod
    def gas_price(self) -> int:
        """
        The price for what it costs to transact
        (pre-`EIP-1559 <https://eips.ethereum.org/EIPS/eip-1559>`__).
        """

    @property
    def config(self) -> PluginConfig:
        """
        The provider's configuration.
        """
        return self.config_manager.get_config(self.name)

    @property
    def priority_fee(self) -> int:
        """
        A miner tip to incentivize them to include your transaction in a block.

        Raises:
            NotImplementedError: When the provider does not implement
              `EIP-1559 <https://eips.ethereum.org/EIPS/eip-1559>`__ typed transactions.
        """
        raise NotImplementedError("priority_fee is not implemented by this provider")

    @property
    def base_fee(self) -> int:
        """
        The minimum value required to get your transaction included on the next block.
        Only providers that implement `EIP-1559 <https://eips.ethereum.org/EIPS/eip-1559>`__
        will use this property.

        Raises:
            NotImplementedError: When this provider does not implement
              `EIP-1559 <https://eips.ethereum.org/EIPS/eip-1559>`__.
        """
        raise NotImplementedError("base_fee is not implemented by this provider")

    @abstractmethod
    def get_block(self, block_id: BlockID) -> BlockAPI:
        """
        Get a block.

        Args:
            block_id (:class:`~ape.types.BlockID`): The ID of the block to get.
                Can be ``"latest"``, ``"earliest"``, ``"pending"``, a block hash or a block number.

        Returns:
            :class:`~ape.types.BlockID`: The block for the given ID.
        """

    @abstractmethod
    def send_call(self, txn: TransactionAPI) -> bytes:  # Return value of function
        """
        Execute a new transaction call immediately without creating a
        transaction on the block chain.

        Args:
            txn: :class:`~ape.api.transactions.TransactionAPI`

        Returns:
            str: The result of the transaction call.
        """

    @abstractmethod
    def get_transaction(self, txn_hash: str) -> ReceiptAPI:
        """
        Get the information about a transaction from a transaction hash.

        Args:
            txn_hash (str): The hash of the transaction to retrieve.

        Returns:
            :class:`~api.providers.ReceiptAPI`:
            The receipt of the transaction with the given hash.
        """

    @abstractmethod
    def get_transactions_by_block(self, block_id: BlockID) -> Iterator[TransactionAPI]:
        """
        Get the information about a set of transactions from a block.

        Args:
            block_id (:class:`~ape.types.BlockID`): The ID of the block.

        Returns:
            Iterator[:class: `~ape.api.transactions.TransactionAPI`]
        """

    @abstractmethod
    def send_transaction(self, txn: TransactionAPI) -> ReceiptAPI:
        """
        Send a transaction to the network.

        Args:
            txn (:class:`~ape.api.transactions.TransactionAPI`): The transaction to send.

        Returns:
            :class:`~ape.api.transactions.ReceiptAPI`
        """

    @abstractmethod
    def get_contract_logs(self, log_filter: LogFilter) -> Iterator[ContractLog]:
        """
        Get logs from contracts.

        Args:
            log_filter (:class:`~ape.types.LogFilter`): A mapping of event ABIs to
              topic filters. Defaults to getting all events.

        Returns:
            Iterator[:class:`~ape.types.ContractLog`]
        """

    @raises_not_implemented
    def snapshot(self) -> SnapshotID:
        """
        Defined to make the ``ProviderAPI`` interchangeable with a
        :class:`~ape.api.providers.TestProviderAPI`, as in
        :class:`ape.managers.chain.ChainManager`.

        Raises:
            NotImplementedError: Unless overridden.
        """

    @raises_not_implemented
    def revert(self, snapshot_id: SnapshotID):
        """
        Defined to make the ``ProviderAPI`` interchangeable with a
        :class:`~ape.api.providers.TestProviderAPI`, as in
        :class:`ape.managers.chain.ChainManager`.

        Raises:
            NotImplementedError: Unless overridden.
        """

    @raises_not_implemented
    def set_timestamp(self, new_timestamp: int):
        """
        Defined to make the ``ProviderAPI`` interchangeable with a
        :class:`~ape.api.providers.TestProviderAPI`, as in
        :class:`ape.managers.chain.ChainManager`.

        Raises:
            NotImplementedError: Unless overridden.
        """

    @raises_not_implemented
    def mine(self, num_blocks: int = 1):
        """
        Defined to make the ``ProviderAPI`` interchangeable with a
        :class:`~ape.api.providers.TestProviderAPI`, as in
        :class:`ape.managers.chain.ChainManager`.

        Raises:
            NotImplementedError: Unless overridden.
        """

    @raises_not_implemented
    def set_balance(self, address: AddressType, amount: int):
        """
        Change the balance of an account.

        Args:
            address (AddressType): An address on the network.
            amount (int): The balance to set in the address.
        """

    def __repr__(self) -> str:
        return f"<{self.name} chain_id={self.chain_id}>"

    @raises_not_implemented
    def unlock_account(self, address: AddressType) -> bool:
        """
        Ask the provider to allow an address to submit transactions without validating
        signatures. This feature is intended to be subclassed by a
        :class:`~ape.api.providers.TestProviderAPI` so that during a fork-mode test,
        a transaction can be submitted by an arbitrary account or contract without a private key.

        Raises:
            NotImplementedError: When this provider does not support unlocking an account.

        Args:
            address (``AddressType``): The address to unlock.

        Returns:
            bool: ``True`` if successfully unlocked account and ``False`` otherwise.
        """

    @raises_not_implemented
    def get_transaction_trace(self, txn_hash: str) -> Iterator[TraceFrame]:
        """
        Provide a detailed description of opcodes.

        Args:
            txn_hash (str): The hash of a transaction to trace.

        Returns:
            Iterator(TraceFrame): Transaction execution trace object.
        """

    @raises_not_implemented
    def get_call_tree(self, txn_hash: str) -> CallTreeNode:
        """
        Create a tree structure of calls for a transaction.

        Args:
            txn_hash (str): The hash of a transaction to trace.

        Returns:
            CallTreeNode: Transaction execution call-tree objects.
        """

    def prepare_transaction(self, txn: TransactionAPI) -> TransactionAPI:
        """
        Set default values on the transaction.

        Raises:
            :class:`~ape.exceptions.TransactionError`: When given negative required confirmations.

        Args:
            txn (:class:`~ape.api.transactions.TransactionAPI`): The transaction to prepare.

        Returns:
            :class:`~ape.api.transactions.TransactionAPI`
        """

        # NOTE: Use "expected value" for Chain ID, so if it doesn't match actual, we raise
        txn.chain_id = self.network.chain_id

        from ape_ethereum.transactions import TransactionType

        txn_type = TransactionType(txn.type)
        if txn_type == TransactionType.STATIC and txn.gas_price is None:  # type: ignore
            txn.gas_price = self.gas_price  # type: ignore
        elif txn_type == TransactionType.DYNAMIC:
            if txn.max_priority_fee is None:  # type: ignore
                txn.max_priority_fee = self.priority_fee  # type: ignore

            if txn.max_fee is None:
                txn.max_fee = self.base_fee + txn.max_priority_fee
            # else: Assume user specified the correct amount or txn will fail and waste gas

        if txn.gas_limit is None:
            txn.gas_limit = self.estimate_gas_cost(txn)
        # else: Assume user specified the correct amount or txn will fail and waste gas

        if txn.required_confirmations is None:
            txn.required_confirmations = self.network.required_confirmations
        elif not isinstance(txn.required_confirmations, int) or txn.required_confirmations < 0:
            raise TransactionError(message="'required_confirmations' must be a positive integer.")

        return txn

    def _try_track_receipt(self, receipt: ReceiptAPI):
        if self.chain_manager:
            self.chain_manager.account_history.append(receipt)

    def get_virtual_machine_error(self, exception: Exception) -> VirtualMachineError:
        """
        Get a virtual machine error from an error returned from your RPC.
        If from a contract revert / assert statement, you will be given a
        special :class:`~ape.exceptions.ContractLogicError` that can be
        checked in ``ape.reverts()`` tests.

        **NOTE**: The default implementation is based on ``geth`` output.
        ``ProviderAPI`` implementations override when needed.

        Args:
            exception (Exception): The error returned from your RPC client.

        Returns:
            :class:`~ape.exceptions.VirtualMachineError`: An error representing what
               went wrong in the call.
        """

        if isinstance(exception, Web3ContractLogicError):
            # This happens from `assert` or `require` statements.
            message = str(exception).split(":")[-1].strip()
            if message == "execution reverted":
                # Reverted without an error message
                raise ContractLogicError()

            return ContractLogicError(revert_message=message)

        if not len(exception.args):
            return VirtualMachineError(base_err=exception)

        err_data = exception.args[0] if (hasattr(exception, "args") and exception.args) else None
        if not isinstance(err_data, dict):
            return VirtualMachineError(base_err=exception)

        err_msg = err_data.get("message")
        if not err_msg:
            return VirtualMachineError(base_err=exception)

        return VirtualMachineError(message=str(err_msg), code=err_data.get("code"))


class TestProviderAPI(ProviderAPI):
    """
    An API for providers that have development functionality, such as snapshotting.
    """

    @cached_property
    def test_config(self) -> PluginConfig:
        return self.config_manager.get_config("test")

    @abstractmethod
    def snapshot(self) -> SnapshotID:
        """
        Record the current state of the blockchain with intent to later
        call the method :meth:`~ape.managers.chain.ChainManager.revert`
        to go back to this point. This method is for local networks only.

        Returns:
            :class:`~ape.types.SnapshotID`: The snapshot ID.
        """

    @abstractmethod
    def revert(self, snapshot_id: SnapshotID):
        """
        Regress the current call using the given snapshot ID.
        Allows developers to go back to a previous state.

        Args:
            snapshot_id (str): The snapshot ID.
        """

    @abstractmethod
    def set_timestamp(self, new_timestamp: int):
        """
        Change the pending timestamp.

        Args:
            new_timestamp (int): The timestamp to set.

        Returns:
            int: The new timestamp.
        """

    @abstractmethod
    def mine(self, num_blocks: int = 1):
        """
        Advance by the given number of blocks.

        Args:
            num_blocks (int): The number of blocks allotted to mine. Defaults to ``1``.
        """


class Web3Provider(ProviderAPI, ABC):
    """
    A base provider mixin class that uses the
    [web3.py](https://web3py.readthedocs.io/en/stable/) python package.
    """

    _web3: Optional[Web3] = None
    _client_version: Optional[str] = None

    @property
    def web3(self) -> Web3:
        if not self._web3:
            raise ProviderNotConnectedError()

        return self._web3

    @property
    def client_version(self) -> str:
        if not self._web3:
            return ""

        # NOTE: Gets reset to `None` on `connect()` and `disconnect()`.
        if self._client_version is None:
            self._client_version = self._web3.clientVersion

        return self._client_version

    @property
    def base_fee(self) -> int:
        block = self.get_block("latest")
        if not hasattr(block, "base_fee"):
            raise APINotImplementedError("No base fee found in block.")
        else:
            base_fee = block.base_fee  # type: ignore

        if base_fee is None:
            # Non-EIP-1559 chains or we time-travelled pre-London fork.
            raise APINotImplementedError("base_fee is not implemented by this provider.")

        return base_fee

    def update_settings(self, new_settings: dict):
        self.disconnect()
        self.provider_settings.update(new_settings)
        self.connect()

    def estimate_gas_cost(self, txn: TransactionAPI) -> int:
        txn_dict = txn.dict()
        try:
            return self._web3.eth.estimate_gas(txn_dict)  # type: ignore
        except ValueError as err:
            tx_error = self.get_virtual_machine_error(err)

            # If this is the cause of a would-be revert,
            # raise ContractLogicError so that we can confirm tx-reverts.
            if isinstance(tx_error, ContractLogicError):
                raise tx_error from err

            message = gas_estimation_error_message(tx_error)
            raise TransactionError(base_err=tx_error, message=message) from err

    @property
    def chain_id(self) -> int:
        if self.network.name != LOCAL_NETWORK_NAME and not self.network.name.endswith("-fork"):
            # If using a live network, the chain ID is hardcoded.
            return self.network.chain_id

        elif hasattr(self.web3, "eth"):
            return self.web3.eth.chain_id

        else:
            raise ProviderNotConnectedError()

    @property
    def gas_price(self) -> int:
        return self._web3.eth.generate_gas_price()  # type: ignore

    @property
    def priority_fee(self) -> int:
        return self.web3.eth.max_priority_fee

    def get_block(self, block_id: BlockID) -> BlockAPI:
        if isinstance(block_id, str) and block_id.isnumeric():
            block_id = int(block_id)
        block_data = dict(self.web3.eth.get_block(block_id))
        return self.network.ecosystem.decode_block(block_data)

    def get_nonce(self, address: str) -> int:
        return self.web3.eth.get_transaction_count(address)  # type: ignore

    def get_balance(self, address: str) -> int:
        return self.web3.eth.get_balance(address)  # type: ignore

    def get_code(self, address: str) -> bytes:
        return self.web3.eth.get_code(address)  # type: ignore

    def get_storage_at(self, address: str, slot: int) -> bytes:
        return self.web3.eth.get_storage_at(address, slot)  # type: ignore

    def send_call(self, txn: TransactionAPI) -> bytes:
        try:
            return self.web3.eth.call(txn.dict())
        except ValueError as err:
            raise self.get_virtual_machine_error(err) from err

    def get_transaction(self, txn_hash: str, required_confirmations: int = 0) -> ReceiptAPI:
        if required_confirmations < 0:
            raise TransactionError(message="Required confirmations cannot be negative.")

        timeout = self.config_manager.transaction_acceptance_timeout
        receipt_data = self.web3.eth.wait_for_transaction_receipt(
            HexBytes(txn_hash), timeout=timeout
        )
        txn = self.web3.eth.get_transaction(txn_hash)  # type: ignore
        receipt = self.network.ecosystem.decode_receipt(
            {
                "provider": self,
                "required_confirmations": required_confirmations,
                **txn,
                **receipt_data,
            }
        )
        return receipt.await_confirmations()

    def get_transactions_by_block(self, block_id: BlockID) -> Iterator:
        if isinstance(block_id, str):
            block_id = HexStr(block_id)

            if block_id.isnumeric():
                block_id = add_0x_prefix(block_id)

        block = self.web3.eth.get_block(block_id, full_transactions=True)
        for transaction in block.get("transactions"):  # type: ignore
            yield self.network.ecosystem.create_transaction(**transaction)  # type: ignore

    def block_ranges(self, start=0, stop=None, page=None):
        if stop is None:
            stop = self.chain_manager.blocks.height
        if page is None:
            page = self.chain_manager.provider.block_page_size

        for start_block in range(start, stop + 1, page):
            stop_block = min(stop, start_block + page - 1)
            yield start_block, stop_block

    def get_contract_logs(self, log_filter: LogFilter) -> Iterator[ContractLog]:
        height = self.chain_manager.blocks.height
        start_block = log_filter.start_block
        stop_block = min(log_filter.stop_block or height, height)
        block_ranges = self.block_ranges(start_block, stop_block, self.block_page_size)

        def fetch_log_page(block_range):
            start, stop = block_range
            page_filter = log_filter.copy(update=dict(start_block=start, stop_block=stop))
            # eth-tester expects a different format, let web3 handle the conversions for it
            raw = "EthereumTester" not in self.client_version
            logs = self._get_logs(page_filter.dict(), raw)
            return self.network.ecosystem.decode_logs(log_filter.events, logs)

        with ThreadPoolExecutor(self.concurrency) as pool:
            for page in pool.map(fetch_log_page, block_ranges):
                yield from page

    def _get_logs(self, filter_params, raw=True):
        if raw:
            response = self.web3.provider.make_request("eth_getLogs", [filter_params])
            if "error" in response:
                raise ValueError(response["error"]["message"])
            return response["result"]
        else:
            return self.web3.eth.get_logs(filter_params)

    def send_transaction(self, txn: TransactionAPI) -> ReceiptAPI:
        try:
            txn_hash = self.web3.eth.send_raw_transaction(txn.serialize_transaction())
        except ValueError as err:
            raise self.get_virtual_machine_error(err) from err

        required_confirmations = (
            txn.required_confirmations
            if txn.required_confirmations is not None
            else self.network.required_confirmations
        )

        receipt = self.get_transaction(
            txn_hash.hex(), required_confirmations=required_confirmations
        )
        receipt.raise_for_status()
        logger.info(f"Confirmed {receipt.txn_hash} (total fees paid = {receipt.total_fees_paid})")
        self._try_track_receipt(receipt)
        return receipt


class UpstreamProvider(ProviderAPI):
    """
    A provider that can also be set as another provider's upstream.
    """

    @property
    @abstractmethod
    def connection_str(self) -> str:
        """
        The str used by downstream providers to connect to this one.
        For example, the URL for HTTP-based providers.
        """


class SubprocessProvider(ProviderAPI):
    """
    A provider that manages a process, such as for ``ganache``.
    """

    PROCESS_WAIT_TIMEOUT = 15
    process: Optional[Popen] = None
    is_stopping: bool = False

    stdout_queue: Optional[JoinableQueue] = None
    stderr_queue: Optional[JoinableQueue] = None

    @property
    @abstractmethod
    def process_name(self) -> str:
        """The name of the process, such as ``Hardhat node``."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """
        ``True`` if the process is running and connected.
        ``False`` otherwise.
        """

    @abstractmethod
    def build_command(self) -> List[str]:
        """
        Get the command as a list of ``str``.
        Subclasses should override and add command arguments if needed.

        Returns:
            List[str]: The command to pass to ``subprocess.Popen``.
        """

    @property
    def base_logs_path(self) -> Path:
        return self.config_manager.DATA_FOLDER / self.name / "subprocess_output"

    @property
    def stdout_logs_path(self) -> Path:
        return self.base_logs_path / "stdout.log"

    @property
    def stderr_logs_path(self) -> Path:
        return self.base_logs_path / "stderr.log"

    @cached_property
    def _stdout_logger(self) -> Logger:
        return self._make_logger("stdout", self.stdout_logs_path)

    @cached_property
    def _stderr_logger(self) -> Logger:
        return self._make_logger("stderr", self.stderr_logs_path)

    def _make_logger(self, name: str, path: Path):
        logger = getLogger(f"{self.name}_{name}_subprocessProviderLogger")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            path.unlink()

        path.touch()
        handler = FileHandler(str(path))
        handler.setFormatter(Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def connect(self):
        """
        Start the process and connect to it.
        Subclasses handle the connection-related tasks.
        """

        if self.is_connected:
            raise ProviderError("Cannot connect twice. Call disconnect before connecting again.")

        # Register atexit handler to make sure disconnect is called for normal object lifecycle.
        atexit.register(self.disconnect)

        # Register handlers to ensure atexit handlers are called when Python dies.
        def _signal_handler(signum, frame):
            atexit._run_exitfuncs()
            sys.exit(143 if signum == SIGTERM else 130)

        signal(SIGINT, _signal_handler)
        signal(SIGTERM, _signal_handler)

    def disconnect(self):
        """Stop the process if it exists.
        Subclasses override this method to do provider-specific disconnection tasks.
        """

        self.cached_chain_id = None
        if self.process:
            self.stop()

    def start(self, timeout: int = 20):
        """Start the process and wait for its RPC to be ready."""

        if self.is_connected:
            logger.info(f"Connecting to existing '{self.process_name}' process.")
            self.process = None  # Not managing the process.
        else:
            logger.info(f"Starting '{self.process_name}' process.")
            pre_exec_fn = _linux_set_death_signal if platform.uname().system == "Linux" else None
            self.stderr_queue = JoinableQueue()
            self.stdout_queue = JoinableQueue()
            self.process = Popen(
                self.build_command(), preexec_fn=pre_exec_fn, stdout=PIPE, stderr=PIPE
            )
            spawn(self.produce_stdout_queue)
            spawn(self.produce_stderr_queue)
            spawn(self.consume_stdout_queue)
            spawn(self.consume_stderr_queue)

            with RPCTimeoutError(self, seconds=timeout) as _timeout:
                while True:
                    if self.is_connected:
                        break

                    time.sleep(0.1)
                    _timeout.check()

    def produce_stdout_queue(self):
        for line in iter(self.process.stdout.readline, b""):
            self.stdout_queue.put(line)
            time.sleep(0)

    def produce_stderr_queue(self):
        for line in iter(self.process.stderr.readline, b""):
            self.stderr_queue.put(line)
            time.sleep(0)

    def consume_stdout_queue(self):
        for line in self.stdout_queue:
            output = line.decode("utf8").strip()
            logger.debug(output)
            self._stdout_logger.info(output)
            self.stdout_queue.task_done()
            time.sleep(0)

    def consume_stderr_queue(self):
        for line in self.stderr_queue:
            logger.debug(line.decode("utf8").strip())
            self._stdout_logger.info(line)
            self.stderr_queue.task_done()
            time.sleep(0)

    def stop(self):
        """Kill the process."""

        if not self.process or self.is_stopping:
            return

        self.is_stopping = True
        logger.info(f"Stopping '{self.process_name}' process.")
        self._kill_process()
        self.is_stopping = False
        self.process = None
        self.stdout_queue = None
        self.stderr_queue = None

    def _wait_for_popen(self, timeout: int = 30):
        if not self.process:
            # Mostly just to make mypy happy.
            raise SubprocessError("Unable to wait for process. It is not set yet.")

        try:
            with SubprocessTimeoutError(self, seconds=timeout) as _timeout:
                while self.process.poll() is None:
                    time.sleep(0.1)
                    _timeout.check()

        except SubprocessTimeoutError:
            pass

    def _kill_process(self):
        if platform.uname().system == "Windows":
            self._windows_taskkill()
            return

        warn_prefix = f"Trying to close '{self.process_name}' process."

        def _try_close(warn_message):
            try:
                self.process.send_signal(SIGINT)
                self._wait_for_popen(self.PROCESS_WAIT_TIMEOUT)
            except KeyboardInterrupt:
                logger.warning(warn_message)

        try:
            if self.process.poll() is None:
                _try_close(f"{warn_prefix}. Press Ctrl+C 1 more times to force quit")

            if self.process.poll() is None:
                self.process.kill()
                self._wait_for_popen(2)

        except KeyboardInterrupt:
            self.process.kill()

        self.process = None

    def _windows_taskkill(self) -> None:
        """
        Kills the given process and all child processes using taskkill.exe. Used
        for subprocesses started up on Windows which run in a cmd.exe wrapper that
        doesn't propagate signals by default (leaving orphaned processes).
        """
        process = self.process
        if not process:
            return

        taskkill_bin = shutil.which("taskkill")
        if not taskkill_bin:
            raise SubprocessError("Could not find taskkill.exe executable.")

        proc = Popen(
            [
                taskkill_bin,
                "/F",  # forcefully terminate
                "/T",  # terminate child processes
                "/PID",
                str(process.pid),
            ]
        )
        proc.wait(timeout=self.PROCESS_WAIT_TIMEOUT)


def _linux_set_death_signal():
    """
    Automatically sends SIGTERM to child subprocesses when parent process
    dies (only usable on Linux).
    """
    # from: https://stackoverflow.com/a/43152455/75956
    # the first argument, 1, is the flag for PR_SET_PDEATHSIG
    # the second argument is what signal to send to child subprocesses
    libc = ctypes.CDLL("libc.so.6")
    return libc.prctl(1, SIGTERM)

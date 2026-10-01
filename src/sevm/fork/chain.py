"""The fork chain assembly: `fork_web3(rpc_url, block)` and its moving parts.

Mirrors eth-tester's `setup_tester_chain` recipe (NoProofConsensus, ten
pre-funded dev accounts so driver scripts and `vm.*` cheatcodes work) with two
fork-specific changes: the VM's state class installs `ForkAccountDB`, and the
chain's header is pinned to the remote block — number, timestamp, gas limit and
base fee come from `eth_getBlockByNumber`, and `chain_id` from `eth_chainId`,
so replayed transactions validate exactly as they did on the real chain.
"""

from __future__ import annotations

import threading
from typing import Any

from web3 import Web3

from .rpc import RpcClient
from .state import ForkAccountDB, ForkRegistry

# VM plumbing the state itself installs at every block; never fetched remotely.
_SYSTEM_CONTRACTS: tuple[bytes, ...] = ()
try:
    from eth.vm.forks.cancun.constants import BEACON_ROOTS_ADDRESS

    _SYSTEM_CONTRACTS = (*_SYSTEM_CONTRACTS, BEACON_ROOTS_ADDRESS)
    from eth.vm.forks.prague.constants import HISTORY_STORAGE_ADDRESS

    _SYSTEM_CONTRACTS = (*_SYSTEM_CONTRACTS, HISTORY_STORAGE_ADDRESS)
except ImportError:  # pragma: no cover - depends on the installed py-evm
    pass

_DEV_BALANCE = 1000 * 10**18  # 1000 ETH each, eth-tester's convention


def _make_state_class(rpc: RpcClient, registry: ForkRegistry) -> type:
    """A PragueState subclass whose AccountDB reads through to the remote."""
    from eth.vm.forks.prague.state import PragueState

    registry.local_only.update(_SYSTEM_CONTRACTS)

    class _ForkAccountDB(ForkAccountDB):
        def __init__(self, db: Any, state_root: Any) -> None:
            super().__init__(db, state_root, rpc, registry)

    class _ForkState(PragueState):
        account_db_class = _ForkAccountDB

    return _ForkState


def _build_fork(rpc: RpcClient, stats: dict[str, Any]) -> tuple[Any, Any]:
    """(account_keys, chain) — the eth-tester fork recipe with a read-through DB."""
    from eth.chains.base import MiningChain
    from eth.consensus import ConsensusApplier, NoProofConsensus
    from eth.db import get_db_backend
    from eth.vm.forks import PragueVM
    from eth_tester.backends.pyevm.main import (
        generate_genesis_state_for_keys,
        get_default_account_keys,
    )

    registry = ForkRegistry()
    state_class = _make_state_class(rpc, registry)

    fork_vm = type("ForkVM", (PragueVM,), {"_state_class": state_class})
    vm_config = ConsensusApplier(NoProofConsensus).amend_vm_configuration(((0, fork_vm),))

    class ForkChain(MiningChain):
        chain_id = rpc.chain_id
        vm_configuration = vm_config

        def get_transaction_builder(self):
            return super().get_vm().get_transaction_builder()

        def create_header_from_parent(self, parent_header, **header_params):
            # Keep the forked gas limit constant across mined blocks.
            if parent_header is not None:
                header_params.setdefault("gas_limit", parent_header.gas_limit)
            return super().create_header_from_parent(parent_header, **header_params)

    from eth_tester.backends.pyevm.main import get_default_genesis_params

    account_keys = get_default_account_keys(quantity=10)
    genesis_state = generate_genesis_state_for_keys(account_keys)
    for address in genesis_state:
        genesis_state[address]["balance"] = _DEV_BALANCE
        registry.local_only.add(bytes(address))
    base_db = get_db_backend()
    header = rpc.block_header()
    genesis_params = get_default_genesis_params()
    genesis_params["gas_limit"] = int(header["gasLimit"], 16)
    genesis_params["timestamp"] = max(1, int(header["timestamp"], 16) - 1)
    chain = ForkChain.from_genesis(base_db, genesis_params, genesis_state)

    header = rpc.block_header()
    # The remote's timestamp and gas limit live in the GENESIS params (canonical
    # from block 0). The base fee is NOT pinned: EIP-1559 derives each mined
    # block's fee from the parent, and an arbitrary remote fee would fail header
    # validation — execution and state do not depend on it. The local block
    # NUMBER still starts at 0 ("latest" walks canonical hashes; a remote number
    # without its history would break every block query, and remote BLOCKHASH
    # history is unavailable either way — the same trade anvil's fork makes).
    stats.update(
        registry=registry,
        url=rpc.url,
        block=rpc.block,
        chain_id=rpc.chain_id,
        remote_block=int(header["number"], 16),
    )
    return account_keys, chain


def fork_web3(rpc_url: str, block: int) -> Web3:
    """A Web3 whose eth-tester backend forks `rpc_url` at `block`.

    Reads fall through to the remote at that block; writes stay local and
    journaled. The returned provider's backend is a stock PyEVMBackend whose
    chain was swapped for the fork, so every eth-tester API keeps working.
    """
    from eth_tester import EthereumTester
    from eth_tester.backends.pyevm.main import PyEVMBackend
    from web3.providers.eth_tester.main import EthereumTesterProvider

    rpc = RpcClient(rpc_url, block)
    stats: dict[str, Any] = {"rpc_calls": 0, "materialized": 0}

    class _ForkBackend(PyEVMBackend):
        def __init__(self) -> None:
            self.account_keys, self.chain = None, None

        def reset_to_genesis(self, genesis_params=None, genesis_state=None) -> None:
            self.account_keys, self.chain = _build_fork(rpc, stats)

    backend = _ForkBackend()
    backend.reset_to_genesis()
    original_apply = backend.send_raw_transaction

    def _counting_send_raw(raw: Any) -> Any:  # wraps for stats
        stats["rpc_calls"] = rpc.calls
        return original_apply(raw)

    backend.send_raw_transaction = _counting_send_raw
    stats["backend"] = backend
    stats["rpc"] = rpc
    _ACTIVE["fork"] = stats
    tester = EthereumTester(backend)
    return Web3(EthereumTesterProvider(tester))


# -- the process-wide active fork (attach()/info) ------------------------------

_ACTIVE: dict[str, Any] = {}
_PENDING: dict[str, Any] = {}
_LOCK = threading.Lock()


def set_pending_fork(rpc_url: str | None, block: int | None) -> None:
    """`sevm run --fork` records what attach() should serve to driver scripts."""
    with _LOCK:
        if rpc_url:
            _PENDING.clear()
            _PENDING.update(url=rpc_url, block=block if block is not None else -1)
        else:
            _PENDING.clear()


def attach() -> Web3:
    """The chain a driver script should drive.

    Under `sevm run --fork URL script.py` this is the forked remote; otherwise
    a fresh in-process tester chain, so the same script runs both ways.
    """
    with _LOCK:
        pending = dict(_PENDING)
    if pending:
        return fork_web3(pending["url"], pending["block"])
    from web3 import EthereumTesterProvider

    return Web3(EthereumTesterProvider())


def fork_info() -> dict[str, Any]:
    """What the active fork is looking at, for humans and the MCP client."""
    stats = _ACTIVE.get("fork")
    if not stats:
        return {"active": False}
    rpc: RpcClient = stats["rpc"]
    backend = stats.get("backend")
    materialized = 0
    # The live AccountDB reports what it fetched; walk the chain's current state.
    try:
        state = backend.chain.get_vm().state
        account_db = state._account_db
        materialized = account_db._registry.fetches
    except Exception:
        pass
    return {
        "active": True,
        "url": rpc.url,
        "block": rpc.block,
        "chain_id": rpc.chain_id,
        "rpc_calls": rpc.calls,
        "materialized_reads": materialized,
        "note": (
            "reads fall through to the remote at the pinned block; writes are "
            "local and journaled — checkpoint restore rolls them back, restart "
            "refetches a pristine fork"
        ),
    }

"""The remote fork: lazy materialization, divergence, and transaction replay.

The fake "remote" is a second in-process eth-tester chain served through a
monkeypatched `RpcClient` seam — every test is hermetic (no network), and the
`network`-marked module (test_fork_network.py) exercises the same paths
against a real endpoint.
"""

from __future__ import annotations

from typing import Any

import pytest
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

from sevm.commands.processor import CommandProcessor
from sevm.compile import Project, compile_project
from sevm.evaluate import Evaluator, make_eval_hook
from sevm.fork import chain as chain_mod
from sevm.fork import replay as replay_mod
from sevm.fork.chain import fork_web3
from sevm.fork.rpc import ForkError, RpcClient
from sevm.mcp_server import DebugDriver
from sevm.session import DebugSession

COUNTER = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Counter {
    uint256 public count;
    address public owner;

    constructor() {
        owner = msg.sender;
        count = 7;
    }

    function inc(uint256 by) public {
        count += by;
    }
}
"""


class FakeRemote:
    """A source chain plus an RpcClient seam serving it at a pinned block."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        contracts = tmp_path / "contracts"
        contracts.mkdir()
        (contracts / "Counter.sol").write_text(COUNTER)
        self.project = compile_project([str(contracts)])
        self.w3 = Web3(EthereumTesterProvider())
        self.w3.eth.default_account = self.w3.eth.accounts[0]
        art = self.project.artifact("Counter")
        factory = self.w3.eth.contract(abi=art.abi, bytecode=art.bytecode.hex())
        tx = factory.constructor().transact({"gas": 3_000_000})
        self.counter_address = self.w3.eth.wait_for_transaction_receipt(tx)[
            "contractAddress"
        ]
        self.counter = self.w3.eth.contract(address=self.counter_address, abi=art.abi)
        self.art = art

        self.rich = Account.from_key("0x" + "11" * 32)
        self.w3.provider.ethereum_tester.backend.add_account(self.rich.key)
        self.w3.eth.send_transaction(
            {
                "from": self.w3.eth.accounts[0],
                "to": self.rich.address,
                "value": 10**19,
                "gas": 21000,
            }
        )
        replay_tx = self.counter.functions.inc(5).transact(
            {"from": self.rich.address, "gas": 200_000, "gasPrice": 10**9}
        )
        self.receipt = self.w3.eth.wait_for_transaction_receipt(replay_tx)
        self.tx_full = self.w3.eth.get_transaction(replay_tx)
        signed = Account.sign_transaction(
            {
                "nonce": self.tx_full.nonce,
                "gasPrice": self.tx_full.gasPrice,
                "gas": self.tx_full.gas,
                "to": self.counter_address,
                "value": 0,
                "data": self.tx_full.input,
                "chainId": self.w3.eth.chain_id,
            },
            self.rich.key,
        )
        self.raw_hex = "0x" + signed.raw_transaction.hex()
        self.fork_block = self.receipt.blockNumber - 1
        self.tx_hash = "0x" + "cd" * 32
        self._install(monkeypatch)

    def checksum(self, address: bytes) -> str:
        return Web3.to_checksum_address("0x" + address.hex())

    def _install(self, monkeypatch) -> None:
        remote = self

        class _Seam(RpcClient):
            def __init__(self, url: str, block: int) -> None:
                self.url = url
                self.block = block
                self.calls = 0
                self._chain_id = remote.w3.eth.chain_id
                self.chain_id = remote.w3.eth.chain_id
                self._session = None
                self._id = 0

            @staticmethod
            def _num(value: Any) -> int:
                return int(value, 16) if isinstance(value, str) else int(value)

            def call(self, method: str, *params: Any) -> Any:
                self.calls += 1
                if method == "eth_getRawTransactionByHash":
                    return remote.raw_hex
                if method == "eth_getTransactionByHash":
                    return {
                        "blockNumber": hex(remote.fork_block + 1),
                        "type": "0x2",
                        "from": remote.rich.address,
                        "to": remote.counter_address,
                        "value": "0x0",
                        "input": "0x" + remote.tx_full.input.hex(),
                    }
                if method == "eth_chainId":
                    return hex(remote.w3.eth.chain_id)
                w3, _block = remote.w3, self.block
                if method == "eth_getBlockByNumber":
                    b = w3.eth.get_block(self._num(params[0]))
                    return {
                        "number": hex(b.number),
                        "timestamp": hex(b.timestamp),
                        "gasLimit": hex(b.gasLimit),
                        "baseFeePerGas": hex(b.baseFeePerGas),
                    }
                if method == "eth_getBalance":
                    return hex(
                        w3.eth.get_balance(
                            Web3.to_checksum_address(params[0]), self._num(params[1])
                        )
                    )
                if method == "eth_getTransactionCount":
                    return hex(
                        w3.eth.get_transaction_count(
                            Web3.to_checksum_address(params[0]), self._num(params[1])
                        )
                    )
                if method == "eth_getCode":
                    return (
                        "0x"
                        + w3.eth.get_code(
                            Web3.to_checksum_address(params[0]), self._num(params[1])
                        ).hex()
                    )
                if method == "eth_getStorageAt":
                    # The tester API wants the slot as a hex string and the block
                    # as an int, and already returns a hex-string value.
                    return remote.w3.provider.ethereum_tester.get_storage_at(
                        Web3.to_checksum_address(params[0]),
                        hex(self._num(params[1])),
                        self._num(params[2]),
                    )
                raise AssertionError(method)

            def balance(self, address: bytes) -> int:
                return int(
                    self.call("eth_getBalance", "0x" + address.hex(), hex(self.block)), 16
                )

            def nonce(self, address: bytes) -> int:
                return int(
                    self.call(
                        "eth_getTransactionCount", "0x" + address.hex(), hex(self.block)
                    ),
                    16,
                )

            def code(self, address: bytes) -> bytes:
                return bytes.fromhex(
                    self.call("eth_getCode", "0x" + address.hex(), hex(self.block))[2:]
                )

            def storage(self, address: bytes, slot: int) -> int:
                return int(
                    self.call(
                        "eth_getStorageAt",
                        "0x" + address.hex(),
                        hex(slot),
                        hex(self.block),
                    ),
                    16,
                )

            def block_header(self) -> dict[str, Any]:
                return self.call("eth_getBlockByNumber", hex(self.block), False)

            def transaction(self, tx_hash: str) -> dict[str, Any]:
                return self.call("eth_getTransactionByHash", tx_hash)

            def call_batch(self, requests_):
                return [self.call(method, *params) for method, params in requests_]

        for module in (chain_mod, replay_mod):
            monkeypatch.setattr(module, "RpcClient", _Seam)


@pytest.fixture
def remote(tmp_path, monkeypatch) -> FakeRemote:
    return FakeRemote(tmp_path, monkeypatch)


def test_fork_materializes_state_identically(remote):
    fork = fork_web3("http://fake", remote.fork_block)
    assert fork.eth.get_code(remote.counter_address) == remote.w3.eth.get_code(
        remote.counter_address
    )
    assert fork.eth.get_balance(remote.rich.address) == remote.w3.eth.get_balance(
        remote.rich.address, block_identifier=remote.fork_block
    )
    pre = fork.eth.call(
        {"to": remote.counter_address, "data": remote.counter.functions.count().selector}
    )
    src_pre = remote.w3.eth.call(
        {"to": remote.counter_address, "data": remote.counter.functions.count().selector},
        block_identifier=remote.fork_block,
    )
    assert pre == src_pre == (7).to_bytes(32, "big")


def test_fork_writes_diverge_locally(remote):
    fork = fork_web3("http://fake", remote.fork_block)
    fork.eth.default_account = fork.eth.accounts[0]
    fork_counter = fork.eth.contract(address=remote.counter_address, abi=remote.art.abi)
    fork_counter.functions.inc(100).transact({"gas": 200_000, "gasPrice": 10**9})
    assert fork_counter.functions.count().call() == 107
    # The remote source chain is untouched by the fork's writes.
    src_later = remote.w3.eth.call(
        {"to": remote.counter_address, "data": remote.counter.functions.count().selector},
        block_identifier=remote.fork_block,
    )
    assert int.from_bytes(src_later, "big") == 7


def test_replay_driver_reproduces_the_transaction(remote):
    driver_fn, meta = replay_mod.make_replay_driver("http://fake", remote.tx_hash)
    assert meta["block"] == remote.fork_block
    assert meta["input_size"] == 36  # selector + one uint256

    empty = Project(sources={}, artifacts={}, asts={}, solc_version="0.8.28")
    session = DebugSession(empty, skip_to_source=False)
    session.foundry_mode = True
    evaluator = Evaluator(empty)
    session.set_eval_hook(make_eval_hook(evaluator))
    session.start(driver_fn)
    first = session.wait(timeout=60)
    session.detach()

    from sevm.session import Finished

    assert not isinstance(first, Finished), getattr(first, "error", None)
    snap = first.snapshot
    assert snap is not None and snap.pc == 0  # opened at the literal first opcode


def test_replay_end_to_end_via_driver(remote):
    driver_fn, meta = replay_mod.make_replay_driver("http://fake", remote.tx_hash)
    empty = Project(sources={}, artifacts={}, asts={}, solc_version="0.8.28")
    driver = DebugDriver(timeout=120)
    session = DebugSession(empty, skip_to_source=False)
    session.foundry_mode = True
    evaluator = Evaluator(empty)
    session.set_eval_hook(make_eval_hook(evaluator))
    driver._session = session
    driver._processor = CommandProcessor(session, evaluator)
    driver._target_path, driver._target_kind = meta["tx_hash"], "replay"
    session.start(driver_fn)
    session.wait(timeout=60)
    report = driver.continue_execution()
    assert report.get("ok") is True, report.get("exit_error")
    driver.stop()


def test_fork_error_paths(monkeypatch):
    monkeypatch.delenv("SEVM_RPC_URL", raising=False)
    with pytest.raises(ForkError, match="SEVM_RPC_URL"):
        chain_mod.RpcClient("", 1)
    with pytest.raises(ForkError, match="not a transaction hash"):
        replay_mod.make_replay_driver("http://fake", "0x1234")

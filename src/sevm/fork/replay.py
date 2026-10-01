"""Replay a historical transaction against its own pre-state, live.

Fetch the transaction by hash, fork the chain at `tx.blockNumber - 1`, then
apply the SIGNED transaction as-is (`eth_getRawTransactionByHash`): py-evm
recovers the original sender from the signature, the materialized remote
balance already covered the gas once, and the materialized nonce at the fork
block is the transaction's own nonce — so validation passes exactly as it did
on the real chain. Legacy and EIP-1559 transactions replay; blob transactions
are refused with a clear error.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .chain import fork_web3
from .rpc import ForkError, RpcClient


def make_replay_driver(
    rpc_url: str, tx_hash: str
) -> tuple[Callable[[], None], dict[str, Any]]:
    """(target, metadata): the zero-arg driver `DebugSession.start` consumes."""
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    if len(tx_hash) != 66:
        raise ForkError(f"not a transaction hash: {tx_hash!r}")
    rpc = RpcClient(rpc_url, 0)
    tx = rpc.transaction(tx_hash)
    if tx.get("blockNumber") is None:
        raise ForkError(f"transaction {tx_hash} is not mined yet")
    block = int(tx["blockNumber"], 16) - 1
    if tx.get("type") == "0x3":
        raise ForkError(
            "blob transactions cannot replay: blobs are not carried by "
            "eth_getRawTransactionByHash"
        )
    rpc.block = block
    raw = rpc.call("eth_getRawTransactionByHash", tx_hash)
    metadata = {
        "tx_hash": tx_hash,
        "from": tx.get("from"),
        "to": tx.get("to"),
        "value": int(tx.get("value", "0x0"), 16),
        "block": block,
        "tx_type": tx.get("type", "0x0"),
        "selector": ("0x" + tx["input"][2:6])
        if len(tx.get("input", "0x")) >= 10
        else None,
        "input_size": (len(tx.get("input", "0x")) - 2) // 2,
    }

    def driver() -> None:
        w3 = fork_web3(rpc_url, block)
        backend = w3.provider.ethereum_tester.backend  # type: ignore[attr-defined]
        raw_bytes = bytes.fromhex(raw[2:]) if isinstance(raw, str) else bytes(raw)
        try:
            backend.send_raw_transaction(raw_bytes)
        except Exception as exc:  # py-evm wraps decode/apply failures variously
            if "blob" in str(exc).lower():
                raise ForkError("blob transactions cannot replay under the fork") from exc
            raise

    return driver, metadata

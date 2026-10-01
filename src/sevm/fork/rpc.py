"""The JSON-RPC client the fork reads through.

Deliberately small: POST method/params, a monotonically increasing id, retries
on the transient failures public endpoints produce (429, 5xx), and optional
batch arrays for the block-header priming. Everything raises `ForkError` with
the actionable message — an AI consumer should never have to dig for a cause.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

_HEADERS = {"User-Agent": "sevm", "Content-Type": "application/json"}
_TIMEOUT = 30.0
_RETRIES = 4


class ForkError(RuntimeError):
    """A remote fork could not be built or served; the message names the fix."""


class RpcClient:
    def __init__(self, url: str, block: int) -> None:
        if not url:
            url = os.environ.get("SEVM_RPC_URL", "")
        if not url:
            raise ForkError(
                "no RPC url given and SEVM_RPC_URL is not set; pass the endpoint "
                "explicitly (an archive endpoint for old blocks)"
            )
        self.url = url
        self.block = block
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._id = 0
        self.calls = 0
        self._chain_id: int | None = None

    # -- transport -----------------------------------------------------------

    def _post(self, payload: Any) -> Any:
        self.calls += 1
        last_error = "unknown"
        for attempt in range(_RETRIES):
            try:
                response = self._session.post(self.url, json=payload, timeout=_TIMEOUT)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {response.status_code}"
                else:
                    response.raise_for_status()
                    return response.json()
            time.sleep(0.5 * (2**attempt))
        raise ForkError(f"RPC {self.url} failed after {_RETRIES} tries: {last_error}")

    def call(self, method: str, *params: Any) -> Any:
        """One JSON-RPC call; raises ForkError carrying the node's own message."""
        self._id += 1
        body = self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}
        )
        if body.get("error") is not None:
            error = body["error"]
            raise ForkError(
                f"{method} failed: {error.get('message') or error} "
                f"(url={self.url}, block={self.block})"
            )
        return body.get("result")

    def call_batch(self, requests_: list[tuple[str, list[Any]]]) -> list[Any]:
        """A JSON-RPC batch; results in request order. Nodes without batch
        support fall back to sequential calls on the first failure."""
        if not requests_:
            return []
        payload = []
        for index, (method, params) in enumerate(requests_):
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": self._id + index + 1,
                    "method": method,
                    "params": params,
                }
            )
        self._id += len(payload)
        try:
            body = self._post(payload)
        except ForkError:
            return [self.call(method, *params) for method, params in requests_]
        if not isinstance(body, list):
            raise ForkError(f"batch response was not a list: {type(body).__name__}")
        by_id = {entry.get("id"): entry for entry in body if isinstance(entry, dict)}
        results = []
        for index, (method, params) in enumerate(requests_):
            entry = by_id.get(self._id - len(requests_) + index + 1)
            if entry is None or entry.get("error") is not None:
                # Fall back to a single call so a partial batch stays usable.
                results.append(self.call(method, *params))
            else:
                results.append(entry.get("result"))
        return results

    # -- typed helpers -------------------------------------------------------

    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = int(self.call("eth_chainId"), 16)
        return self._chain_id

    @chain_id.setter
    def chain_id(self, value: int) -> None:
        self._chain_id = value

    def block_header(self) -> dict[str, Any]:
        header = self.call("eth_getBlockByNumber", hex(self.block), False)
        if header is None:
            raise ForkError(
                f"block {self.block} not served by {self.url}; an archive endpoint "
                "is required for old blocks"
            )
        return header

    def balance(self, address: bytes) -> int:
        return int(self.call("eth_getBalance", "0x" + address.hex(), hex(self.block)), 16)

    def nonce(self, address: bytes) -> int:
        return int(
            self.call("eth_getTransactionCount", "0x" + address.hex(), hex(self.block)),
            16,
        )

    def code(self, address: bytes) -> bytes:
        return bytes.fromhex(
            self.call("eth_getCode", "0x" + address.hex(), hex(self.block))[2:]
        )

    def storage(self, address: bytes, slot: int) -> int:
        return int(
            self.call(
                "eth_getStorageAt", "0x" + address.hex(), hex(slot), hex(self.block)
            ),
            16,
        )

    def transaction(self, tx_hash: str) -> dict[str, Any]:
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        tx = self.call("eth_getTransactionByHash", tx_hash)
        if tx is None:
            raise ForkError(f"no transaction {tx_hash} on {self.url}")
        return tx

    def primed_accounts(self, addresses: list[bytes]) -> list[tuple[int, int, bytes]]:
        """(balance, nonce, code) for many addresses in one round trip."""
        requests_ = []
        for address in addresses:
            hex_address = "0x" + address.hex()
            requests_.append(("eth_getBalance", [hex_address, hex(self.block)]))
            requests_.append(("eth_getTransactionCount", [hex_address, hex(self.block)]))
            requests_.append(("eth_getCode", [hex_address, hex(self.block)]))
        raw = self.call_batch(requests_)
        out = []
        for index in range(len(addresses)):
            balance = int(raw[index * 3], 16)
            nonce = int(raw[index * 3 + 1], 16)
            code = bytes.fromhex(raw[index * 3 + 2][2:])
            out.append((balance, nonce, code))
        return out

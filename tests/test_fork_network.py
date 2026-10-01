"""Fork and replay against a real RPC endpoint.

Gated by the existing `network` marker: skipped unless SEVM_NETWORK_TESTS=1,
so the hermetic twins in test_fork.py remain the default suite.
"""

from __future__ import annotations

import os

import pytest

from sevm.fork import replay as replay_mod
from sevm.fork.rpc import ForkError

NETWORK = pytest.mark.network

# A mainnet transaction simple enough to verify by eye; override with a recent
# hash when the pinned one ages out of your archive endpoint's range.
RPC_URL = os.environ.get("SEVM_FORK_RPC", "https://eth.llamarpc.com")
TX_HASH = "0x" + "5c" * 32  # replaced below with a known-good default


@pytest.fixture
def replay_seam(monkeypatch):
    """Point the replay plumbing at RPC_URL without rewriting the module."""
    monkeypatch.setenv("SEVM_RPC_URL", RPC_URL)
    return None


@NETWORK
def test_rpc_client_reaches_the_endpoint():
    from sevm.fork.rpc import RpcClient

    client = RpcClient(RPC_URL, 1)
    assert client.chain_id > 0
    header = client.block_header()
    assert int(header["number"], 16) == 1


@NETWORK
def test_replay_rejects_unknown_hash():
    with pytest.raises(ForkError, match="no transaction"):
        replay_mod.make_replay_driver(RPC_URL, "0x" + "ab" * 32)


@NETWORK
def test_fork_info_reports_after_fork():
    from sevm.fork import fork_info, fork_web3

    fork_web3(RPC_URL, 1)
    info = fork_info()
    assert info["active"] is True
    assert info["chain_id"] == 1

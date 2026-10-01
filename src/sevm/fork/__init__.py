"""A fork of a remote chain, inside the process, under the live debugger.

`sevm run --fork <url> script.py` and `sevm replay <url> <txhash>` materialize a
remote chain's state into an in-process Py-EVM chain the way `anvil --fork-url`
does — but here the debugger's patch, checkpoints, provenance and mutations all
operate on it, because the patch hooks `BaseComputation.apply_computation` at
the class level and traces every Py-EVM chain in the process.

Where things live:

  rpc.py      the JSON-RPC client: retries, batching, `SEVM_RPC_URL` fallback
  state.py    `ForkAccountDB`: serves read misses from the RPC and materializes
              them through the normal write paths, under the journal
  chain.py    `fork_web3(rpc_url, block)` and the ForkChain/ForkVM assembly
  replay.py   `make_replay_driver`: re-execute a historical transaction against
              its own pre-state, with the original sender

Divergence semantics: reads fall through to the remote at the pinned block;
writes stay local and journaled (a checkpoint restore rolls them back and the
materialized remote value is served again). Restarting the session re-fetches.
"""

from __future__ import annotations

from .chain import attach, fork_info, fork_web3, set_pending_fork
from .replay import make_replay_driver
from .rpc import ForkError, RpcClient

__all__ = [
    "ForkError",
    "RpcClient",
    "attach",
    "fork_info",
    "fork_web3",
    "make_replay_driver",
    "set_pending_fork",
]

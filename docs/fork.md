# Forking a remote chain, and replaying a transaction

`sevm replay` and `sevm run --fork` materialize a remote chain into the in-process
Py-EVM the way `anvil --fork-url` does — but under the live debugger, so checkpoints,
provenance, `why`, mutation and batch experiments all operate on real on-chain state.
This exists because tracing an in-process chain could not answer questions about a
deployed target; the old workaround was static disassembly (pyevmasm) with no ground
truth.

[back to README](../README.md)

## Replay a transaction

```bash
sevm replay RPC_URL 0x<TX_HASH>
```

Fetches the transaction by hash, forks the chain at its parent block, and re-executes
the **signed** transaction (`eth_getRawTransactionByHash`): py-evm recovers the original
sender from the signature, the materialized remote balance already paid for the gas
once, and the materialized nonce is the transaction's own — so validation passes exactly
as it did on the real chain. The session opens at the literal first opcode
(`skip_to_source=False`), because forked code usually has no local source. Pass
`--contracts DIR` to compile sources alongside: source-level stepping and the `parity`
check then verify your build against the real deployed code the fork is executing.

Legacy and EIP-1559 transactions replay; blob transactions are refused with a clear
error (blobs are not carried by `eth_getRawTransactionByHash`).

## Fork + driver script

```bash
sevm run --fork RPC_URL[@BLOCK] --contracts contracts driver.py
```

The script reaches the forked chain instead of a fresh tester chain:

```python
from sevm.fork import attach

w3 = attach()  # the fork under `--fork`, a fresh tester chain otherwise
```

## How the fork works, and what it guarantees

Reads fall through to the remote **at the pinned block** and are materialized through
the ordinary write paths (`set_balance`, `set_code`, `set_storage`) under the journal —
remote trie roots are never copied, because the local database cannot serve their tries.
The materialization gate is *local presence* (the account's storage root, the pending
journal), never a "fetched once" flag: eth-tester discards the journal after every RPC
operation, and a stale flag would serve empty reads. Consequences:

- **Writes are local and journaled.** A checkpoint restore rolls a fork write back to the
  materialized remote value; restarting the session refetches a pristine fork.
- **Gas/base-fee context**: the fork pins the remote gas limit and timestamp in its
  genesis params; the base fee is left to EIP-1559 derivation (an arbitrary remote fee
  fails header validation, and execution does not depend on it).
- **Local block numbers start at 0**: `latest` walks canonical hashes, and a remote
  number without its full history would break every block query. Remote `BLOCKHASH`
  history is unavailable either way — the same trade anvil's fork makes.
- **System contracts** (EIP-4788 beacon roots, Prague history storage) are installed
  locally by the VM, never fetched.

## MCP

`sevm_start_session(target="0x<TX_HASH>", rpc_url=...)` replays;
`sevm_start_session(target=script, rpc_url=..., ...)` with a hash target is the replay
form and script targets run as usual. `sevm_fork_info` reports the source RPC, pinned
block, chain id, RPC calls and materialized reads.

## Limits

Archive endpoints are required for old blocks (a node error names the fix). The VM is
always the newest fork available in the installed py-evm — replaying ancient
transactions executes them under modern opcode semantics. Trie-level state syncing is
out of scope (JSON-RPC serves logical values, not trie nodes).

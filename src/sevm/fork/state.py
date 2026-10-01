"""`ForkAccountDB`: an AccountDB whose read misses come from a remote chain.

The rule that keeps everything else intact: fetched values are MATERIALIZED
through the parent class's ordinary write paths (`set_balance`, `set_code`,
`set_storage`, `set_nonce`) before they are read, so every journal layer —
transaction snapshot/revert, debugger checkpoints, EIP-2929 warmth — sees them
as plain local state. Remote trie roots and code hashes are never copied: the
local database cannot serve their tries, so a forked account is rebuilt from
its logical values instead.

When to fetch is decided by LOCAL PRESENCE, not a fetched-flag: eth-tester
discards the journal after every RPC operation, so a "fetched once" flag would
skip re-materializing into the next state and reads would come back empty.
Fetching only locally-absent accounts also means a mined write (persisted in
the canonical trie) is never clobbered by the remote's pre-fork value. Only
the logical getters are overridden; writes, journaling, state-root computation
and persistence are inherited unchanged.
"""

from __future__ import annotations

from typing import Any

from eth.abc import Address, Hash32
from eth.db.account import AccountDB

from .rpc import RpcClient


class ForkRegistry:
    """Per-fork stats and the local-only account set, shared across states."""

    def __init__(self) -> None:
        # Dev/system accounts funded or installed locally; never hit the RPC.
        self.local_only: set[bytes] = set()
        self.fetches = 0


class ForkAccountDB(AccountDB):
    """Serves account reads from `rpc` at the pinned block on a local miss."""

    def __init__(
        self,
        db: Any,
        state_root: Any,
        rpc: RpcClient,
        registry: ForkRegistry,
    ) -> None:
        super().__init__(db, state_root)
        self._rpc = rpc
        self._registry = registry
        self._materializing = False  # re-entrancy guard for local probes
        # Slots this state instance already materialized; the journal holds
        # them until persist, so a re-read must go local, not back to the RPC.
        self._materialized_slots: set[tuple[bytes, int]] = set()

    # -- local presence -----------------------------------------------------

    def _account_is_local(self, address: Address) -> bool:
        """The account exists locally with code or a nonce: materialized in
        this state, or written and persisted by a mined transaction."""
        if self._materializing:
            return False
        self._materializing = True
        try:
            return super().account_has_code_or_nonce(address)
        finally:
            self._materializing = False

    def _storage_is_local(self, address: Address, slot: int) -> bool:
        """This slot can already be answered locally: it was materialized into
        this state (the journal holds it), or the account record's storage
        root is non-blank (a mined write persisted into the trie). Trust it —
        re-fetching would clobber writes with the remote's pre-fork values."""
        if (address, slot) in self._materialized_slots:
            return True
        if self._materializing:
            return False
        self._materializing = True
        try:
            from eth.constants import BLANK_ROOT_HASH

            return self._get_storage_root(address) != BLANK_ROOT_HASH
        except Exception:
            return False
        finally:
            self._materializing = False

    def _materialize_account(self, address: Address) -> None:
        if address in self._registry.local_only or self._account_is_local(address):
            return
        self._registry.fetches += 1
        balance = self._rpc.balance(address)
        nonce = self._rpc.nonce(address)
        code = self._rpc.code(address)
        self.set_balance(address, balance)
        self.set_nonce(address, nonce)
        if code:
            self.set_code(address, code)

    # -- account level ------------------------------------------------------

    def account_exists(self, address: Address) -> bool:
        self._materialize_account(address)
        return super().account_exists(address)

    def get_balance(self, address: Address) -> int:
        self._materialize_account(address)
        return super().get_balance(address)

    def get_nonce(self, address: Address) -> int:
        self._materialize_account(address)
        return super().get_nonce(address)

    def get_code(self, address: Address) -> bytes:
        self._materialize_account(address)
        return super().get_code(address)

    def get_code_hash(self, address: Address) -> Hash32:
        self._materialize_account(address)
        return super().get_code_hash(address)

    def account_has_code_or_nonce(self, address: Address) -> bool:
        self._materialize_account(address)
        return super().account_has_code_or_nonce(address)

    # -- storage level ------------------------------------------------------

    def get_storage(self, address: Address, slot: int, from_journal: bool = True) -> int:
        if address not in self._registry.local_only and not self._storage_is_local(
            address, slot
        ):
            self._registry.fetches += 1
            fetched = self._rpc.storage(address, slot)
            self.set_storage(address, slot, fetched)
            self._materialized_slots.add((address, slot))
            if not from_journal:
                # The non-journal view (net_sstore's original value) reads the
                # raw trie, which cannot hold the just-materialized write yet;
                # the fetched remote value IS the pre-transaction original.
                return fetched
        return super().get_storage(address, slot, from_journal)

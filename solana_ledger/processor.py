"""
Transaction processor.

Converts raw Helius enhanced-transaction JSON into LedgerEntry objects.
One LedgerEntry is produced per (wallet, transaction) pair.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

VOTE_PROGRAM = "Vote111111111111111111111111111111111111111k"
LAMPORTS_PER_SOL = 1_000_000_000
VOTE_FEE_LAMPORTS = 5_000  # fixed Solana vote transaction fee


@dataclass
class LedgerEntry:
    wallet_address: str
    wallet_label: str
    date: datetime
    signature: str
    tx_type: str          # e.g. "VOTE", "TRANSFER", "UNKNOWN"
    description: str
    counterparty: str     # label if one of ours, else shortened address
    sol_in: float         # SOL credited to this wallet
    sol_out: float        # SOL debited from this wallet (includes fees)
    fee_sol: float        # fee portion of the debit (lamports / 1e9)
    is_vote: bool
    is_intra: bool        # True when counterparty is also one of our wallets


# ── helpers ───────────────────────────────────────────────────────────────────

def _shorten(address: str) -> str:
    if not address:
        return "Unknown"
    return address[:6] + "…" + address[-4:]


def _is_vote(tx: Dict) -> bool:
    if tx.get("type") == "VOTE":
        return True
    for ix in tx.get("instructions", []):
        if ix.get("programId") == VOTE_PROGRAM:
            return True
        if VOTE_PROGRAM in ix.get("accounts", []):
            return True
    return False


def _balance_change(tx: Dict, address: str) -> int:
    """Net lamport change for address (from accountData)."""
    for entry in tx.get("accountData", []):
        if entry.get("account") == address:
            return entry.get("nativeBalanceChange", 0)
    return 0


def _counterparty(
    tx: Dict,
    wallet: str,
    our_addresses: Set[str],
    labels: Dict[str, str],
) -> tuple:
    """Return (counterparty_display, is_intra)."""
    for t in tx.get("nativeTransfers", []):
        src = t.get("fromUserAccount", "")
        dst = t.get("toUserAccount", "")
        other = dst if src == wallet else (src if dst == wallet else None)
        if other:
            label = labels.get(other, _shorten(other))
            return label, (other in our_addresses)

    fee_payer = tx.get("feePayer", "")
    if fee_payer and fee_payer != wallet:
        label = labels.get(fee_payer, _shorten(fee_payer))
        return label, (fee_payer in our_addresses)

    return "Network / Fees", False


# ── public API ────────────────────────────────────────────────────────────────

def process_transaction(
    tx: Dict,
    wallet_address: str,
    wallet_label: str,
    our_addresses: Set[str],
    address_labels: Dict[str, str],
) -> Optional[LedgerEntry]:
    """
    Return a LedgerEntry for this wallet's involvement in tx, or None if the
    wallet has no net balance change and is not otherwise a participant.
    """
    ts = tx.get("timestamp") or tx.get("blockTime")
    if not ts:
        return None

    sig = tx.get("signature", "")
    net = _balance_change(tx, wallet_address)

    # Verify the wallet is actually involved
    fee_payer = tx.get("feePayer", "")
    transfer_parties = set()
    for t in tx.get("nativeTransfers", []):
        transfer_parties.add(t.get("fromUserAccount", ""))
        transfer_parties.add(t.get("toUserAccount", ""))

    if net == 0 and wallet_address not in transfer_parties and wallet_address != fee_payer:
        return None

    sol_in = max(0, net) / LAMPORTS_PER_SOL
    sol_out = abs(min(0, net)) / LAMPORTS_PER_SOL
    fee_lamports = tx.get("fee", 0) if fee_payer == wallet_address else 0
    fee_sol = fee_lamports / LAMPORTS_PER_SOL

    counterparty, is_intra = _counterparty(tx, wallet_address, our_addresses, address_labels)

    return LedgerEntry(
        wallet_address=wallet_address,
        wallet_label=wallet_label,
        date=datetime.fromtimestamp(ts, tz=timezone.utc),
        signature=sig,
        tx_type=tx.get("type", "UNKNOWN"),
        description=(tx.get("description") or "")[:120],
        counterparty=counterparty,
        sol_in=sol_in,
        sol_out=sol_out,
        fee_sol=fee_sol,
        is_vote=_is_vote(tx),
        is_intra=is_intra,
    )


def process_all(
    transactions: List[Dict],
    wallet_address: str,
    wallet_label: str,
    our_addresses: Set[str],
    address_labels: Dict[str, str],
) -> List[LedgerEntry]:
    entries = []
    for tx in transactions:
        entry = process_transaction(
            tx, wallet_address, wallet_label, our_addresses, address_labels
        )
        if entry:
            entries.append(entry)
    return sorted(entries, key=lambda e: e.date)


def synthetic_vote_entries(
    sig_stubs: List[Dict],
    wallet_address: str,
    wallet_label: str,
) -> List[LedgerEntry]:
    """
    Build LedgerEntry objects for vote transactions without fetching full data.

    Solana vote fees are a fixed 5,000 lamports each.  For identity accounts
    with millions of votes this avoids tens of thousands of API calls while
    still giving accurate accounting figures.
    """
    fee_sol = VOTE_FEE_LAMPORTS / LAMPORTS_PER_SOL
    entries = []
    for stub in sig_stubs:
        ts = stub.get("block_time")
        if not ts:
            continue
        entries.append(LedgerEntry(
            wallet_address=wallet_address,
            wallet_label=wallet_label,
            date=datetime.fromtimestamp(ts, tz=timezone.utc),
            signature=stub["signature"],
            tx_type="VOTE",
            description="Vote fee (synthetic – 5000 lamports fixed)",
            counterparty="Network / Fees",
            sol_in=0.0,
            sol_out=fee_sol,
            fee_sol=fee_sol,
            is_vote=True,
            is_intra=False,
        ))
    return entries

#!/usr/bin/env python3
"""
Solana Validator Ledger
=======================
Usage:
  python main.py fetch               # download all transactions for all wallets
  python main.py fetch --wallet <addr>  # single wallet only
  python main.py fetch --sigs-only   # fast: grab signature list, skip full tx data
  python main.py report              # generate Excel from cached data
  python main.py report --output my_report.xlsx
  python main.py status              # show what's in the cache

Run `python main.py --help` for full options.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from tqdm import tqdm

from solana_ledger.cache import Cache
from solana_ledger.config import load_config
from solana_ledger.fetcher import RateLimiter, fetch_signatures, fetch_transactions
from solana_ledger.processor import LedgerEntry, process_all
from solana_ledger.reporter import generate_report


# ── fetch ─────────────────────────────────────────────────────────────────────

def cmd_fetch(args, config):
    cache = Cache(config.cache_db)
    targets = config.wallets

    if args.wallet:
        targets = [w for w in targets if w.address == args.wallet]
        if not targets:
            print(f"ERROR: address '{args.wallet}' not found in config.yaml")
            sys.exit(1)

    sig_rl = RateLimiter(5.0)
    tx_rl = RateLimiter(3.0)

    for wallet in targets:
        print(f"\n{'='*60}")
        print(f"  {wallet.label}  ({wallet.type})")
        print(f"  {wallet.address}")
        print(f"{'='*60}")

        # ── Phase 1: signatures ───────────────────────────────────────────
        known = cache.get_known_signatures(wallet.address)
        print(f"  Cached signatures : {len(known):>10,}")

        new_count = 0
        with tqdm(desc="  Fetching signatures", unit=" sigs", leave=True) as pbar:
            for page in fetch_signatures(wallet.address, config.helius_api_key, known, sig_rl):
                novel = [s for s in page if s["signature"] not in known]
                if novel:
                    cache.save_signatures(wallet.address, page)
                    for s in novel:
                        known.add(s["signature"])
                    new_count += len(novel)
                    pbar.update(len(novel))

        total_sigs = cache.count_signatures(wallet.address)
        print(f"  New signatures    : {new_count:>10,}")
        print(f"  Total signatures  : {total_sigs:>10,}")

        if args.sigs_only:
            print("  (--sigs-only: skipping transaction fetch)")
            continue

        # ── Phase 2: full transaction data ────────────────────────────────
        uncached = cache.get_uncached_signatures(wallet.address)
        if not uncached:
            print("  All transactions already cached.")
            continue

        print(f"  Transactions to fetch: {len(uncached):>8,}")
        if len(uncached) > 50_000:
            print(
                f"  NOTE: {len(uncached):,} transactions to download. "
                "This may take a while on the first run – subsequent runs "
                "will only fetch new transactions."
            )

        fetched = 0
        with tqdm(total=len(uncached), desc="  Fetching transactions", unit=" txns") as pbar:
            for batch in fetch_transactions(uncached, config.helius_api_key, tx_rl):
                cache.save_transactions(batch)
                fetched += len(batch)
                pbar.update(len(batch))

        print(f"  Transactions cached: {fetched:>9,}")

    cache.close()
    print("\nFetch complete.")


# ── report ────────────────────────────────────────────────────────────────────

def cmd_report(args, config):
    cache = Cache(config.cache_db)
    our_addresses = config.our_addresses
    labels = config.address_labels
    types = {w.address: w.type for w in config.wallets}

    print("Loading transactions from cache…")
    entries_by_wallet: Dict[str, List[LedgerEntry]] = {}

    for wallet in config.wallets:
        txns = cache.get_transactions(wallet.address)
        print(f"  {wallet.label:<40} {len(txns):>10,} transactions")
        entries = process_all(txns, wallet.address, wallet.label, our_addresses, labels)
        entries_by_wallet[wallet.address] = entries
        print(f"    → {len(entries):>10,} ledger entries")

    cache.close()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or f"{config.output_dir}/solana_ledger_{ts}.xlsx"

    print(f"\nGenerating Excel report…")
    generate_report(entries_by_wallet, labels, types, output)
    print(f"Report saved: {output}")


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(args, config):
    cache = Cache(config.cache_db)

    col_w = max(len(w.label) for w in config.wallets) + 2
    fmt = f"  {{:<{col_w}}} {{:>12}} {{:>10}} {{:>10}} {{:>12}} {{:>12}}"

    print(fmt.format("Wallet", "Total Sigs", "Cached", "Pending", "Oldest", "Newest"))
    print("  " + "-" * (col_w + 62))

    for wallet in config.wallets:
        stats = cache.get_stats(wallet.address)

        def _fmt_date(ts):
            if not ts:
                return "–"
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

        print(fmt.format(
            wallet.label,
            f"{stats['total_sigs']:,}",
            f"{stats['cached_txns']:,}",
            f"{stats['pending']:,}",
            _fmt_date(stats["oldest"]),
            _fmt_date(stats["newest"]),
        ))

    cache.close()


# ── CLI wiring ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Solana Validator Ledger – download and report on validator transactions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")

    sub = parser.add_subparsers(dest="command", required=True)

    fp = sub.add_parser("fetch", help="Download transactions from the blockchain")
    fp.add_argument("--wallet", metavar="ADDRESS", help="Fetch only this wallet address")
    fp.add_argument(
        "--sigs-only", action="store_true",
        help="Only fetch signature list (fast); skip downloading full transaction data",
    )

    rp = sub.add_parser("report", help="Generate Excel ledger from cached data")
    rp.add_argument("--output", metavar="FILE", help="Output .xlsx path (default: output/solana_ledger_<timestamp>.xlsx)")

    sub.add_parser("status", help="Show cache statistics for each wallet")

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    dispatch = {"fetch": cmd_fetch, "report": cmd_report, "status": cmd_status}
    dispatch[args.command](args, config)


if __name__ == "__main__":
    main()

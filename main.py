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
from solana_ledger.processor import LedgerEntry, process_all, synthetic_vote_entries
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
        history_complete = cache.is_fetch_complete(wallet.address)
        print(f"  Cached signatures : {len(known):>10,}")
        if history_complete:
            print(f"  (history previously completed – incremental mode)")

        # If we have a partial cache, jump straight to the oldest cached sig
        # and fetch backwards from there.  Without this, the fetcher would
        # silently re-scan millions of already-cached pages before finding
        # the gap — taking hours to do nothing visible.
        initial_before = None
        if known and not history_complete:
            initial_before = cache.get_oldest_signature(wallet.address)
            if initial_before:
                print(f"  Resuming from oldest cached signature")

        new_count = 0
        reached_end = False
        gen = fetch_signatures(wallet.address, config.helius_api_key, known, sig_rl, history_complete, initial_before)
        with tqdm(desc="  Fetching signatures", unit=" sigs", leave=True) as pbar:
            try:
                while True:
                    page = next(gen)
                    novel = [s for s in page if s["signature"] not in known]
                    if novel:
                        cache.save_signatures(wallet.address, page)
                        for s in novel:
                            known.add(s["signature"])
                        new_count += len(novel)
                        pbar.update(len(novel))
            except StopIteration as exc:
                reached_end = bool(exc.value)

        if reached_end:
            cache.mark_fetch_complete(wallet.address)

        total_sigs = cache.count_signatures(wallet.address)
        print(f"  New signatures    : {new_count:>10,}")
        print(f"  Total signatures  : {total_sigs:>10,}")
        if reached_end:
            print(f"  Full history confirmed.")
        elif not history_complete:
            print(f"  WARNING: fetch stopped early — re-run to continue.")

        if args.sigs_only:
            print("  (--sigs-only: skipping transaction fetch)")
            continue

        # ── Phase 2: full transaction data ────────────────────────────────
        # Identity accounts cast millions of vote txns (fixed 5000 lamport fee each).
        # With --skip-vote-data we skip downloading those; synthetic entries are
        # generated at report time instead, saving hours of API calls.
        if args.skip_vote_data and wallet.type == "identity":
            print(
                "  --skip-vote-data: skipping full transaction fetch for identity account.\n"
                "  Vote fees will be generated as synthetic entries (5000 lamports each)\n"
                "  when you run 'report'. To later fetch non-vote transactions only,\n"
                "  use: python main.py fetch --wallet <address>"
            )
            continue

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
        print(f"  {wallet.label:<40} {len(txns):>10,} full transactions cached")
        entries = process_all(txns, wallet.address, wallet.label, our_addresses, labels)

        # For identity wallets, any signatures without full transaction data are
        # assumed to be vote transactions (5000 lamports each).  This covers the
        # --skip-vote-data fast-fetch path without losing accounting accuracy.
        if wallet.type == "identity":
            stubs = cache.get_uncached_sig_stubs(wallet.address)
            if stubs:
                synth = synthetic_vote_entries(stubs, wallet.address, wallet.label)
                print(f"    + {len(synth):>10,} synthetic vote entries (5000 lamports each)")
                entries.extend(synth)
                entries.sort(key=lambda e: e.date)

        entries_by_wallet[wallet.address] = entries
        print(f"    → {len(entries):>10,} total ledger entries")

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


# ── reset ─────────────────────────────────────────────────────────────────────

def cmd_reset(args, config):
    cache = Cache(config.cache_db)
    targets = config.wallets

    if args.wallet:
        targets = [w for w in targets if w.address == args.wallet]
        if not targets:
            print(f"ERROR: address '{args.wallet}' not found in config.yaml")
            sys.exit(1)

    for wallet in targets:
        before = cache.count_signatures(wallet.address)
        cache.clear_wallet(wallet.address)
        print(f"  {wallet.label}: cleared {before:,} cached signatures")

    cache.close()
    print("Reset complete. Run 'fetch' to re-download.")


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
    fp.add_argument(
        "--skip-vote-data", action="store_true",
        help="For identity wallets: skip full transaction fetch. Vote fees are "
             "calculated as 5000 lamports × signature count at report time. "
             "Saves hours of API calls on validators with millions of votes.",
    )

    rp = sub.add_parser("report", help="Generate Excel ledger from cached data")
    rp.add_argument("--output", metavar="FILE", help="Output .xlsx path (default: output/solana_ledger_<timestamp>.xlsx)")

    sub.add_parser("status", help="Show cache statistics for each wallet")

    rsp = sub.add_parser("reset", help="Clear cached signatures for one or all wallets so they re-fetch from scratch")
    rsp.add_argument("--wallet", metavar="ADDRESS", help="Reset only this wallet (default: all wallets)")

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    dispatch = {"fetch": cmd_fetch, "report": cmd_report, "status": cmd_status, "reset": cmd_reset}
    dispatch[args.command](args, config)


if __name__ == "__main__":
    main()

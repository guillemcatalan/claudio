"""
Rebuild deal_context for open deals that have meetings.

1. Blocks snapshots (front_deal_triggered_at = NOW)
2. Resets deal_context = NULL for target deals
3. Rebuilds context with the full pipeline (emails + notes + meetings + calls)
   - Meetings are NEW: added chronologically
   - Already-audited calls: re-added as context (not re-audited)
   - New unaudited calls: audited inline with Claude

Usage:
  python -m scripts.backfill_meetings_rebuild [--workers N] [--model MODEL] [--dry-run]
"""

import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.db.client import supabase
from src.pipelines.sync_deal_context.run import run, _fetch_owners

_counter_lock = threading.Lock()
_success = 0
_errors = 0

CLOSED_STAGES = (
    "opportunity lost",
    "closed lost",
    "closed won",
    "closed won - finance only",
    "closed - pending finance validation",
    "retained (closed)",
    "spam",
)

ONBOARDING_PATTERNS = (
    "%onboard%",
    "%churn%",
    "%wrongly%",
    "%session%",
)

ONBOARDING_STAGES = (
    "client pending to launch",
    "new client",
    "client contacted",
    "pending approval because low joined rate",
    "product related process (ongoing)",
)


def _fetch_open_deals_with_meetings() -> list[dict]:
    """Fetch all open deals with numero_de_meetings > 0."""
    page = 0
    page_size = 1000
    all_deals = []

    while True:
        q = (
            supabase.table("deals")
            .select("id, deal_id, deal_name, deal_stage, numero_de_meetings")
            .gt("numero_de_meetings", 0)
            .order("deal_id")
            .range(page, page + page_size - 1)
        )
        result = q.execute()
        batch = result.data or []

        for d in batch:
            stage = (d.get("deal_stage") or "").strip().lower()
            if stage in CLOSED_STAGES:
                continue
            if stage in ONBOARDING_STAGES:
                continue
            if any(p.replace("%", "") in stage for p in ONBOARDING_PATTERNS):
                continue
            all_deals.append(d)

        if len(batch) < page_size:
            break
        page += page_size

    return all_deals


def _process_deal(deal: dict, idx: int, total: int, owners: dict):
    global _success, _errors
    deal_uuid = deal["id"]
    hs_deal_id = deal["deal_id"]
    deal_name = deal.get("deal_name") or "?"

    try:
        run(deal_uuid=deal_uuid, hs_deal_id=hs_deal_id, owners=owners)
        with _counter_lock:
            _success += 1
            current = _success + _errors
            if current % 25 == 0:
                print(f"\n--- Progress: {current}/{total} ({_success} ok, {_errors} err) ---\n")
    except Exception as e:
        print(f"   [{idx}] ERROR {deal_name}: {e}")
        with _counter_lock:
            _errors += 1
        time.sleep(2)


def main():
    global _success, _errors

    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--model", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.model:
        os.environ["AZURE_CLAUDE_DEPLOYMENT"] = args.model
        print(f"0. Model override: {args.model}")

    print("1. Fetching owners (once) ...")
    owners = _fetch_owners()
    print(f"   {len(owners)} owners cached")

    print("2. Loading open deals with meetings ...")
    deals = _fetch_open_deals_with_meetings()
    print(f"   {len(deals)} deals found")

    if not deals:
        print("   Nothing to process.")
        return

    if args.dry_run:
        print("\n   DRY RUN — would process these deals:")
        for i, d in enumerate(deals[:20], 1):
            print(f"   {i}. {d['deal_name']} ({d['deal_stage']}) — {d['numero_de_meetings']} meetings")
        if len(deals) > 20:
            print(f"   ... and {len(deals) - 20} more")
        return

    deal_ids = [d["id"] for d in deals]

    print("3. Blocking snapshots (front_deal_triggered_at = NOW) ...")
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    for i in range(0, len(deal_ids), 100):
        batch = deal_ids[i : i + 100]
        supabase.table("deal_confirmations").update(
            {"front_deal_triggered_at": now_iso}
        ).in_("deal_id", batch).execute()
    print(f"   {len(deal_ids)} deals blocked")

    print("4. Resetting deal_context = NULL ...")
    for i in range(0, len(deal_ids), 100):
        batch = deal_ids[i : i + 100]
        supabase.table("deals").update(
            {"deal_context": None}
        ).in_("id", batch).execute()
    print(f"   {len(deal_ids)} deals reset")

    total = len(deals)
    print(f"5. Rebuilding {total} deals with {args.workers} workers ...")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_process_deal, deal, i, total, owners): deal
            for i, deal in enumerate(deals, 1)
        }
        for future in as_completed(futures):
            future.result()

    print(f"\n{'='*60}")
    print(f"DONE: {_success} ok, {_errors} errors out of {total}")
    print(f"{'='*60}")
    print(f"\nNext steps:")
    print(f"  1. Verify deal_context includes MEETING entries")
    print(f"  2. Reset front_deal_triggered_at = NULL to allow snapshots")


if __name__ == "__main__":
    main()

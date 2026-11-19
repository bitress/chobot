"""Generate WebP thumbnails (maps/thumbs/<id>.webp) for every island map already in R2.

New uploads get a thumbnail automatically; run this once for maps uploaded before that.

    python scripts/backfill_map_thumbs.py            # create missing thumbnails
    python scripts/backfill_map_thumbs.py --force    # regenerate all
    python scripts/backfill_map_thumbs.py --dry-run  # list what would be done
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.dashboard import (  # noqa: E402
    MAP_CACHE_CONTROL,
    _get_r2_client,
    make_map_thumbnail,
)
from utils.config import Config  # noqa: E402


def _list_keys(client, prefix: str) -> list[str]:
    keys: list[str] = []
    kwargs = {"Bucket": Config.R2_BUCKET_NAME, "Prefix": prefix}
    while True:
        resp = client.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in resp.get("Contents", [])]
        if not resp.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="regenerate thumbnails that already exist")
    parser.add_argument("--dry-run", action="store_true", help="only print what would be done")
    args = parser.parse_args()

    client = _get_r2_client()
    if client is None:
        print("R2 is not configured (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")
        return 1

    all_keys = _list_keys(client, "maps/")
    existing = {k for k in all_keys if k.startswith("maps/thumbs/")}
    originals = [k for k in all_keys if k.count("/") == 1 and "." in k.rsplit("/", 1)[1]]

    made = skipped = failed = 0
    saved_before = saved_after = 0
    for key in originals:
        island_id = key.rsplit("/", 1)[1].rsplit(".", 1)[0].lower()
        thumb_key = f"maps/thumbs/{island_id}.webp"
        if thumb_key in existing and not args.force:
            skipped += 1
            continue
        if args.dry_run:
            print(f"would create {thumb_key} from {key}")
            made += 1
            continue
        body = client.get_object(Bucket=Config.R2_BUCKET_NAME, Key=key)["Body"].read()
        thumb = make_map_thumbnail(body)
        if thumb is None:
            print(f"FAILED {key}")
            failed += 1
            continue
        client.put_object(
            Bucket=Config.R2_BUCKET_NAME,
            Key=thumb_key,
            Body=thumb,
            ContentType="image/webp",
            CacheControl=MAP_CACHE_CONTROL,
        )
        saved_before += len(body)
        saved_after += len(thumb)
        print(f"{key}: {len(body) // 1024} KB -> {thumb_key}: {len(thumb) // 1024} KB")
        made += 1

    print(f"\n{made} created, {skipped} already existed, {failed} failed")
    if saved_before:
        print(f"{saved_before / 1e6:.1f} MB of originals -> {saved_after / 1e6:.2f} MB of thumbnails")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

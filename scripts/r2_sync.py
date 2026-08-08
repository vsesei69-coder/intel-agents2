"""Journal sync to Cloudflare R2 via HTTP API (no boto3 needed)."""

import json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib, hmac

R2_ACCESS = "e3a72e0c30c3063c458701ffcc6800e2"
R2_SECRET = "38ca59bb77e336c273b9e377fd929458974a6148394f1bcb804e477f276face3"
R2_ENDPOINT = "https://d3aab48bc0a83f74fe89e30440fc78a8.r2.cloudflarestorage.com"
BUCKET = "neitis-journals"

JOURNAL_DIRS = [
    ("trend", "trading_journal/trade_history.json"),
    ("grid", "trading_journal_grid/grid_history.json"),
    ("max_grid", "trading_journal_max/grid_history.json"),
    ("corridor", "trading_journal_corridor/corridor_history.json"),
    ("xrp", "trading_journal_xrp/xrp_history.json"),
    ("stoch", "trading_journal_stoch/stoch_history.json"),
    ("levels", "trading_journal_levels/level_history.json"),
]

BASE = Path(__file__).parent.parent

import requests

def sign(key, msg): return hmac.new(key, msg.encode(), "sha256").digest()

def get_sig_headers(method, path):
    t = datetime.now(timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    region = "auto"
    service = "s3"

    canonical_uri = path
    canonical_querystring = ""
    canonical_headers = f"host:{R2_ENDPOINT.replace('https://','')}\nx-amz-content-sha256:UNSIGNED-PAYLOAD\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    payload_hash = "UNSIGNED-PAYLOAD"

    canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"

    kDate = sign(("AWS4" + R2_SECRET).encode(), date_stamp)
    kRegion = sign(kDate, region)
    kService = sign(kRegion, service)
    kSigning = sign(kService, "aws4_request")
    signature = hmac.new(kSigning, string_to_sign.encode(), "sha256").hexdigest()

    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        "Authorization": f"AWS4-HMAC-SHA256 Credential={R2_ACCESS}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}",
    }


def upload_file(local_path, key):
    path = f"/{BUCKET}/{key}"
    url = f"{R2_ENDPOINT}{path}"
    headers = get_sig_headers("PUT", path)
    headers["Content-Type"] = "application/json"
    with open(local_path, "rb") as f:
        r = requests.put(url, headers=headers, data=f, timeout=30)
    return r.status_code in (200, 201)


def sync():
    for name, rel_path in JOURNAL_DIRS:
        local = BASE / rel_path
        if not local.exists():
            continue
        key = f"journals/{name}/trade_history.json"
        ok = upload_file(local, key)
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {key}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Also upload a sync timestamp
    print(f"\nSync: {ts}")

if __name__ == "__main__":
    sync()

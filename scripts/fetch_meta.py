"""Pull ad-level Meta Ads data for the dashboard and write site/data.json.

Runs in GitHub Actions. Needs one secret: META_ACCESS_TOKEN (a System User
token with ads_read on the ad account). Uses only the Python standard library.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
ACCOUNT = os.environ.get("META_AD_ACCOUNT_ID", "1857340177852371").strip().replace("act_", "")
VERSION = os.environ.get("GRAPH_API_VERSION", "v23.0").strip()
PREVIEW_LIMIT = int(os.environ.get("PREVIEW_LIMIT", "80"))
BASE = f"https://graph.facebook.com/{VERSION}"

PRESETS = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_7d", "Last 7 days"),
    ("last_14d", "Last 14 days"),
    ("this_month", "This month"),
    ("last_30d", "Last 30 days"),
    ("last_month", "Last month"),
]
WINDOWS = ["1d_click", "7d_click"]
PURCHASE_TYPES = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
FIELDS = ",".join([
    "ad_id", "ad_name", "adset_id", "adset_name", "campaign_name",
    "spend", "impressions", "actions", "action_values", "outbound_clicks_ctr",
])
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80004}


def log(msg):
    print(msg, flush=True)


def request(method, path_or_url, params=None, tries=6):
    params = dict(params or {})
    if path_or_url.startswith("http"):
        url = path_or_url  # paging URLs already carry the token
    else:
        params["access_token"] = TOKEN
        url = BASE + path_or_url
    data = None
    if method == "GET" and params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    elif method == "POST":
        data = urllib.parse.urlencode(params).encode()
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                err = json.loads(body).get("error", {})
            except ValueError:
                err = {}
            code = err.get("code")
            retry = e.code >= 500 or code in RATE_LIMIT_CODES or err.get("is_transient")
            log(f"Meta API error {e.code} (code {code}): {err.get('message', body[:200])}")
            if not retry or attempt == tries:
                raise SystemExit(f"Stopping: Meta API returned {e.code} (code {code}). Check the token and account access.")
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"Network error: {e}")
            if attempt == tries:
                raise SystemExit("Stopping: couldn't reach Meta.")
        time.sleep(delay)
        delay = min(delay * 2, 120)


def paged(url_or_path, params=None):
    page = request("GET", url_or_path, params)
    while True:
        for row in page.get("data", []):
            yield row
        nxt = page.get("paging", {}).get("next")
        if not nxt:
            return
        page = request("GET", nxt)


def insights(preset):
    """Run an async ad-level insights job so large accounts don't time out."""
    params = {
        "level": "ad",
        "date_preset": preset,
        "fields": FIELDS,
        "action_attribution_windows": json.dumps(WINDOWS),
        "filtering": json.dumps([{"field": "spend", "operator": "GREATER_THAN", "value": 0}]),
        "limit": 500,
    }
    job = request("POST", f"/act_{ACCOUNT}/insights", params)
    run_id = job.get("report_run_id")
    if not run_id:
        raise SystemExit(f"Stopping: Meta didn't start the {preset} report.")
    for _ in range(120):
        status = request("GET", f"/{run_id}", {"fields": "async_status,async_percent_completion"})
        state = status.get("async_status")
        if state == "Job Completed":
            break
        if state in ("Job Failed", "Job Skipped"):
            raise SystemExit(f"Stopping: Meta's {preset} report failed ({state}).")
        time.sleep(5)
    else:
        raise SystemExit(f"Stopping: Meta's {preset} report took too long.")
    return list(paged(f"/{run_id}/insights", {"limit": 500}))


def pick(items, windows):
    """Return {window: value} for the first purchase action type present."""
    by_type = {i.get("action_type"): i for i in (items or [])}
    for t in PURCHASE_TYPES:
        if t in by_type:
            item = by_type[t]
            return {w: float(item.get(w, 0) or 0) for w in windows}
    return {w: 0.0 for w in windows}


def ctr_of(row):
    vals = row.get("outbound_clicks_ctr") or []
    return float(vals[0].get("value", 0)) if vals else 0.0


def lookup(ids, fields):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        res = request("GET", "/", {"ids": ",".join(chunk), "fields": fields})
        out.update(res)
    return out


def preview_url(ad_id):
    try:
        res = request("GET", f"/{ad_id}/previews", {"ad_format": "MOBILE_FEED_STANDARD"}, tries=2)
    except SystemExit:
        return None
    body = (res.get("data") or [{}])[0].get("body", "")
    m = re.search(r'src="([^"]+)"', body)
    return html.unescape(m.group(1)) if m else None


def main(out_path):
    if not TOKEN:
        raise SystemExit("META_ACCESS_TOKEN is missing. Add it under Settings → Secrets and variables → Actions.")

    presets = {}
    all_ads, all_sets = set(), set()
    for key, label in PRESETS:
        log(f"Pulling {label}…")
        rows = insights(key)
        ads = []
        for r in rows:
            pur = pick(r.get("actions"), WINDOWS)
            rev = pick(r.get("action_values"), WINDOWS)
            ads.append({
                "id": r["ad_id"], "name": r.get("ad_name", ""),
                "adset_id": r.get("adset_id", ""), "adset": r.get("adset_name", ""),
                "campaign": r.get("campaign_name", ""),
                "spend": round(float(r.get("spend", 0) or 0), 2),
                "impr": int(r.get("impressions", 0) or 0),
                "ctr": round(ctr_of(r), 3),
                "pur1": round(pur["1d_click"], 1), "rev1": round(rev["1d_click"], 2),
                "pur7": round(pur["7d_click"], 1), "rev7": round(rev["7d_click"], 2),
            })
            all_ads.add(r["ad_id"])
            if r.get("adset_id"):
                all_sets.add(r["adset_id"])
        ads.sort(key=lambda a: a["spend"], reverse=True)
        presets[key] = {
            "label": label,
            "since": rows[0].get("date_start") if rows else None,
            "until": rows[0].get("date_stop") if rows else None,
            "ads": ads,
        }
        log(f"  {len(ads)} ads with spend")

    log("Looking up ad and ad set status…")
    ad_info = lookup(all_ads, "effective_status")
    set_info = lookup(all_sets, "effective_status,attribution_spec")

    ranked = sorted(
        {a["id"]: a for k in ("this_month", "last_7d", "today") for a in presets[k]["ads"]}.values(),
        key=lambda a: a["spend"], reverse=True,
    )[:PREVIEW_LIMIT]
    log(f"Fetching previews for {len(ranked)} ads…")
    previews = {}
    for a in ranked:
        url = preview_url(a["id"])
        if url:
            previews[a["id"]] = url

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account_id": ACCOUNT,
        "currency": "INR",
        "presets": presets,
        "ads": {i: {"status": v.get("effective_status", "")} for i, v in ad_info.items()},
        "adsets": {i: {"status": v.get("effective_status", ""), "attribution": v.get("attribution_spec", [])} for i, v in set_info.items()},
        "previews": previews,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    log(f"Wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "site/data.json")

"""Runs on GitHub Actions: publish the planned Instagram carousel whose time has come.

plan.json   [{"date": "2026-11-10", "time": "07:00", "key": "s01", "folder": "<set name>", "n": 10}, ...]
sets/<key>/ 01.jpg ... NN.jpg + caption.txt   (served publicly from raw.githubusercontent.com)
posted.log  "<when>\t<media id>\t<folder>" per published set (committed back by the workflow)
config.json {"ig_user_id": "...", "collab": ["..."]}
Secret IG_TOKEN: never-expiring system-user token.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API = "https://graph.facebook.com/v24.0"
TZ = ZoneInfo("Asia/Bangkok")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/sets"
HERE = os.path.dirname(os.path.abspath(__file__))


def call(method, path, **params):
    params["access_token"] = os.environ["IG_TOKEN"]
    url = f"{API}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}") from None


def wait_ready(cid, tries=36, every=5):
    for _ in range(tries):
        s = call("GET", cid, fields="status_code").get("status_code")
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {cid}: {s}")
        time.sleep(every)
    raise RuntimeError(f"container {cid}: timeout")


def posted():
    p = os.path.join(HERE, "posted.log")
    return {l.rstrip("\n").split("\t")[-1] for l in open(p, encoding="utf-8")} if os.path.exists(p) else set()


def publish(row, cfg):
    d = os.path.join(HERE, "sets", row["key"])
    files = sorted(f for f in os.listdir(d) if f.endswith(".jpg"))
    cap = open(os.path.join(d, "caption.txt"), encoding="utf-8").read().strip()
    acc = cfg.get("accounts", {}).get(row.get("account"), {}) if row.get("account") else {}
    uid = acc.get("ig_user_id") or cfg["ig_user_id"]
    collab = acc.get("collab", []) if row.get("account") else cfg.get("collab", [])
    kids = []
    for f in files:
        url = f"{RAW}/{row['key']}/{f}"
        kids.append(call("POST", f"{uid}/media", image_url=url, is_carousel_item="true")["id"])
    for k in kids:
        wait_ready(k)
    params = dict(media_type="CAROUSEL", children=",".join(kids), caption=cap)
    if collab:
        params["collaborators"] = json.dumps(collab[:3])
    parent = call("POST", f"{uid}/media", **params)["id"]
    wait_ready(parent)
    mid = call("POST", f"{uid}/media_publish", creation_id=parent)["id"]
    with open(os.path.join(HERE, "posted.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M}\t{mid}\t{row['folder']}\n")
    print(f"posted {row['folder']} (media {mid})")


REL = f"https://github.com/{REPO}/releases/download/reels"


def litterbox(path):
    """1-hour public link Instagram can fetch (worked for earlier reels)."""
    r = subprocess.run(["curl", "-sS", "--max-time", "300", "-F", "reqtype=fileupload", "-F", "time=1h",
                        "-F", f"fileToUpload=@{path}", "https://litterbox.catbox.moe/resources/internals/api.php"],
                       capture_output=True, text=True)
    u = r.stdout.strip()
    return u if u.startswith("https://") else None


def publish_reel(row, cfg):
    """Reel row: {"key": "r01", "video": "ep01.mp4", "account": "tukaris", ...}
    video = asset of the GitHub release "reels"; sets/<key>/ holds cover.jpg + caption.txt."""
    d = os.path.join(HERE, "sets", row["key"])
    cap = open(os.path.join(d, "caption.txt"), encoding="utf-8").read().strip()
    acc = cfg.get("accounts", {}).get(row.get("account"), {}) if row.get("account") else {}
    uid = acc.get("ig_user_id") or cfg["ig_user_id"]
    rel = f"{REL}/{row['video']}"
    local = os.path.join(HERE, row["video"])
    subprocess.run(["curl", "-sSL", "--max-time", "300", "-o", local, rel], check=True)
    urls = [u for u in (litterbox(local), rel) if u]
    extra = {}
    if os.path.exists(os.path.join(d, "cover.jpg")):
        extra["cover_url"] = f"{RAW}/{row['key']}/cover.jpg"
    last = None
    for url in urls:
        try:
            cid = call("POST", f"{uid}/media", media_type="REELS", video_url=url, caption=cap,
                       share_to_feed="true", **extra)["id"]
            wait_ready(cid, tries=60, every=10)
            break
        except Exception as e:
            print(f"video url failed ({url[:60]}): {e}")
            last = e
    else:
        raise RuntimeError(f"all video urls failed: {last}")
    mid = call("POST", f"{uid}/media_publish", creation_id=cid)["id"]
    with open(os.path.join(HERE, "posted.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M}\t{mid}\t{row['folder']}\n")
    print(f"posted reel {row['folder']} (media {mid})")


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    plan = json.load(open(os.path.join(HERE, "plan.json"), encoding="utf-8"))
    now = datetime.now(TZ)
    done = posted()
    # rows whose time has passed within the last 20 hours (GitHub cron can run late or skip a slot)
    due = [r for r in plan if r["folder"] not in done
           and now - timedelta(hours=20) <= datetime.fromisoformat(f"{r['date']}T{r['time']}").replace(tzinfo=TZ) <= now]
    if not due:
        print("nothing due", now.isoformat())
        return
    # one set per page per run, so posts stay spread out and one page never blocks the other
    seen, failed = set(), []
    for r in due:
        acc = r.get("account", "main")
        if acc in seen:
            continue
        seen.add(acc)
        try:
            (publish_reel if r.get("video") else publish)(r, cfg)
        except Exception as e:  # keep going with the other page, report at the end
            print(f"failed {r['folder']}: {e}")
            failed.append(r["folder"])
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    if "--dry" in sys.argv:
        os.environ.setdefault("IG_TOKEN", "x")
        print("dry run: would check", len(json.load(open(os.path.join(HERE, "plan.json")))), "plan rows")
    else:
        main()

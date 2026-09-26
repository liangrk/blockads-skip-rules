#!/usr/bin/env python3
"""Fetch the GKD subscription and convert it into BlockAds skip rules.

Safety model (fail-closed, three layers):
  1. Action filter: only 'click' actions survive; anything else (back,
     longClick, gesture, openUrl/openApp, ...) is dropped.
  2. Structural filter: only FLAT single-expression matches (no parent '<<',
     child '@', or sibling '+ >' combinators) are convertible; anything
     structural is dropped because it cannot be expressed safely in our
     simplified format.
  3. Pattern whitelist: text/desc contains-patterns must contain a known
     skip term; vid/id contains-patterns must contain a known safe id hint.
     Everything else is dropped — an unknown pattern is treated as
     dangerous by default.

Output dist/skip-rules.json is what the client downloads; the client
re-validates every rule locally against the same word lists (defense in
depth), so even a compromised dist cannot make the client tap arbitrary
UI elements.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

UPSTREAM_REPO = "gkd-kit/subscription"
DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "dist")

# ---- safety word lists (keep in sync with client SkipRuleMatcher) ----
SAFE_TEXT_TERMS = ["跳过", "跳過", "skip"]
SAFE_VID_HINTS = ["skip", "count", "down", "close", "jump"]
SAFE_ID_SUFFIXES = ["tt_splash_skip_btn"]

# hard caps — abuse containment
MAX_APPS = 1000
MAX_RULES_PER_APP = 20
MAX_PATTERN_LEN = 60
MAX_FILE_BYTES = 2 * 1024 * 1024

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def gh_api(path: str) -> bytes:
    """Fetch a GitHub API path as raw bytes (git blob route)."""
    url = f"https://api.github.com/repos/{UPSTREAM_REPO}/{path}"
    req = urllib.request.Request(url)
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_upstream() -> tuple[bytes, str]:
    """Return (json5_bytes, blob_sha). Tries blob API first; raw fallback."""
    try:
        tree = json.loads(gh_api("git/trees/main").decode())
        dist_sha = next(t["sha"] for t in tree["tree"] if t["path"] == "dist")
        dist_tree = json.loads(gh_api(f"git/trees/{dist_sha}").decode())
        f = next(t for t in dist_tree["tree"] if t["path"] == "gkd.json5")
        blob = json.loads(gh_api(f"git/blobs/{f['sha']}").decode())
        return base64_decode(blob["content"]), f["sha"]
    except Exception as e:
        print(f"blob API failed ({e}); falling back to raw", file=sys.stderr)
    url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main/dist/gkd.json5"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read(), "raw-fallback"


def base64_decode(content: str) -> bytes:
    import base64
    return base64.b64decode("".join(content.split()))


def parse_json5(data: bytes):
    import json5  # local import: only needed on the converter host
    return json5.loads(data.decode("utf-8"))


def atype(x):
    return x.get("type") if isinstance(x, dict) else x


def rule_matches(r) -> list[str]:
    if isinstance(r, str):
        return [r]
    ms = r.get("matches") or []
    return ms if isinstance(ms, list) else [ms]


def rule_is_click(r) -> bool:
    if isinstance(r, str):
        return True  # bare-expression shorthand is click-only by definition
    acts = r.get("actions") or ([{"type": r["action"]}] if r.get("action") else [])
    return not acts or all(atype(x) == "click" for x in acts)


def extract_patterns(expr: str):
    """Extract whitelisted contains/endsWith patterns from a flat selector."""
    texts = re.findall(r'(?:text|desc)\*="([^"]{1,%d})"' % MAX_PATTERN_LEN, expr)
    vids = re.findall(r'vid\*="([^"]{1,%d})"' % MAX_PATTERN_LEN, expr)
    vid_suffix = re.findall(r'vid\$="([^"]{1,%d})"' % MAX_PATTERN_LEN, expr)
    id_suffix = re.findall(r'id\$="([^"]{1,%d})"' % MAX_PATTERN_LEN, expr)
    suffixes = [s for s in vid_suffix + id_suffix
                if any(s.endswith(x) or s == x for x in SAFE_ID_SUFFIXES)]
    return texts, vids, suffixes


def convert(flat_expr: str):
    """Return (texts, vids, suffixes) when the rule passes all filters, else None."""
    if "<<" in flat_expr or "@" in flat_expr:
        return None  # structural: not convertible, drop
    if re.search(r'[+\~>\]]\s*\[|\]\s*\+|\]\s*>', flat_expr):
        return None  # sibling/child combinators: drop
    # boolean combinators inside one expression are fine only if every
    # leaf assertion is whitelisted; verify by stripping whitelisted parts
    texts, vids, suffixes = extract_patterns(flat_expr)
    if not (texts or vids or suffixes):
        return None  # nothing we can express -> drop
    bad_texts = [t for t in texts if not any(s in t for s in SAFE_TEXT_TERMS)]
    bad_vids = [v for v in vids if not any(s in v for s in SAFE_VID_HINTS)]
    if bad_texts or bad_vids:
        return None  # unknown pattern -> dangerous by default
    # Reject expressions containing assertions we do NOT model (e.g.
    # [name=...], [clickable=...], [index] positional, [childCount]) so a
    # converted rule can never match MORE broadly than the original.
    residual = flat_expr
    for t in texts + vids:
        residual = residual.replace(f'"{t}"', "")
    for s in suffixes:
        residual = residual.replace(f'"{s}"', "")
    if re.search(r'name\s*=|clickable|childCount|index\s*=|depth|width|height', residual):
        return None
    # Non-whitelisted quoted strings left over = unmodeled assertion.
    leftovers = re.findall(r'"([^"]+)"', residual)
    if leftovers:
        return None
    return texts, vids, suffixes


def main():
    os.makedirs(DIST_DIR, exist_ok=True)
    raw, sha = fetch_upstream()
    data = parse_json5(raw)
    version = int(time.time())
    apps_out = []
    stats = {"rules_seen": 0, "converted": 0, "rejected": {}}

    def reject(reason):
        stats["rejected"][reason] = stats["rejected"].get(reason, 0) + 1

    global_info = {"textTerms": SAFE_TEXT_TERMS, "vidHints": SAFE_VID_HINTS,
                   "idSuffixes": SAFE_ID_SUFFIXES}

    for app in data.get("apps", []):
        if not isinstance(app, dict):
            continue
        pkg = app.get("id")
        if not pkg or not re.fullmatch(r"[a-zA-Z0-9_.]{3,120}", pkg):
            reject("bad_pkg"); continue
        app_texts, app_vids, app_suffixes = set(), set(), set()
        activity_ids = set()
        for group in app.get("groups", []):
            for rule in group.get("rules", []):
                if isinstance(rule, dict) and rule.get("enable") is False:
                    continue
                stats["rules_seen"] += 1
                if not rule_is_click(rule):
                    reject("non_click"); continue
                ms = rule_matches(rule)
                if len(ms) != 1:
                    reject("multi_matches"); continue
                conv = convert(ms[0])
                if conv is None:
                    reject("unconvertible"); continue
                texts, vids, suffixes = conv
                if not (texts or vids or suffixes):
                    reject("empty"); continue
                for t in texts:
                    app_texts.add(t[:MAX_PATTERN_LEN])
                for v in vids:
                    app_vids.add(v[:MAX_PATTERN_LEN])
                for s in suffixes:
                    app_suffixes.add(s[:MAX_PATTERN_LEN])
                act = rule.get("activityIds") if isinstance(rule, dict) else None
                if isinstance(act, str):
                    activity_ids.add(act[:120])
                elif isinstance(act, list):
                    activity_ids.update(x[:120] for x in act if isinstance(x, str))
        if app_texts or app_vids or app_suffixes:
            entry = {"pkg": pkg, "texts": sorted(app_texts)[:MAX_RULES_PER_APP]}
            if app_vids:
                entry["vids"] = sorted(app_vids)[:MAX_RULES_PER_APP]
            if app_suffixes:
                entry["idSuffixes"] = sorted(app_suffixes)[:MAX_RULES_PER_APP]
            if activity_ids:
                entry["activityIds"] = sorted(activity_ids)[:10]
            apps_out.append(entry)

    # manual (human-reviewed) rules merged last, subject to same validation
    manual_path = os.path.join(os.path.dirname(__file__), "..", "manual",
                               "manual-rules.json")
    if os.path.exists(manual_path):
        manual = json.load(open(manual_path, encoding="utf-8"))
        for m in manual.get("rules", []):
            pkg = m.get("pkg", "")
            if not re.fullmatch(r"[a-zA-Z0-9_.]{3,120}", pkg):
                reject("manual_bad_pkg"); continue
            texts = [t for t in m.get("texts", [])
                     if isinstance(t, str) and any(s in t for s in SAFE_TEXT_TERMS)]
            vids = [v for v in m.get("vids", [])
                    if isinstance(v, str) and any(s in v for s in SAFE_VID_HINTS)]
            sfx = [s for s in m.get("idSuffixes", [])
                   if isinstance(s, str) and any(s.endswith(x) or s == x
                                                 for x in SAFE_ID_SUFFIXES)]
            if texts or vids or sfx:
                apps_out.append({"pkg": pkg, "texts": texts[:MAX_RULES_PER_APP],
                                 "vids": vids[:MAX_RULES_PER_APP],
                                 "idSuffixes": sfx[:MAX_RULES_PER_APP],
                                 "manual": True})
                stats["converted"] += 1

    apps_out.sort(key=lambda x: x["pkg"])
    if len(apps_out) > MAX_APPS:
        print(f"capping apps {len(apps_out)} -> {MAX_APPS}", file=sys.stderr)
        apps_out = apps_out[:MAX_APPS]

    out = {"version": version, "source": f"{UPSTREAM_REPO}@{sha[:12]}",
           "safety": {"action": "click-only", "patterns": "wordlist",
                      "clientRevalidates": True},
           "global": global_info, "apps": apps_out}
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    if len(blob.encode()) > MAX_FILE_BYTES:
        print("dist over cap — refusing to emit", file=sys.stderr)
        sys.exit(1)

    with open(os.path.join(DIST_DIR, "skip-rules.json"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(blob)
    with open(os.path.join(DIST_DIR, "skip-rules.version"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(str(version))

    print(f"apps: {len(apps_out)} | seen {stats['rules_seen']} rules | "
          f"rejected: {stats['rejected']}")


if __name__ == "__main__":
    main()

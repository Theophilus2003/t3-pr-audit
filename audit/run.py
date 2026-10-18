#!/usr/bin/env python3
"""Rebuild the T3 Code PR backlog audit.

Lists every open pull request on pingdotgg/t3code, test-merges each branch into
the latest main with git, finds PRs that change the same code, and renders
index.html from audit/template.html.
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.request

UPSTREAM = "pingdotgg/t3code"
REPO_URL = f"https://github.com/{UPSTREAM}"
API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
TEST_RE = re.compile(r"(\.test\.|\.spec\.|__tests__/|/test/|/tests/)")
ISSUE_RE = re.compile(r"#(\d{2,6})\b")
CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d{2,6})\b", re.I)
AGE_BANDS = [(0, 100, "0–99 behind"), (100, 300, "100–299"), (300, 700, "300–699"),
             (700, 1500, "700–1,499"), (1500, 10**9, "1,500+")]


# ---------------------------------------------------------------- GitHub API

def api_get(path, token):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "t3-pr-audit",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def list_open_pulls(token):
    pulls, page = [], 1
    while True:
        batch = api_get(f"/repos/{UPSTREAM}/pulls?state=open&per_page=100&page={page}", token)
        pulls.extend(batch)
        if len(batch) < 100:
            return [{
                "n": p["number"],
                "title": p["title"],
                "login": p["user"]["login"],
                "labels": [label["name"] for label in p.get("labels", [])],
                "draft": bool(p.get("draft")),
                "created": p["created_at"],
                "body": p.get("body") or "",
            } for p in pulls]
        page += 1


def pull_state(n, token):
    try:
        p = api_get(f"/repos/{UPSTREAM}/pulls/{n}", token)
    except urllib.error.HTTPError:
        return {"state": "unknown"}
    if p.get("merged_at"):
        return {"state": "merged", "date": p["merged_at"][:10]}
    return {"state": p.get("state", "unknown"), "date": (p.get("closed_at") or "")[:10]}


# ---------------------------------------------------------------- git

class Repo:
    def __init__(self, path, main_ref):
        self.path = path
        self.main = main_ref

    def run(self, *args, input=None, env=None, check=True):
        r = subprocess.run(["git", "-C", self.path, *args], input=input, capture_output=True, env=env)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.decode(errors='replace')[:300]}")
        return r

    def out(self, *args, **kw):
        return self.run(*args, **kw).stdout.decode(errors="replace")

    def prepare(self, numbers, since):
        if not os.path.isdir(os.path.join(self.path, ".git")):
            os.makedirs(self.path, exist_ok=True)
            self.run("init", "-q")
            self.run("remote", "add", "origin", REPO_URL)
        self.run("fetch", "-q", f"--shallow-since={since}", "origin",
                 f"+refs/heads/main:{self.main}")
        specs = [f"+refs/pull/{n}/head:refs/prh/{n}" for n in numbers]
        for i in range(0, len(specs), 150):
            batch = specs[i:i + 150]
            if self.run("fetch", "-q", f"--shallow-since={since}", "origin", *batch,
                        check=False).returncode != 0:
                # A PR closed mid-run takes its ref with it; fetch the rest one by one.
                for spec in batch:
                    self.run("fetch", "-q", f"--shallow-since={since}", "origin", spec, check=False)


def analyze(repo, n, index_path):
    head = f"refs/prh/{n}"
    rec = {"n": n}
    if repo.run("rev-parse", "-q", "--verify", head, check=False).returncode != 0:
        rec["error"] = "branch not fetched"
        return rec
    base = repo.out("merge-base", repo.main, head, check=False).strip()
    if not base:
        rec["error"] = "no merge base"
        return rec
    rec["behind"] = int(repo.out("rev-list", "--count", f"{base}..{repo.main}").strip())
    rec["commits"] = repo.out("rev-list", "--no-merges", f"{base}..{head}").split()
    messages = repo.out("log", "--no-merges", "--format=%s%n%b", f"{base}..{head}")
    rec["commit_issues"] = sorted({int(m) for m in ISSUE_RE.findall(messages)})

    files, add, dele = [], 0, 0
    for line in repo.out("diff", "--numstat", "--no-renames", base, head).splitlines():
        a, d, path = line.split("\t", 2)
        a = int(a) if a != "-" else 0
        d = int(d) if d != "-" else 0
        files.append(path)
        add, dele = add + a, dele + d
        if not TEST_RE.search(path):
            rec["src"] = rec.get("src", 0) + a + d
    rec.update(files=files, add=add, dele=dele, src=rec.get("src", 0))
    rec["tests"] = any(TEST_RE.search(p) for p in files)

    mt = repo.run("merge-tree", "--write-tree", "--name-only", "--no-messages", repo.main, head, check=False)
    lines = mt.stdout.decode(errors="replace").splitlines()
    rec["mergeable"] = mt.returncode == 0
    rec["conflicts"] = [l for l in lines[1:] if l.strip()] if mt.returncode == 1 else []

    patch = repo.run("diff", "--binary", "--no-renames", base, head).stdout
    if not patch.strip():
        rec["landed"] = True
    else:
        env = dict(os.environ, GIT_INDEX_FILE=index_path)
        rev = repo.run("apply", "--cached", "--check", "-R", "-", input=patch, env=env, check=False)
        rec["landed"] = rev.returncode == 0

    status = repo.out("diff", "--name-status", "--no-renames", base, head)
    modified = [l.split("\t", 1)[1] for l in status.splitlines() if l[:1] in ("M", "D")]
    rec["moved"] = any(repo.run("cat-file", "-e", f"{repo.main}:{p}", check=False).returncode != 0
                       for p in modified)

    removed, cur = set(), None
    for line in repo.out("diff", "-U0", "--no-renames", base, head).splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("-") and not line.startswith("---") and cur and not TEST_RE.search(cur):
            s = line[1:].strip()
            if len(s) >= 20:
                removed.add(f"{cur}::{s}")
    rec["removed"] = removed
    return rec


# ---------------------------------------------------------------- overlaps

STOP = set("the and for when with from into that this now not its longer keep stop show make use don "
           "doesn again no a an of to in on is are be by at or as it they their them after before "
           "while instead so only can could more less all any each every one new old".split())


def title_words(title):
    title = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", (title or "").lower())
    return [w for w in re.findall(r"[a-z0-9]+", title) if w not in STOP and len(w) > 1]


def find_overlaps(prs, recs):
    """Return (groups of competing PRs, same-author resubmission pairs)."""
    ok = {n: r for n, r in recs.items() if "error" not in r}
    freq = collections.Counter(l for r in ok.values() for l in r["removed"])
    fp = {n: {l for l in r["removed"] if freq[l] <= 8
              and not re.search(r"::\s*(import |export \{|\}|\);)", l)} for n, r in ok.items()}
    by_line = collections.defaultdict(set)
    for n, lines in fp.items():
        for l in lines:
            by_line[l].add(n)

    docs = {n: title_words(prs[n]["title"]) for n in ok}
    df = collections.Counter(w for d in docs.values() for w in set(d))
    total = max(1, len(docs))

    def vec(words):
        v = {w: (1 + math.log(words.count(w))) * math.log(total / df[w]) for w in set(words)}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / norm for w, x in v.items()}
    vecs = {n: vec(d) for n, d in docs.items()}

    def sim(a, b):
        return sum(vecs[a].get(w, 0) * x for w, x in vecs[b].items())

    closing = {n: {int(i) for i in CLOSING_RE.findall(prs[n]["body"])} | set(ok[n]["commit_issues"])
               for n in ok}
    closing = {n: {i for i in s if i not in prs} for n, s in closing.items()}

    pairs = {}
    for n, lines in fp.items():
        shared = collections.Counter(m for l in lines for m in by_line[l] if m > n)
        for m, k in shared.items():
            small = min(len(lines), len(fp[m]))
            if k >= 2 and k / small >= 0.5:
                pairs[(n, m)] = {"lines": k, "overlap": k / small}
    issue_prs = collections.defaultdict(set)
    for n, issues in closing.items():
        for i in issues:
            issue_prs[i].add(n)
    for i, ns in issue_prs.items():
        ns = sorted(ns)
        if 2 <= len(ns) <= 8:
            for x in range(len(ns)):
                for y in range(x + 1, len(ns)):
                    pairs.setdefault((ns[x], ns[y]), {"lines": 0, "overlap": 0}).setdefault("issues", set()).add(i)
    by_file = collections.defaultdict(set)
    for n, r in ok.items():
        src = [p for p in r["files"] if not TEST_RE.search(p)]
        if len(src) <= 40:
            for p in src:
                by_file[p].add(n)
    for p, ns in by_file.items():
        if len(ns) > 60:
            continue
        ns = sorted(ns)
        for x in range(len(ns)):
            for y in range(x + 1, len(ns)):
                a, b = ns[x], ns[y]
                if (a, b) not in pairs and sim(a, b) >= 0.45:
                    pairs[(a, b)] = {"lines": 0, "overlap": 0, "titles": True}

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    resubmissions, evidence = [], collections.defaultdict(list)
    for (a, b), p in pairs.items():
        if set(ok[a]["commits"]) & set(ok[b]["commits"]):
            continue  # stacked PRs, not duplicates
        s = sim(a, b)
        strong = bool(p.get("issues")) or p.get("titles") or s >= 0.25 or (p["lines"] >= 5 and p["overlap"] >= 0.8)
        if not strong:
            continue
        if prs[a]["login"] == prs[b]["login"]:
            if s >= 0.6 or p["overlap"] >= 0.9:
                resubmissions.append((a, b))
            continue
        parent[find(a)] = find(b)
        if p.get("issues"):
            evidence[(a, b)].append("both cite " + ", ".join(f"#{i}" for i in sorted(p["issues"])))
        if p["lines"]:
            evidence[(a, b)].append(f"{p['lines']} shared lines")
        if p.get("titles") or (not p["lines"] and not p.get("issues")):
            evidence[(a, b)].append("similar titles, same files")

    groups = collections.defaultdict(set)
    for (a, b) in evidence:
        groups[find(a)].update((a, b))
    out = []
    for members in groups.values():
        ev = []
        for (a, b), e in evidence.items():
            if a in members:
                ev.extend(e)
        strength = sum(int(m.group(1)) for e in ev for m in [re.match(r"(\d+) shared", e)] if m)
        out.append({"members": sorted(members), "evidence": sorted(set(ev)), "strength": strength})
    out.sort(key=lambda g: (-g["strength"], -len(g["members"])))
    return out, sorted(set(resubmissions))


# ---------------------------------------------------------------- rendering

def esc(s):
    return html.escape(str(s), quote=True)


def rich(text):
    """Escape text, then turn `code` into <code> and #123 into GitHub links."""
    s = esc(text)
    s = re.sub(r"`([^`]+)`", r'<code class="mono">\1</code>', s)
    return re.sub(r"#(\d{2,6})\b", rf'<a href="{REPO_URL}/issues/\1">#\1</a>', s)


def pr_link(n, cls="mono"):
    return f'<a class="{cls}" href="{REPO_URL}/pull/{n}">#{n}</a>'


def status_of(n, recs, prs, states):
    if n in prs:
        r = recs.get(n, {})
        if "error" in r:
            return ("open", "chip", "open")
        if prs[n]["draft"]:
            return ("draft", "chip", "draft")
        if r.get("mergeable"):
            return ("clean", "chip ok", "open · merges clean")
        return ("conflict", "chip bad", "open · conflicts now")
    st = states.get(n, {"state": "unknown"})
    if st["state"] == "merged":
        return ("merged", "chip acc", f"merged {st.get('date', '')}".strip())
    if st["state"] == "closed":
        return ("closed", "chip", f"closed {st.get('date', '')}".strip())
    return ("unknown", "chip", "status unknown")


def chip(cls, text):
    return f'<span class="{cls}">{esc(text)}</span>'


def fmt(n):
    return f"{n:,}"


def human_date(iso):
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d.strftime("%b %-d, %Y, %H:%M UTC")


def sparkline(history, key, width=520, height=64):
    points = [h[key] for h in history]
    lo, hi = min(points), max(points)
    span = max(1, hi - lo)
    step = width / max(1, len(points) - 1)
    coords = [(i * step, height - 6 - (v - lo) / span * (height - 12)) for i, v in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    x, y = coords[-1]
    return (f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" aria-label="{esc(key)} over time" '
            f'preserveAspectRatio="none"><path d="{path}" fill="none" stroke="currentColor" stroke-width="2" '
            f'vector-effect="non-scaling-stroke"/><circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="currentColor"/></svg>')


def render(ctx):
    t = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    for key, value in ctx.items():
        token = f"<!--@{key}-->" if key != "ROWS" else "/*@ROWS*/[]"
        if token not in t:
            raise SystemExit(f"template is missing {token}")
        t = t.replace(token, value)
    return t


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="where to keep the t3code clone")
    ap.add_argument("--out", required=True, help="directory that receives index.html and history.json")
    ap.add_argument("--main-ref", default="refs/remotes/origin/main")
    ap.add_argument("--pulls-file", help="read open PRs from this JSON file instead of the API")
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    pulls = json.load(open(args.pulls_file)) if args.pulls_file else list_open_pulls(token)
    prs = {p["n"]: p for p in pulls}
    print(f"{len(prs)} open PRs", flush=True)

    repo = Repo(args.work, args.main_ref)
    if not args.no_fetch:
        oldest = min(dt.datetime.fromisoformat(p["created"].replace("Z", "+00:00")) for p in pulls)
        repo.prepare(sorted(prs), (oldest - dt.timedelta(days=60)).strftime("%Y-%m-%d"))
    main_sha = repo.out("rev-parse", args.main_ref).strip()
    index_path = os.path.join(args.work, ".git", "audit-main.index")
    repo.run("read-tree", args.main_ref, env=dict(os.environ, GIT_INDEX_FILE=index_path))

    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        recs = {r["n"]: r for r in pool.map(lambda n: analyze(repo, n, index_path), sorted(prs))}
    ok = {n: r for n, r in recs.items() if "error" not in r}
    print(f"analyzed {len(ok)}, skipped {len(recs) - len(ok)}", flush=True)

    maintainers = {line.split(":", 1)[1].strip().lower()
                   for line in repo.out("show", f"{args.main_ref}:.github/TRIAGE_EXEMPTIONS.td", check=False).splitlines()
                   if line.startswith("github:")}

    def community(n):
        login = prs[n]["login"].lower()
        return login not in maintainers and not login.endswith("[bot]")

    curated = json.load(open(os.path.join(HERE, "curated.json"), encoding="utf-8"))
    curated_numbers = {curated["my_pr"]["n"]}
    curated_numbers |= {i["n"] for i in curated["merge_first"]} | {i["n"] for i in curated["take_over"]}
    for g in curated["pick_one"]:
        curated_numbers |= set(g["keep"]) | set(g["close"]) | set(g["other"])
    states = {n: pull_state(n, token) for n in sorted(curated_numbers - set(prs))} if token else {}

    # ---- headline numbers
    clean = sum(1 for r in ok.values() if r["mergeable"])
    conflict = len(ok) - clean
    moved = sum(1 for r in ok.values() if r["moved"])
    median_behind = int(statistics.median([r["behind"] for r in ok.values()])) if ok else 0
    since = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week = int(repo.out("rev-list", "--count", f"--since={since}", args.main_ref).strip())
    pct = round(conflict / max(1, len(ok)) * 100)

    hist_path = os.path.join(args.out, "history.json")
    history = json.load(open(hist_path)) if os.path.exists(hist_path) else []
    history.append({"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": len(prs), "clean": clean, "conflict": conflict})
    history = history[-1000:]

    ctx = {}
    ctx["EYEBROW"] = (f'Updated {esc(human_date(now.isoformat()))} · main at <span class="mono">{main_sha[:7]}</span>'
                      f' · refreshes every 3 hours')
    ctx["LEDE"] = (f"All {fmt(len(prs))} open pull requests, test-merged into the latest main with git. "
                   "Below are the small fixes that merge clean and stop real damage, the bugs that have several "
                   "competing PRs, and what can be closed today.")
    w_clean = clean / max(1, len(ok)) * 100
    ctx["BAR"] = (f'<div class="diffstat" aria-label="{fmt(clean)} of {fmt(len(ok))} open PRs merge clean, {fmt(conflict)} conflict">'
                  f'<div class="bar"><span class="c" style="width:{w_clean:.2f}%"></span>'
                  f'<span class="x" style="width:{100 - w_clean:.2f}%"></span></div>'
                  f'<p class="statline">{len(prs)} PRs | <span class="plus">{clean} merge clean ++++++++++++</span> '
                  f'<span class="minus">{conflict} conflict -------------</span></p></div>')
    ctx["STATS"] = "".join(f'<div class="stat"><b>{v}</b><span>{esc(label)}</span></div>' for v, label in [
        (fmt(conflict), f"conflict with main today ({pct}%)"),
        (fmt(moved), "edit files that have since moved or been deleted"),
        (fmt(median_behind), "commits behind main, for the median PR"),
        (fmt(week), "commits landed on main in the last 7 days"),
    ])

    # ---- staleness chart
    rows, rates = [], []
    for lo, hi, label in AGE_BANDS:
        band = [r for r in ok.values() if lo <= r["behind"] < hi]
        rate = round(sum(1 for r in band if not r["mergeable"]) / len(band) * 100) if band else 0
        rates.append(rate)
        rows.append(f'<div class="agerow"><span class="lbl">{label}</span><div class="track">'
                    f'<div class="fill" style="width:{rate}%"></div></div><span class="val"><b>{rate}%</b> '
                    f'<span>of {fmt(len(band))}</span></span></div>')
    ctx["AGE_ROWS"] = "".join(rows)
    per_day = week / 7 if week else 0
    days = f"about {round(700 / per_day)} days at this pace" if per_day else "a while"
    ctx["AGE_SUB"] = (f"Main moved {fmt(week)} commits in the last 7 days. Of the PRs fewer than 100 commits behind, "
                      f"{rates[0]}% conflict. Once a PR is 700 commits behind, {days}, {max(rates[3], rates[4])}% do.")

    # ---- my PR
    mine = curated["my_pr"]
    _, cls, label = status_of(mine["n"], recs, prs, states)
    ctx["MYPR"] = (f'<aside class="mine"><p class="eyebrow">My fix</p><div class="mine-row">{pr_link(mine["n"])}'
                   f'<div class="mine-body"><h3>{esc(mine["title"])}</h3><p>{rich(mine["text"])}</p>'
                   f'<div class="chips">{chip(cls, label)}</div></div></div></aside>')

    # ---- lane 1: hand-picked merges
    items, still = [], 0
    for item in curated["merge_first"]:
        kind, cls, label = status_of(item["n"], recs, prs, states)
        still += kind == "clean"
        r = recs.get(item["n"], {})
        chips = [chip(cls, label)]
        if r.get("tests"):
            chips.append(chip("chip", "tests"))
        if "src" in r:
            chips.append(chip("chip", f"{r['src']} source lines"))
        chips.append(chip("chip acc" if item["bot"] == "approved" else "chip warn", f"bot: {item['bot']}"))
        items.append(f'<article class="item{" done" if kind in ("merged", "closed") else ""}">'
                     f'<div class="num">{pr_link(item["n"], "")}<small>{esc(item["by"])}</small></div>'
                     f'<h3>{esc(item["title"])}</h3><p class="today"><b>Today:</b> {rich(item["today"])}</p>'
                     f'<div class="chips">{"".join(chips)}</div></article>')
    ctx["MERGE_HEAD"] = (f"Picked by hand on {esc(curated['picked_on'])} after reading each fix. "
                         f"{still} of {len(curated['merge_first'])} are still open and merge clean right now. "
                         "Status updates on every refresh.")
    ctx["MERGE_ITEMS"] = "".join(items)

    # ---- fresh picks
    fresh_cut = now - dt.timedelta(days=7)
    fresh = [n for n, r in ok.items()
             if community(n) and n not in curated_numbers and not prs[n]["draft"]
             and re.match(r"^(fix|perf)", prs[n]["title"]) and r["mergeable"] and r["tests"]
             and 0 < r["src"] <= 150 and not r["moved"] and not r["landed"]
             and dt.datetime.fromisoformat(prs[n]["created"].replace("Z", "+00:00")) >= fresh_cut]
    fresh.sort(key=lambda n: -n)
    fresh_html = []
    for n in fresh[:12]:
        p, r = prs[n], ok[n]
        trusted = "vouch:trusted" in p["labels"]
        fresh_html.append(f'<li><span class="row-head">{pr_link(n)} <span class="who">{esc(p["login"])}</span>'
                          f'{chip("chip acc", "trusted") if trusted else ""}</span><span class="row-title">{esc(p["title"])}</span>'
                          f'<span class="row-meta">{r["src"]} source lines · opened {p["created"][:10]}</span></li>')
    ctx["FRESH"] = ("".join(fresh_html) if fresh_html else
                    '<li><span class="row-title">No new PRs from the last 7 days pass every check right now.</span></li>')
    ctx["FRESH_SUB"] = (f"Picked automatically: {len(fresh)} PRs from the last 7 days come from outside contributors, "
                        "merge clean, ship tests and change at most 150 lines of source. "
                        + ("Showing the 12 newest. " if len(fresh) > 12 else "") + "Not reviewed by hand.")

    # ---- lane 2: curated groups, then mechanical overlaps
    def member(n, role):
        kind, _, label = status_of(n, recs, prs, states)
        cls = {"keep": "pr keep", "close": "pr close"}.get(role, "pr")
        if kind in ("merged", "closed"):
            cls += " gone"
        tag = "" if kind in ("clean", "conflict", "open", "draft") else f' <small>{esc(label)}</small>'
        if kind == "conflict":
            tag = ' <small>conflicts</small>'
        return f'<a class="{cls}" href="{REPO_URL}/pull/{n}">#{n}{tag}</a>'

    groups_html = []
    covered = set()
    for g in curated["pick_one"]:
        members = [member(n, "keep") for n in g["keep"]] + [member(n, "close") for n in g["close"]] + \
                  [member(n, "") for n in g["other"]]
        covered |= set(g["keep"]) | set(g["close"]) | set(g["other"])
        groups_html.append(f'<div class="group"><h3>{esc(g["name"])}</h3><div class="members">{" ".join(members)}</div>'
                           f'<p>{rich(g["text"])}</p></div>')
    ctx["GROUPS"] = "".join(groups_html)

    overlaps, resubs = find_overlaps(prs, recs)
    likely = []
    for g in overlaps:
        if set(g["members"]) <= covered or not any(community(n) for n in g["members"]):
            continue
        links = " ".join(pr_link(n) for n in g["members"])
        title = esc(prs[g["members"][0]]["title"])
        likely.append(f'<tr><td>{title}</td><td>{links}</td><td>{esc("; ".join(g["evidence"]))}</td></tr>')
        if len(likely) == 30:
            break
    ctx["LIKELY"] = "".join(likely) or '<tr><td colspan="3">No overlaps found in this run.</td></tr>'

    # ---- lane 3: close
    resub_html = [f'<li><span>{pr_link(a)} and {pr_link(b)}</span><span>{esc(prs[b]["title"])}</span></li>'
                  for a, b in resubs if community(a)]
    ctx["RESUB"] = "".join(resub_html) or "<li><span>None in this run.</span></li>"
    empty = [n for n, r in ok.items() if r["landed"]]
    ctx["EMPTY"] = "".join(f'<li>{pr_link(n)}<span>{esc(prs[n]["title"])}</span></li>' for n in sorted(empty)) \
        or "<li><span>None in this run.</span></li>"
    sup = []
    for g in curated["pick_one"]:
        if g["keep"] and g["close"]:
            open_closers = [n for n in g["close"] if n in prs]
            if open_closers:
                sup.append(f'<li><span>{", ".join(pr_link(n) for n in open_closers)} → '
                           f'{" ".join(member(n, "keep") for n in g["keep"])}</span></li>')
    ctx["SUPERSEDED"] = "".join(sup) or "<li><span>All closed already.</span></li>"

    # ---- lane 4: take over
    take = []
    for item in curated["take_over"]:
        kind, cls, label = status_of(item["n"], recs, prs, states)
        take.append(f'<li><span class="row-head">{pr_link(item["n"])} {chip(cls, label)}</span>'
                    f'<span class="row-title">{esc(item["title"])}</span><span>{rich(item["text"])}</span></li>')
    ctx["TAKEOVER"] = "".join(take)

    # ---- trend
    if len(history) >= 2:
        first = history[0]
        ctx["TREND"] = (f'<div class="trend"><div><p class="eyebrow">Open PRs</p><b>{fmt(len(prs))}</b>'
                        f'<span class="delta">{len(prs) - first["open"]:+,} since {first["t"][:10]}</span>'
                        f'<div class="spark-wrap open">{sparkline(history, "open")}</div></div>'
                        f'<div><p class="eyebrow">Conflicting with main</p><b>{fmt(conflict)}</b>'
                        f'<span class="delta">{conflict - first["conflict"]:+,} since {first["t"][:10]}</span>'
                        f'<div class="spark-wrap bad">{sparkline(history, "conflict")}</div></div></div>')
    else:
        ctx["TREND"] = '<p class="sub">The trend appears after the second refresh.</p>'

    # ---- hotspots
    hot = collections.Counter(f for r in ok.values() for f in r["conflicts"]).most_common(8)
    top = hot[0][1] if hot else 1
    ctx["HOT"] = "".join(f'<div class="hotrow"><code title="{esc(f)}">{esc(f)}</code><div class="track">'
                         f'<div class="fill" style="width:{k / top * 100:.1f}%"></div></div><b>{k}</b></div>'
                         for f, k in hot)
    ctx["HOT_SUB"] = f"The files named most often in merge conflicts across the {fmt(conflict)} conflicting PRs."

    # ---- every PR
    group_of = {}
    for g in overlaps:
        for n in g["members"]:
            group_of.setdefault(n, "overlap")
    for a, b in resubs:
        group_of[a] = group_of[b] = "opened twice"
    rows = []
    for n in sorted(prs):
        p, r = prs[n], recs.get(n, {})
        rows.append({
            "n": n, "t": p["title"], "a": p["login"], "c": p["created"][:10],
            "m": 1 if r.get("mergeable") else 0, "u": 1 if "error" in r else 0,
            "b": r.get("behind", 0), "ad": r.get("add", 0), "de": r.get("dele", 0),
            "x": 1 if r.get("tests") else 0, "mv": 1 if r.get("moved") else 0,
            "e": 1 if r.get("landed") else 0, "d": 1 if p["draft"] else 0,
            "v": 1 if "vouch:trusted" in p["labels"] else 0, "g": group_of.get(n, ""),
        })
    # A title containing "</script>" must not end the inline script.
    ctx["ROWS"] = json.dumps(rows, separators=(",", ":")).replace("</", "<\\/")
    ctx["ALL_SUB"] = f'All {fmt(len(prs))}, with their status against main at <span class="mono">{main_sha[:7]}</span>.'

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(ctx))
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=1)
    print(f"wrote index.html: {len(prs)} open, {clean} clean, {conflict} conflict, {len(fresh)} fresh picks, "
          f"{len(likely)} overlap groups, {len(resub_html)} resubmissions", flush=True)


if __name__ == "__main__":
    sys.exit(main())

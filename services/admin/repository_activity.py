"""Read-only repository observations. Publish one complete daily snapshot for all web workers."""

import hashlib
import json
import time
from pathlib import Path
from threading import Lock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from github_watch import PROJECTS, RepositoryReader, WatchError, owner_token, safe_text
from host_metrics import atomic_json, read_json
from sync_runtime import SyncError, canonical, git, require, safe_path

WEEK = 7 * 86400
ZONE = ZoneInfo("Asia/Shanghai")


def repo_id(value):
    return hashlib.sha256(value.lower().encode()).hexdigest()[:24]


def local_git(path, *args, **kwargs):
    return git(path, *args, timeout=12, env={"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}, **kwargs)


def output(path, *args):
    return local_git(path, *args)[1].decode().rstrip("\n")


def ref_data(path):
    fmt = "%(refname)%09%(objectname)%09%(committerdate:unix)%09%(symref)"
    refs = output(
        path, "for-each-ref", "--count=4097", "--format=" + fmt, "refs/heads", "refs/remotes/origin"
    )
    rows = refs.splitlines()
    require(len(rows) <= 4096, "分支数超过检查上限")
    heads, tracking, dates, default = {}, {}, {}, ""
    for row in rows:
        name, sha, stamp, link = row.split("\t")
        if name == "refs/remotes/origin/HEAD":
            default = link.removeprefix("refs/remotes/origin/")
        elif name.startswith("refs/heads/"):
            heads[name.removeprefix("refs/heads/")] = sha
        else:
            tracking[name.removeprefix("refs/remotes/origin/")] = sha
        dates[sha] = int(stamp or 0)
    return heads, tracking, dates, default


def worktrees(path, roots, common):
    blocks = local_git(path, "worktree", "list", "--porcelain", "-z")[1].decode().strip("\0").split("\0\0")
    require(len(blocks) <= 256, "工作目录数超过检查上限")
    items = []
    for block in blocks:
        record = dict((field.partition(" ")[0], field.partition(" ")[2]) for field in block.split("\0"))
        directory = record["worktree"]
        item = {
            "directory": directory,
            "branch": record.get("branch", "").removeprefix("refs/heads/"),
            "head": record.get("HEAD", ""),
            "dirty": None,
            "note": "",
        }
        try:
            wt = safe_path(directory, roots)
            require(wt.is_dir(), "目录不存在")
            require(
                output(wt, "rev-parse", "--path-format=absolute", "--git-common-dir") == common,
                "目录已不属于此仓库",
            )
            if not item["branch"] and item["head"]:
                item["commit_at"] = int(output(wt, "show", "-s", "--format=%ct", item["head"]))
            raw = local_git(
                wt,
                "-c",
                "diff.renames=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            )[1]
            staged = modified = new = 0
            for entry in raw.split(b"\0"):
                if entry.startswith(b"??"):
                    new += 1
                elif entry:
                    staged += entry[:1] != b" "
                    modified += entry[1:2] != b" "
            item.update(dirty=bool(raw), staged=staged, modified=modified, new=new)
        except (OSError, ValueError) as error:
            item["note"] = (
                str(error)
                if str(error) in ("目录不存在", "目录已不属于此仓库")
                else "目录未获读取授权或读取失败"
            )
        items.append(item)
    return items


def pages(reader, resource):
    rows = []
    for page in range(1, 11):
        separator = "&" if "?" in resource else "?"
        data, more = reader.get(f"{resource}{separator}per_page=100&page={page}")
        if not isinstance(data, list):
            raise WatchError("github-response-invalid")
        rows.extend(data)
        if not more:
            return rows
    raise WatchError("分页未读全")


def github_state(hub, group):
    remote = {"default": "", "heads": None, "prs": None, "merged": {}, "notes": []}
    try:
        credential = group.get("credential")
        token = hub.secret(credential) if credential else ""
        if not token and group["repository"] in PROJECTS.values():
            try:
                token = owner_token(hub.state / "github", group["repository"])
            except WatchError:
                pass
        reader = RepositoryReader(group["repository"], token or "")
        meta, _ = reader.get()
        remote["default"] = meta["default_branch"]
        remote["heads"] = {r["name"]: r["commit"]["sha"] for r in pages(reader, "/branches")}
        pulls = pages(reader, "/pulls?state=open")

        def same_repo(pr):
            return ((pr["head"].get("repo") or {}).get("full_name") or "").lower() == group[
                "repository"
            ].lower()

        remote["prs"] = [
            {
                "name": r["head"]["ref"],
                "base": r["base"]["ref"],
                "number": int(r["number"]),
                "updated": datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")).timestamp(),
            }
            for r in pulls
            if same_repo(r)
        ]
        # An exact merged PR head also covers squash/rebase integration.
        closed, _ = reader.get("/pulls?state=closed&sort=updated&direction=desc&per_page=100")
        for pr in closed:
            if pr.get("merged_at") and same_repo(pr):
                remote["merged"][(pr["head"]["sha"], pr["base"]["ref"])] = pr["number"]
    except (ValueError, OSError, KeyError, TypeError) as error:
        reason = {
            "github-http-404": "仓库不可见或尚未授权",
            "github-http-401": "凭据无效",
            "github-http-403": "访问受限",
            "rate-limited": "已达访问频率上限",
            "github-network-unconfirmed": "连接失败",
            "collection-time-budget": "读取超时",
            "github-response-too-large": "单页数据超过读取上限",
        }.get(str(error), "读取未完成")
        remote["notes"].append("GitHub " + reason)
    return remote


def candidates(hub):
    groups = {}
    roots = hub.policy()["source_roots"]
    projects = hub.registry.read_hub()["projects"].values()
    sources = hub.store.all("sources")
    entries = [(s["name"], s["directory"], s["repository"], s) for s in sources]
    registered = {s["project"]: s["repository"] for s in sources if s["project"]}
    entries.extend((p["name"], p.get("directory") or "", registered.get(p["id"], ""), {}) for p in projects)
    for name, directory, expected, source in entries:
        root = None
        repo = expected
        issue = ""
        try:
            require(
                directory and Path(directory).is_absolute() and ".." not in Path(directory).parts,
                "未登记有效的绝对路径",
            )
            # Service registrations can use a host alias. Only pass the authorized real path to Git.
            real_directory = directory if source else str(Path(directory).resolve())
            wt = safe_path(real_directory, roots)
            require(wt.is_dir(), "登记目录不存在")
            issue = "目录中未找到 Git 工作区"
            root = safe_path(output(wt, "rev-parse", "--show-toplevel"), roots)
            issue = "未找到指向 GitHub 的 origin"
            actual = canonical(output(root, "remote", "get-url", "origin"))
            require(not expected or actual.lower() == expected.lower(), "仓库来源已变化")
            repo = actual
            common = str(
                safe_path(output(root, "rev-parse", "--path-format=absolute", "--git-common-dir"), roots)
            )
            issue = ""
        except (OSError, ValueError, RuntimeError) as error:
            root = None
            reason = str(error)
            if reason in {"未登记有效的绝对路径", "登记目录不存在", "仓库来源已变化"}:
                issue = reason
            elif reason in {"XD_SYNC_PATH_NOT_GRANTED", "XD_SYNC_SYMLINK_DENIED"}:
                issue = "目录超出已授权的源码读取范围"
            else:
                issue = issue or "登记目录无法读取"
        key = repo_id(repo or "path:" + (directory or name))
        group = groups.setdefault(
            key,
            {
                "id": key,
                "name": name,
                "repository": repo,
                "projects": [],
                "roots": {},
                "issues": {},
                "directories": [],
                "credential": None,
            },
        )
        if name not in group["projects"]:
            group["projects"].append(name)
        if directory not in group["directories"]:
            group["directories"].append(directory)
        if issue:
            group["issues"][directory] = issue
        if root:
            group["roots"].setdefault(common, str(root))
        if source.get("credential"):
            group["credential"] = source["credential"]
    return list(groups.values()), roots


def contains(path, sha, target):
    # 1 means explicitly not an ancestor. Missing objects/partial history are unknown.
    code, _ = local_git(path, "merge-base", "--is-ancestor", sha, target, allowed=(0, 1, 128))
    return {0: True, 1: False}.get(code)


def activity_scope(hub, key, settings=None):
    keys = ("activity-target:" + key, "activity-release:" + key)
    with hub.store.db(write=settings is not None) as db:
        if settings is not None:
            values = [(keys[0], settings["branch"])]
            if "release" in settings:
                values.append((keys[1], settings["release"]))
            values.append(("activity-request", settings["request"]))
            db.executemany(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values,
            )
        stored = dict(db.execute("SELECT key,value FROM metadata WHERE key IN (?,?)", keys))
    return {"branch": stored.get(keys[0], ""), "release": stored.get(keys[1], "")}


def release_comparison(paths, development, release, development_sha, release_sha, basis):
    result = {"branch": release, "development": development, "basis": basis, "count": None, "note": ""}
    if development == release:
        result["note"] = "请为发布分支选择另一条分支"
    elif not development_sha or not release_sha:
        result["note"] = "开发主干或发布分支的提交待核实"
    elif development_sha == release_sha:
        result["count"] = 0
    else:
        revisions = release_sha + ".." + development_sha
        for path in paths:
            try:
                count = int(output(path, "rev-list", "--count", revisions, "--"))
                raw = output(
                    path,
                    "log",
                    "--no-show-signature",
                    "-n",
                    "25",
                    "--format=%H%x00%ct%x00%s",
                    revisions,
                    "--",
                )
            except (SyncError, OSError, ValueError):
                continue
            commits = []
            for line in raw.split("\n") if raw else []:
                sha, stamp, subject = line.split("\0", 2)
                commits.append({"sha": sha, "at": int(stamp), "subject": safe_text(subject, limit=160)})
            result["count"] = count
            return result, commits
        result["note"] = "本机缺少完整的两条分支历史"
    return result, []


def observe(hub, group, roots, now, remote_reader=github_state):
    summary = {k: group[k] for k in ("id", "name", "repository", "projects", "directories")}
    summary.update(
        branch="",
        directory="",
        active=None,
        pending=None,
        latest=None,
        target="",
        basis="",
        notes=list(dict.fromkeys(group["issues"].values())),
        directory_issues=group["issues"],
        partial=True,
        branch_count=None,
        worktree_count=None,
        at=now,
    )
    scope = activity_scope(hub, group["id"])
    override, release = scope["branch"], scope["release"]
    detail = {"summary": summary, "branches": [], "worktrees": []}
    if not group["roots"]:
        summary["notes"] = summary["notes"] or ["登记目录中未找到可读取的 GitHub 仓库"]
        return detail
    remote = remote_reader(hub, group)
    summary["notes"].extend(remote["notes"])
    local, cached, dates, defaults = {}, {}, {}, []
    locations = {}
    shallow = set()
    trees = detail["worktrees"]
    for common, path in group["roots"].items():
        heads, tracking, stamps, default = ref_data(path)
        if default:
            defaults.append(default)
        dates.update(stamps)
        if output(path, "rev-parse", "--is-shallow-repository") == "true":
            shallow.add(path)
        for refs, dest in [(heads, local), (tracking, cached)]:
            for name, sha in refs.items():
                dest.setdefault(name, set()).add(sha)
                locations.setdefault(sha, set()).add(path)
        current_trees = worktrees(path, roots, common)
        trees.extend(current_trees)
        if not summary["directory"]:
            summary["directory"] = path
            primary = next((w for w in current_trees if w["directory"] == path), {})
            summary["branch"] = primary.get("branch") or (
                "提交 " + primary["head"][:12] if primary.get("head") else ""
            )
    default_branch = remote["default"] or (defaults[0] if len(set(defaults)) == 1 else "")
    summary["default_branch"] = default_branch
    target = override or default_branch
    summary["target"] = target
    live = remote["heads"] is not None
    remote_heads = {k: {v} for k, v in remote["heads"].items()} if live else cached
    target_shas = remote_heads.get(target, set())
    summary["basis"] = "GitHub" if live else "本机保存的 origin 分支"
    if not target_shas and not live and target in local:
        target_shas = local[target]
        summary["basis"] = "本地目标分支"
    target_sha = next(iter(target_shas)) if len(target_shas) == 1 else ""
    if release:
        release_shas = remote_heads.get(release, set())
        release_basis = summary["basis"]
        if not release_shas and not live and release in local:
            release_shas = local[release]
            release_basis = "本机分支引用"
        release_sha = next(iter(release_shas)) if len(release_shas) == 1 else ""
        summary["release"], detail["release_commits"] = release_comparison(
            [path for path in group["roots"].values() if path not in shallow],
            target,
            release,
            target_sha,
            release_sha,
            release_basis,
        )
    if not target_sha:
        summary["notes"].append("目标分支待确认")
    if not live:
        summary["notes"].append("推送与合入状态仅按本机引用判断")
    prs = remote["prs"] or []
    names = set(local) | set(remote_heads) | {pr["name"] for pr in prs}
    if any(sha not in dates for tips in remote_heads.values() for sha in tips):
        summary["notes"].append("部分远端提交尚无本地记录，活动时间或合入关系待核实")
    require(len(names) <= 4096, "分支数超过检查上限")
    ancestry = {}

    def ancestor(sha, tip):
        if sha == tip:
            return True
        key = (sha, tip)
        if key not in ancestry:
            ancestry[key] = None
            for path in sorted(locations.get(sha, group["roots"].values())):
                result = contains(path, sha, tip)
                if result is not None and (result or path not in shallow):
                    ancestry[key] = result
                    break
        return ancestry[key]

    def merged(sha):
        if (sha, target) in remote["merged"]:
            return True
        if not target_sha:
            return None
        return ancestor(sha, target_sha)

    def push_status(ours, theirs):
        if not ours:
            return "仅远端有分支"
        if not theirs:
            return "尚未推送同名分支" if live else "本机无同名远端记录"
        if len(ours) != 1 or len(theirs) != 1:
            return "不同副本的提交不一致"
        local_tip, remote_tip = next(iter(ours)), next(iter(theirs))
        if local_tip == remote_tip:
            return "本地与远端一致"
        ahead = ancestor(remote_tip, local_tip)
        behind = ancestor(local_tip, remote_tip)
        if ahead:
            return "本地有待推送提交"
        if behind:
            return "远端有更新"
        return "本地与远端已分叉" if ahead is False and behind is False else "推送状态待核实"

    for name in sorted(names):
        ours = local.get(name, set())
        theirs = remote_heads.get(name, set())
        tips = ours | theirs
        if name in (target, release):
            merge = "release" if name != target else "target"
        else:
            results = {merged(sha) for sha in tips}
            merge = "pending" if False in results else "merged" if results == {True} else "unknown"
        linked = [w for w in trees if w["branch"] == name]
        pull = [pr for pr in prs if pr["name"] == name]
        commit_dates = {s: dates.get(s) for s in tips}
        stamp = max((v for v in commit_dates.values() if v), default=0)
        reasons = []
        if name not in (target, release):
            if any(w["dirty"] for w in linked):
                reasons.append("工作区有改动")
            if pull:
                reasons.append("有开放 PR")
            if merge == "pending" and any(
                now - WEEK <= dates.get(s, 0) <= now and merged(s) is False for s in tips
            ):
                reasons.append("近 7 天有待合入提交")
        push = push_status(ours, theirs)
        if name == target:
            summary["target_push"] = push
        latest = max([stamp] + [pr["updated"] for pr in pull])
        detail["branches"].append(
            {
                "name": name,
                "sha": sorted(ours or theirs),
                "commit_at": stamp or None,
                "tip_dates": commit_dates,
                "remote_sha": sorted(theirs),
                "latest": latest or None,
                "merge": merge,
                "push": push,
                "prs": pull,
                "prs_known": remote["prs"] is not None,
                "reasons": reasons,
                "directories": [w["directory"] for w in linked],
            }
        )
    branches = detail["branches"]
    branches.sort(key=lambda b: (not b["reasons"], -(b["latest"] or 0), b["name"]))
    summary.update(
        active=sum(bool(b["reasons"]) for b in branches),
        pending=sum(b["merge"] == "pending" for b in branches) if target_sha else None,
        unknown_merges=sum(b["merge"] == "unknown" for b in branches),
        latest=max([b["latest"] or 0 for b in branches] + [w.get("commit_at", 0) for w in trees], default=0)
        or None,
        worktree_count=len(trees),
        branch_count=len(branches),
        override=override or "",
        partial=bool(summary["notes"])
        or not live
        or remote["prs"] is None
        or not target_sha
        or any(b["merge"] == "unknown" for b in branches)
        or any(w["dirty"] is None for w in trees),
    )
    return detail


def slot(now):
    return (datetime.fromtimestamp(now, ZONE) - timedelta(hours=2)).date().isoformat()


class ActivityMonitor:
    def __init__(self, hub, remote_reader=github_state):
        self.hub = hub
        self.file = hub.root / "repository-activity.json"
        self.reader = remote_reader
        self.cached = None
        self.signature = None
        self.lock = Lock()

    def snapshot(self):
        with self.lock:
            try:
                st = self.file.stat()
            except FileNotFoundError:
                return {}
            signature = (st.st_ino, st.st_mtime_ns)
            if self.signature != signature:
                value = read_json(self.file)
                self.cached = value
                self.signature = signature
            return self.cached

    def status(self):
        value = self.snapshot()
        progress = json.loads(self.hub.store.metadata("activity-progress") or "{}")
        return {
            "at": value.get("at"),
            "repositories": [d["summary"] for d in value.get("repos", {}).values()],
            "progress": progress,
            "pending": (self.hub.store.metadata("activity-request") or "") != value.get("request", ""),
        }

    def collect(self, now=None, stopping=None):
        now = time.time() if now is None else now
        groups, roots = candidates(self.hub)
        repos = {}
        request = self.hub.store.metadata("activity-request") or ""
        for i, g in enumerate(groups):
            if stopping and stopping.is_set():
                return
            self.progress("running", i, len(groups), g["name"])
            try:
                d = observe(self.hub, g, roots, now, self.reader)
            except (OSError, ValueError, KeyError, TypeError) as error:
                d = observe(self.hub, {**g, "roots": {}}, roots, now, self.reader)
                d["summary"]["notes"] = [
                    "仓库检查未完成（" + (str(error) if isinstance(error, SyncError) else "读取失败") + "）"
                ]
            repos[g["id"]] = d
        atomic_json(self.file, {"at": now, "slot": slot(now), "request": request, "repos": repos})
        self.progress("complete", len(groups), len(groups), "")

    def progress(self, state, done, total, name):
        self.hub.store.metadata(
            "activity-progress",
            json.dumps(dict(state=state, done=done, total=total, name=name, at=time.time())),
        )

    def run(self, stopping):
        while not stopping.is_set():
            try:
                snap = self.snapshot()
                if snap.get("slot") != slot(time.time()) or snap.get("request", "") != (
                    self.hub.store.metadata("activity-request") or ""
                ):
                    self.collect(stopping=stopping)
            except (OSError, ValueError, KeyError, TypeError):
                self.progress("failed", 0, 0, "")
                stopping.wait(60)
            stopping.wait(5)

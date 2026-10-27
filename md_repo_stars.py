#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""md-repo-stars — 提取 Markdown 中提到的所有 GitHub 仓库，并按 stars 排序。

零依赖（纯标准库），支持:

  * 链接形态: https://github.com/OWNER/REPO (含 .git / 锚点 / 子路径 / 徽章)
  * 短名形态: 行内或代码块中的 OWNER/REPO
  * 忽略: issues / pull / tree / blob / releases / topics / users / sponsors ...
  * 鉴权: --token 或 GITHUB_TOKEN / GH_TOKEN
  * 缓存: ETag 条件请求，304 复用，stars 落盘
  * 离线: --offline + --map 星星表（本地 JSON/CSV）也能排
  * 输出: table / markdown / json / csv，可 --output 落盘

示例:
    python md_repo_stars.py README.md
    python md_repo_stars.py docs/*.md --format markdown --output stars.md
    python md_repo_stars.py README.md --offline --map stars.json --verbose
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

VERSION = "1.0.0"
UA = f"md-repo-stars/{VERSION} (+https://github.com)"

DEFAULT_CACHE = Path.home() / ".cache" / "md-repo-stars" / "stars.json"
API_ROOT = "https://api.github.com"
PER_PAGE = 100  # /search/repositories 上限

# ---------------------------------------------------------------- 正则 / 常量

# owner: 1-39 字符，字母数字与内部连字符；repo: 1-100 字符，字母数字 . _ -
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPO = r"[A-Za-z0-9._-]{1,100}"

# 第一段路径若命中，则不是仓库
RESERVED_OWNERS = {
    "about", "account", "apps", "assets", "blog", "codespaces", "collections",
    "contact", "customer-stories", "dashboard", "discussions", "downloads",
    "education", "enterprise", "events", "explore", "features", "gist", "git-guides",
    "github", "home", "issues", "join", "login", "logout", "marketplace", "mobile",
    "new", "notifications", "open-source", "organizations", "orgs", "pricing",
    "pulls", "readme", "search", "security", "sessions", "settings", "signup",
    "site", "sponsors", "stars", "team", "topics", "trending", "users", "watching",
}

# 第二段路径若命中，说明指向仓库内部页面而非仓库本体
RESERVED_REPOS = {
    "issues", "pulls", "pull", "tree", "blob", "releases", "tags", "wiki",
    "actions", "packages", "projects", "discussions", "graphs", "network",
    "compare", "commits", "commit", "branches", "forks", "stargazers", "watchers",
    "raw", "archive", "deployments", "security", "pulse", "activity", "search",
    "about", "settings", "labels", "milestones", "contributors", "codespaces",
}

URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?github\.com/"
    rf"(?P<owner>{_OWNER})/(?P<repo>{_REPO})"
    rf"(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)

SHORT_RE = re.compile(rf"(?<![A-Za-z0-9._/-])(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?![A-Za-z0-9._/-])")

FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
LINK_TEXT_RE = re.compile(r"(?<!!)\[([^\]\n]{1,120})\]\(([^)\n]*)\)")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# 常见目录名，出现在裸名左侧时基本是路径
PATH_WORDS = {
    "app", "src", "lib", "bin", "build", "dist", "docs", "doc", "data", "demo",
    "examples", "example", "tests", "test", "config", "configs", "assets", "static",
    "public", "scripts", "tools", "vendor", "node_modules", "package", "packages",
    "internal", "cmd", "pkg", "api", "web", "www", "tmp", "temp", "out", "target",
    "docker", "deploy", "ci", "env", "logs", "cache", "images", "img", "fonts",
}

# 文件名后缀：命中即认为是路径而不是仓库
FILE_SUFFIXES = {
    "md", "markdown", "txt", "rst", "json", "yaml", "yml", "toml", "ini", "cfg",
    "csv", "tsv", "log", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "java",
    "kt", "kts", "go", "rs", "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php",
    "sh", "bash", "zsh", "ps1", "bat", "sql", "db", "sqlite", "png", "jpg",
    "jpeg", "gif", "svg", "webp", "ico", "pdf", "zip", "tar", "gz", "whl",
    "lock", "gradle", "properties", "html", "htm", "css", "scss", "vue", "svelte",
    "exe", "dll", "so", "dylib", "ipynb", "xml", "plist", "pem", "key", "env",
}

# 徽章/图片里的伪路径，例如 img.shields.io/github/stars/OWNER/REPO
BADGE_RE = re.compile(
    rf"(?:img\.shields\.io|badgen\.net|badge\.fury\.io|flat\.badge\.fury\.io)"
    rf"[^\s)\"'>]*?/(?:github/)?[\w-]+/(?P<owner>{_OWNER})/(?P<repo>{_REPO})",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 数据结构

@dataclass
class Ref:
    """一处引用（去重前）。"""

    owner: str
    repo: str
    source: str
    line: int


@dataclass
class Repo:
    """去重后的仓库，附带收集到的出处与 stars 结果。"""

    key: str                      # 规范化的 owner/repo（保留原始大小写中的 owner/repo）
    owner: str
    repo: str
    refs: list[Ref] = field(default_factory=list)
    stars: Optional[int] = None
    desc: str = ""
    updated: str = ""
    url: str = ""
    status: str = "pending"       # ok / cached / not_found / rate_limited / error / offline / pending
    error: str = ""

    @property
    def folder(self) -> str:
        return f"{self.owner}/{self.repo}"


# ---------------------------------------------------------------- 提取

def normalize(owner: str, repo: str) -> Optional[str]:
    owner, repo = owner.strip(), repo.strip()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".,;:!?)]}>'\"")
    owner = owner.rstrip(".,;:!?)]}>'\"")
    if not owner or not repo:
        return None
    if owner.lower() in RESERVED_OWNERS:
        return None
    if repo.lower() in RESERVED_REPOS:
        return None
    if not re.fullmatch(_OWNER, owner) or not re.fullmatch(_REPO, repo):
        return None
    return f"{owner}/{repo}"


def looks_like_path(owner: str, repo: str) -> bool:
    """判断裸写的 a/b 更像文件路径而不是仓库名。"""
    if re.search(r"\.[A-Za-z0-9]{1,8}$", repo) and repo.rsplit(".", 1)[-1].lower() in FILE_SUFFIXES:
        return True
    if owner.isdigit() and repo.isdigit():          # 3/11 之类的数字比
        return True
    if owner.lower() in PATH_WORDS:                 # app/, src/, data/ 之类目录前缀
        return True
    if "." in owner or owner.startswith((".", "~")) or "/" in owner:
        return True
    if repo.startswith(".") or repo.endswith("."):
        return True
    return False


def extract_urls(line: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []

    def add(owner: str, repo: str) -> None:
        key = normalize(owner, repo)
        if key:
            hits.append(tuple(key.split("/", 1)))  # type: ignore[arg-type]

    for m in URL_RE.finditer(line):
        add(m.group("owner"), m.group("repo"))
    for m in BADGE_RE.finditer(line):
        add(m.group("owner"), m.group("repo"))
    return hits


def looks_like_repo_name(owner: str, repo: str) -> bool:
    """仓库名风格校验：过滤 `charStart/charEnd`、`Kotlin/Java`、`3/11` 这类代码片段与路径。

    GitHub 用户名只能是字母数字和连字符，这里再要求全小写，能挡掉绝大多数散文/标识符。
    """
    if not owner or not repo:
        return False
    if not re.search(r"[A-Za-z]", owner) or not re.search(r"[A-Za-z]", repo):
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", owner):
        return False
    return True


def extract_short(
    span: str,
    label: bool = False,
    code: bool = False,
    prose: bool = False,
    lenient: bool = False,
) -> list[tuple[str, str]]:
    """从一段文本里抓裸写的 owner/repo。

    label=True  行内代码 / 链接文本：整段就是仓库名即收
    code=True   代码块：整段即收；零散出现需强信号（或 --code-names）
    prose=True  正文：整段且带强信号才收，避免把 a/b 路径当仓库
    """
    hits: list[tuple[str, str]] = []
    stripped = span.strip().strip("`'\"()[]{}<>*_ ")
    for m in SHORT_RE.finditer(span):
        owner, repo = m.group("owner"), m.group("repo")
        if span[max(0, m.start() - 11):m.start()].lower().endswith("github.com/"):
            continue
        if prose:
            before = span[m.start() - 1] if m.start() > 0 else " "
            after = span[m.end()] if m.end() < len(span) else " "
            if before.isalnum() or before in "/-._" or after.isalnum() or after in "/-._":
                continue
        if looks_like_path(owner, repo):
            continue
        # 整段就是仓库名：去掉这段后两侧只剩分隔符
        whole = _is_bare_token(span, m)
        if not looks_like_repo_name(owner, repo):
            continue
        if not lenient or prose:
            if label and not (_strong_signal(owner, repo) or _twin_name(owner, repo)):
                continue
            if prose and not (whole and _strong_signal(owner, repo)):
                continue
            if code and not whole and not _strong_signal(owner, repo):
                continue
        key = normalize(owner, repo)
        if key:
            hits.append(tuple(key.split("/", 1)))  # type: ignore[arg-type]
    return hits


def _is_bare_token(span: str, match: re.Match) -> bool:
    """匹配两侧是否只有分隔符（即这一段就是 owner/repo）。"""
    rest = (span[: match.start()] + span[match.end():]).strip().strip("`'\"()[]{}<>*_ .,;:!?")
    return rest == ""


def _strong_signal(owner: str, repo: str) -> bool:
    """owner/repo 是否带有「像 GitHub 仓库」的强特征。"""
    return bool(
        "-" in owner
        or owner.endswith("bot")
        or ".git" in repo.lower()
        or (re.search(r"[A-Z]", repo) and re.search(r"[a-z]", repo) and "-" in repo)
    )


def _twin_name(owner: str, repo: str) -> bool:
    """仓库名与账号高度相似，例如 tqdm/tqdm、BurntSushi/ripgrep 之外的 owner/project-name。

    经验上 GitHub 项目名常与账号同名或明显更长（webpack/webpack、ollama/ollama），
    而 `gk/cet4`、`primary/primaryContainer` 这类代码片段两段互不相干。
    """
    a, b = owner.lower().replace("-", ""), repo.lower().replace("-", "")
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        short, long = sorted((len(a), len(b)))
        if short * 2 >= long:
            return True


def scan_text(text: str, source: str, code_names: bool = False) -> list[Ref]:
    """扫描整份 Markdown。

    默认只信任「有明确仓库信号」的写法：
      * 任意位置的 GitHub URL / 徽章；
      * 代码块、行内代码、链接文本里的 owner/repo（优先）；
      * 正文里需两侧独立成词。

    `app/build.gradle.kts`、`gk/cet4`、`Kotlin/Java` 这类路径/标识符会被过滤；
    code_names=True 时放宽代码块里的裸名识别（仍然过滤路径与文件名）。
    """
    text = HTML_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    refs: list[Ref] = []
    in_fence = False
    for no, raw in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        found = extract_urls(raw)
        prose = raw
        if in_fence:
            found += extract_short(raw, code=True, lenient=code_names)
        else:
            # 行内代码：整段就是仓库名才算；解析后从正文里挖空，避免重复/误判
            for code in INLINE_CODE_RE.findall(raw):
                found += extract_short(code, label=True, lenient=code_names)
                prose = prose.replace(f"`{code}`", " ")
            # 链接文本 [owner/repo](...)：只在文本段内匹配
            for label, _target in LINK_TEXT_RE.findall(raw):
                found += extract_short(label, label=True, lenient=code_names)
                prose = prose.replace(label, " ")
            # 正文里的裸名：逐词匹配（不跨空白），整词且带强信号
            for word in prose.split():
                found += extract_short(word, prose=True, lenient=code_names)
        for owner, repo in found:
            refs.append(Ref(owner, repo, source, no))
    return refs


def collect(files: Iterable[Path], code_names: bool = False) -> dict[str, Repo]:
    repos: dict[str, Repo] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for ref in scan_text(text, str(path), code_names=code_names):
            key = f"{ref.owner}/{ref.repo}"
            repo = repos.get(key.lower())
            if repo is None:
                repo = Repo(key=key, owner=ref.owner, repo=ref.repo)
                repos[key.lower()] = repo
            repo.refs.append(ref)
    return repos


# ---------------------------------------------------------------- 缓存

class Cache:
    def __init__(self, path: Optional[Path]):
        self.path = path
        self.data: dict[str, dict] = {}
        self.dirty = False
        self.lock = threading.Lock()
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.data = raw.get("repos", raw) if isinstance(raw, dict) else {}
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def get(self, key: str) -> Optional[dict]:
        entry = self.data.get(key.lower())
        if entry and "stars" in entry:
            return entry
        return None

    def put(self, key: str, entry: dict) -> None:
        with self.lock:
            self.data[key.lower()] = {**entry, "synced_at": int(time.time())}
            self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = {"version": 1, "repos": self.data}
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def load_map(path: Path) -> dict[str, dict]:
    """载入本地星星表：JSON（{repo: stars} / {"repos": {...}} / 本工具缓存结构）或 CSV（owner/repo,stars）。"""
    out: dict[str, dict] = {}
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if len(row) < 2:
                    continue
                name, val = row[0].strip(), row[1].strip()
                if name.lower() in {"repo", "name", "project", "仓库", "owner/repo"}:
                    continue
                if "/" not in name:
                    continue
                try:
                    out[name.lower()] = {"stars": int(float(val))}
                except ValueError:
                    continue
        return out

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("repos"), dict):
        raw = raw["repos"]
    if not isinstance(raw, dict):
        return out
    for name, val in raw.items():
        if not isinstance(name, str) or "/" not in name:
            continue
        if isinstance(val, dict):
            if "stars" not in val:
                continue
            out[name.lower()] = dict(val)
        elif isinstance(val, (int, float)):
            out[name.lower()] = {"stars": int(val)}
    return out


# ---------------------------------------------------------------- GitHub API

class RateLimiter:
    """遇到 X-RateLimit 用尽或 Retry-After 时，统一决定是否放弃。"""

    def __init__(self) -> None:
        self.deadline: Optional[float] = None
        self.reason = ""
        self.lock = threading.Lock()

    def trip(self, until: float, reason: str) -> None:
        with self.lock:
            if self.deadline is None or until < self.deadline:
                self.deadline = until
            self.reason = reason

    def tripped(self) -> bool:
        return self.deadline is not None


class GitHub:
    def __init__(
        self,
        token: Optional[str],
        timeout: float,
        retries: int,
        offline: bool,
        cache: Cache,
        max_age: float,
        refresh: bool,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self.cache = cache
        self.max_age = max_age
        self.refresh = refresh
        self.limiter = RateLimiter()
        self.requests = 0
        self.network_errors = 0
        self.cache_hits = 0
        self._own_token_hint = token is None

    # -- 单个仓库 --------------------------------------------------

    def lookup(self, repo: Repo) -> None:
        key = repo.key
        cached = self.cache.get(key)

        fresh = bool(cached) and (time.time() - cached.get("synced_at", 0) < self.max_age * 86400)
        if cached and fresh and not self.refresh:
            self._apply_cache(repo, cached, "cached")
            return

        if self.offline:
            repo.status = "offline"
            repo.error = "离线模式且本地无数据"
            return

        if self.limiter.tripped():
            repo.status = "rate_limited"
            repo.error = self.limiter.reason
            if cached:
                self._apply_cache(repo, cached, "cached")
                repo.error = f"{self.limiter.reason}（显示缓存值）"
            return

        url = f"{API_ROOT}/repos/{key}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": UA,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]

        attempt = 0
        while True:
            attempt += 1
            try:
                self.requests += 1
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    etag = resp.headers.get("ETag", "")
                repo.stars = int(payload.get("stargazers_count", 0))
                repo.desc = (payload.get("description") or "").strip()
                repo.updated = (payload.get("pushed_at") or payload.get("updated_at") or "")[:10]
                repo.url = payload.get("html_url", f"https://github.com/{key}")
                repo.status = "ok"
                self.cache.put(
                    key,
                    {
                        "stars": repo.stars,
                        "desc": repo.desc,
                        "updated": repo.updated,
                        "url": repo.url,
                        "etag": etag,
                        "full_name": payload.get("full_name", key),
                    },
                )
                return

            except urllib.error.HTTPError as exc:
                code = exc.code
                if code == 304 and cached:
                    self.cache_hits += 1
                    self._apply_cache(repo, cached, "cached")
                    self.cache.put(key, {k: v for k, v in cached.items() if k != "synced_at"})
                    return
                if code == 404:
                    repo.status = "not_found"
                    repo.error = "仓库不存在或已私有（404）"
                    return
                if code in (403, 429):
                    reset = self._rate_reset(exc.headers)
                    remaining = exc.headers.get("X-RateLimit-Remaining")
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait = int(retry_after)
                    else:
                        wait = max(1, int(reset - time.time()))
                    hint = "未提供 token" if self._own_token_hint else "token 配额已用尽"
                    reason = (
                        f"GitHub API 限流（{hint}），约 {wait}s 后恢复"
                    )
                    self.limiter.trip(time.time() + wait, reason)
                    repo.status = "rate_limited"
                    repo.error = reason
                    if cached:
                        self._apply_cache(repo, cached, "cached")
                        repo.error += "（显示缓存值）"
                    return
                if code >= 500 and attempt <= self.retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                repo.status = "error"
                repo.error = f"HTTP {code}"
                return

            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.network_errors += 1
                if attempt <= self.retries:
                    time.sleep(min(1.5 * attempt, 8))
                    continue
                repo.status = "error"
                repo.error = f"请求失败: {getattr(exc, 'reason', exc)}"
                if cached:
                    self._apply_cache(repo, cached, "cached")
                    repo.error += "（显示缓存值）"
                return

    @staticmethod
    def _rate_reset(headers) -> float:
        try:
            return float(headers.get("X-RateLimit-Reset", time.time()))
        except (TypeError, ValueError):
            return time.time()

    @staticmethod
    def _apply_cache(repo: Repo, cached: dict, status: str) -> None:
        repo.stars = int(cached.get("stars", 0))
        repo.desc = cached.get("desc", "")
        repo.updated = cached.get("updated", "")
        repo.url = cached.get("url", f"https://github.com/{repo.key}")
        if repo.status in ("pending", "offline"):
            repo.status = status
            repo.error = ""

    # -- 批量 ------------------------------------------------------

    def fetch_all(self, repos: list[Repo], workers: int) -> None:
        if not repos:
            return
        done = 0
        total = len(repos)
        tty = sys.stderr.isatty()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.lookup, r): r for r in repos}
            for fut in as_completed(futures):
                done += 1
                if tty:
                    print(f"\r  抓取 {done}/{total} …", end="", file=sys.stderr, flush=True)
        if tty:
            print("\r" + " " * 24 + "\r", end="", file=sys.stderr)


# ---------------------------------------------------------------- 输出

def sort_repos(repos: Iterable[Repo], sort: str) -> list[Repo]:
    items = list(repos)
    if sort == "name":
        return sorted(items, key=lambda r: r.folder.lower())
    if sort == "updated":
        return sorted(items, key=lambda r: (r.updated or "", -1), reverse=True)
    return sorted(items, key=lambda r: (-(r.stars if r.stars is not None else -1), r.folder.lower()))


def fmt_stars(repo: Repo) -> str:
    if repo.stars is None:
        return "—"
    s = repo.stars
    if s >= 1_000_000:
        return f"{s/1_000_000:.1f}m".replace(".0m", "m")
    if s >= 1_000:
        return f"{s/1_000:.1f}k".replace(".0k", "k")
    return str(s)


def wrap(text: str, width: int) -> str:
    if width <= 0 or len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def render_table(repos: list[Repo], verbose: bool) -> str:
    rows = [(" #", "★ Stars", "仓库", "描述", "最近更新")]
    for i, r in enumerate(repos, 1):
        desc = wrap(r.desc, 58) + (f"  [{r.error}]" if r.error and verbose else "")
        rows.append((f"{i:>4}", f"{fmt_stars(r):>8}", r.folder, desc, r.updated or "—"))
    widths = [max(len(row[c]) for row in rows) for c in range(len(rows[0]))]
    out = []
    for idx, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[c]) if c in (2, 3) else cell.rjust(widths[c]) for c, cell in enumerate(row))
        out.append(line.rstrip())
        if idx == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def render_markdown(repos: list[Repo], verbose: bool) -> str:
    out = ["| # | ★ Stars | 仓库 | 描述 | 最近更新 |", "|---:|---:|---|---|---|"]
    for i, r in enumerate(repos, 1):
        desc = (r.desc or "").replace("|", "\\|")
        if verbose and r.error:
            desc += f" `{r.error}`".replace("|", "\\|")
        out.append(
            f"| {i} | {fmt_stars(r)} | [{r.folder}](https://github.com/{r.folder}) | {desc} | {r.updated or '—'} |"
        )
    return "\n".join(out)


def render_json(repos: list[Repo], summary: dict) -> str:
    body = {
        "summary": summary,
        "repos": [
            {
                "rank": i,
                "full_name": r.folder,
                "stars": r.stars,
                "description": r.desc,
                "pushed_at": r.updated,
                "url": r.url or f"https://github.com/{r.folder}",
                "status": r.status,
                "error": r.error or None,
                "sources": [{"file": ref.source, "line": ref.line} for ref in r.refs],
            }
            for i, r in enumerate(repos, 1)
        ],
    }
    return json.dumps(body, ensure_ascii=False, indent=2)


def render_csv(repos: list[Repo]) -> str:
    import io

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["rank", "full_name", "stars", "description", "pushed_at", "url", "status"])
    for i, r in enumerate(repos, 1):
        writer.writerow(
            [i, r.folder, "" if r.stars is None else r.stars, r.desc, r.updated,
             r.url or f"https://github.com/{r.folder}", r.status]
        )
    return buf.getvalue().rstrip("\n")


def render_verbose_sources(repos: list[Repo]) -> str:
    lines = []
    for r in repos:
        where = ", ".join(sorted({f"{Path(ref.source).name}:{ref.line}" for ref in r.refs}))
        lines.append(f"  {r.folder:<40} ← {where}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="md-repo-stars",
        description="提取 Markdown 中提到的所有 GitHub 仓库，按 stars 数排序。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python md_repo_stars.py README.md
  python md_repo_stars.py README.md docs/*.md --format markdown --output STARS.md
  python md_repo_stars.py README.md --offline --map stars.csv
  GITHUB_TOKEN=ghp_xxx python md_repo_stars.py huge-list.md --workers 12
""",
    )
    p.add_argument("files", nargs="+", help="输入 Markdown 文件（支持通配符展开）")
    p.add_argument("--format", "-f", choices=["table", "markdown", "json", "csv"], default="table",
                   help="输出格式，默认 table")
    p.add_argument("--sort", choices=["stars", "name", "updated"], default="stars",
                   help="排序方式，默认 stars 降序")
    p.add_argument("--output", "-o", help="结果写入文件（默认打印到 stdout）")
    p.add_argument("--top", "-n", type=int, default=0, help="只显示前 N 个（0 = 全部）")
    p.add_argument("--token", help="GitHub token；默认读 GITHUB_TOKEN / GH_TOKEN")
    p.add_argument("--workers", "-j", type=int, default=8, help="并发请求数，默认 8")
    p.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数，默认 15")
    p.add_argument("--retries", type=int, default=2, help="失败重试次数，默认 2")
    p.add_argument("--cache", default=str(DEFAULT_CACHE), help=f"缓存文件路径，默认 {DEFAULT_CACHE}")
    p.add_argument("--no-cache", action="store_true", help="不读不写缓存")
    p.add_argument("--max-age", type=float, default=7, help="缓存有效期（天），默认 7")
    p.add_argument("--refresh", action="store_true", help="忽略缓存新鲜度，强制回源")
    p.add_argument("--offline", action="store_true", help="完全离线，仅用本地数据/--map")
    p.add_argument("--map", dest="map_file", help="本地星星表（JSON 或 CSV），离线排序用")
    p.add_argument("--quiet", action="store_true", help="不打印统计信息到 stderr")
    p.add_argument("--verbose", "-v", action="store_true", help="输出引用出处、错误原因")
    p.add_argument("--code-names", action="store_true",
                   help="放宽：代码块里零散的 owner/repo 也算（默认只认整段或带强信号）")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    paths: list[Path] = []
    for pattern in args.files:
        candidate = Path(pattern)
        if candidate.is_file():
            paths.append(candidate)
            continue
        matched = sorted(glob.glob(pattern, recursive=True))
        if not matched:
            print(f"警告: 找不到文件 {pattern}", file=sys.stderr)
        paths.extend(Path(m) for m in matched if Path(m).is_file())
    if not paths:
        print("错误: 没有可读取的 Markdown 文件", file=sys.stderr)
        return 2

    started = time.time()
    repos_map = collect(paths, code_names=args.code_names)
    repos = list(repos_map.values())

    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))
    log(f"📄 扫描 {len(paths)} 个文件，发现 {len(repos)} 个唯一仓库")

    if not repos:
        print(
            "⚠️  没有识别到任何 GitHub 仓库。\n"
            "   - 若仓库是裸写的 owner/repo，加 --code-names 放宽；\n"
            "   - 链接请用 https://github.com/OWNER/REPO 形式。",
            file=sys.stderr,
        )
        return 0

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        token = token.strip() or None

    cache = Cache(None if args.no_cache else Path(args.cache).expanduser())
    gh = GitHub(
        token=token,
        timeout=args.timeout,
        retries=args.retries,
        offline=args.offline,
        cache=cache,
        max_age=args.max_age,
        refresh=args.refresh,
    )

    # 本地星星表优先填充，网络结果可覆盖
    hints: dict[str, dict] = {}
    if args.map_file:
        map_path = Path(args.map_file).expanduser()
        try:
            hints = load_map(map_path)
            log(f"🗂  载入本地星星表 {map_path}（{len(hints)} 条）")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"警告: 读取 --map 失败: {exc}", file=sys.stderr)
    for repo in repos:
        hint = hints.get(repo.key.lower())
        if hint:
            repo.stars = int(hint["stars"])
            repo.desc = hint.get("desc", "") or repo.desc
            repo.updated = hint.get("updated", "") or repo.updated
            repo.url = hint.get("url", "") or repo.url
            repo.status = "offline" if args.offline else "cached"

    to_fetch = [r for r in repos if r.stars is None]
    if to_fetch and not args.offline:
        gh.fetch_all(to_fetch, args.workers)

    for repo in repos:
        if repo.stars is None and repo.status == "pending":
            repo.status = "offline"
            repo.error = repo.error or "未取到 stars 数据"

    ordered = sort_repos(repos, args.sort)
    ok = [r for r in repos if r.stars is not None]
    shown = ordered[: args.top] if args.top and args.top > 0 else ordered
    summary = {
        "files": [str(p) for p in paths],
        "unique_repos": len(repos),
        "with_stars": len(ok),
        "missing": len(repos) - len(ok),
        "requests": gh.requests,
        "cache_hits": gh.cache_hits,
        "elapsed_sec": round(time.time() - started, 2),
        "sorted_by": args.sort,
    }

    if args.format == "json":
        text = render_json(ordered, {**summary, "shown": shown})
    elif args.format == "csv":
        text = render_csv(shown)
    elif args.format == "markdown":
        text = render_markdown(shown, args.verbose)
    else:
        text = render_table(shown, args.verbose)
        if args.verbose:
            text += "\n\n引用出处:\n" + render_verbose_sources(shown)

    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        log(f"💾 已写入 {out_path}")
    else:
        print(text)

    log(
        f"✅ {len(repos)} 个仓库 · {len(ok)} 个取到 stars · {len(repos) - len(ok)} 个缺失 · "
        f"{gh.requests} 次请求 / {gh.cache_hits} 次缓存命中 · {summary['elapsed_sec']}s"
    )
    if gh.limiter.tripped():
        print(f"⚠️  {gh.limiter.reason}；可设置 GITHUB_TOKEN 提升配额，或稍后重跑（缓存已保留）。", file=sys.stderr)
    if gh.network_errors and not args.quiet:
        print(f"⚠️  {gh.network_errors} 次网络请求失败（已重试 {args.retries} 次）。", file=sys.stderr)

    if not args.no_cache:
        cache.save()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)

# md-repo-stars

> Pull every GitHub repo your Markdown mentions, then rank them by stars — a zero-dependency CLI with caching and an offline mode.
>
> 把 Markdown 里提到的 GitHub 仓库全部抓出来，按 **stars** 排序——零依赖命令行工具，带缓存与离线模式。

纯 Python 标准库，Windows / macOS / Linux 直接跑；stars 走 GitHub API，本地缓存 7 天，未认证撞限流也不丢已抓结果。

```
   #   ★ Stars  仓库                            描述                                                  最近更新
----  --------  ----------------------------  ----------------------------------------------------  ----------
   1    225.6k  TheAlgorithms/Python          All Algorithms implemented in Python                  2026-10-20
   2    202.9k  yt-dlp/yt-dlp                 A feature-rich command-line audio/video downloader    2026-10-26
   3    194.4k  microsoft/vscode              Visual Studio Code                                    2026-10-27
   ...
```

## 用法

```bash
# 最常用：排序并打印
python md_repo_stars.py README.md

# 生成一份 Markdown 排名表
python md_repo_stars.py notes.md --format markdown -o STARS.md

# 一次扫描多个文件 / 整个目录
python md_repo_stars.py "docs/**/*.md" --format csv -o stars.csv

# 只看前 10，带引用出处
python md_repo_stars.py README.md --top 10 -v

# 有 token 时配额 5000 次/小时，速度快很多
GITHUB_TOKEN=ghp_xxx python md_repo_stars.py awesome-list.md --workers 12
```

Windows 上也可以直接：

```bat
md-repo-stars.cmd README.md
```

## 它认哪些写法

| 写法 | 是否识别 |
| --- | --- |
| `https://github.com/账号/仓库` | ✅ |
| `https://github.com/账号/仓库.git`、`/tree/main/...`、`#readme` | ✅ 规范化到仓库本体 |
| `github.com/账号/仓库`（不带协议） | ✅ |
| `git@github.com:账号/仓库.git` | ✅ |
| 行内代码 / 链接文本 `[账号/仓库](...)` | ✅ |
| 代码块里 `pip install 账号/仓库` | ✅ |
| 徽章 `img.shields.io/github/stars/账号/仓库` | ✅ |
| `https://github.com/账号/仓库/issues/1` | ❌ 指向 issue，不是仓库 |
| `https://github.com/topics/xxx`、`/orgs/xxx`、`/sponsors/xxx` | ❌ GitHub 功能页 |
| `app/build.gradle.kts`、`data/report.md`、`gk/cet4` | ❌ 路径/标识符，默认过滤 |
| HTML 注释里的链接 | ❌ 视为没写 |

正文里裸写的 `owner/repo` 默认只收「明显像仓库」的（账号带连字符、名字与账号同名等），
避免把文档里的路径当成仓库。想放宽：

```bash
python md_repo_stars.py notes.md --code-names
```

## stars 从哪来

1. **本地缓存**（默认 `~/.cache/md-repo-stars/stars.json`，7 天有效）→ 命中直接返回；
2. **GitHub REST API** `GET /repos/{owner}/{repo}`，带 ETag 条件请求，304 时复用缓存；
3. **`--map` 本地星星表**（JSON 或 CSV，`owner/repo,stars`），可配合 `--offline` 完全断网排序。

未带 token 时 GitHub 限流为 **60 次/小时**；此时工具会立即停止请求、显示缓存值并提示，
已抓到的结果全部落盘，等配额恢复后重跑即可续上，不会重复消耗。

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `-f, --format` | `table`（默认）/ `markdown` / `json` / `csv` |
| `-o, --output` | 写入文件 |
| `--sort` | `stars`（默认降序）/ `name` / `updated` |
| `-n, --top` | 只显示前 N 个（汇总数字仍是全量） |
| `--token` | GitHub token，默认读 `GITHUB_TOKEN` / `GH_TOKEN` |
| `-j, --workers` | 并发数，默认 8 |
| `--cache` / `--no-cache` / `--max-age` / `--refresh` | 缓存控制 |
| `--offline` | 完全离线，只用缓存 / `--map` |
| `--map` | 本地星星表（JSON 或 CSV） |
| `-v, --verbose` | 输出引用出处（文件:行号）与错误原因 |
| `--quiet` | 不打印 stderr 统计信息 |

## 退出码

- `0` 正常（含「没找到仓库」的情况，stderr 会提示）
- `2` 输入文件不可读

## 备注

- 输出默认带三位有效数字缩写（`225.6k`），JSON / CSV 里是精确整数。
- 同一个仓库在一份文件里出现多次只算一条，`-v` 会列出所有出处行号。
- 私有仓库需要 token 且有访问权限，否则按 404 处理（显示 `—`）。

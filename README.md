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

## 配置 GitHub Token

带上 token 后配额从 **60 次/小时** 提到 **5000 次/小时**，扫几百个仓库也不怕。

### 1. 生成 token

打开 https://github.com/settings/tokens ：

- **Classic token**（`ghp_...`）：什么都不用勾，仅读公共仓库信息即可；
- **Fine-grained token**（`github_pat_...`）：Repository access 选 *Public repositories (read-only)*。

### 2. 四种配置方式，任选其一

**A. 一条命令存进配置文件（推荐，配一次以后都不用管）**

```bash
python md_repo_stars.py --save-token ghp_你的token
python md_repo_stars.py --check-token      # 验证并看剩余配额
```

保存位置：Windows `%APPDATA%\md-repo-stars\config.json`，macOS/Linux `~/.config/md-repo-stars/config.json`（Unix 下自动 `chmod 600`）。

**B. 环境变量**

```powershell
# Windows：当前会话临时生效
$env:GITHUB_TOKEN = "ghp_xxx"

# Windows：永久生效（重开终端后可用）
setx GITHUB_TOKEN "ghp_xxx"

# macOS / Linux
export GITHUB_TOKEN=ghp_xxx            # 临时
echo 'export GITHUB_TOKEN=ghp_xxx' >> ~/.zshrc   # 永久
```

**C. 项目里的 `.env`**（适合只给某个项目用）

```
GITHUB_TOKEN=ghp_xxx
```

工具会依次查 `./.env`、`./.env.local`、配置目录下的 `.env`。记得把 `.env` 加进 `.gitignore`。

**D. 复用 `gh` CLI 的登录态**

装了 [GitHub CLI](https://cli.github.com/) 并 `gh auth login` 过的话，工具会自动调 `gh auth token` 取用，无需额外配置。

### 3. 优先级

`--token` > 环境变量（`GITHUB_TOKEN` → `GH_TOKEN` → `MD_REPO_STARS_TOKEN`）> `.env` > 配置文件 > `gh auth token`。

运行时会打印实际来源（token 已脱敏）：

```
🔑 使用 token ghp_…a1b2（来源：环境变量 GITHUB_TOKEN）
```

### 4. 体检

```bash
python md_repo_stars.py --check-token
```

```
🔑 token 来源：C:\Users\you\AppData\Roaming\md-repo-stars\config.json
✅ token 有效 · 账号 your-name（ghp_…a1b2）
配额：4987/5000 次 · 重置时间 2026-10-27 21:31:05
```

token 失效会返回 `HTTP 401` 并以退出码 1 结束。

### 安全提醒

token 等同于账号密码的一部分：别提交进仓库、别贴进截图，泄漏了立刻去 GitHub 页面 Revoke。工具只把它用在 `Authorization` 请求头里，不会写进 stars 缓存。

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `-f, --format` | `table`（默认）/ `markdown` / `json` / `csv` |
| `-o, --output` | 写入文件 |
| `--sort` | `stars`（默认降序）/ `name` / `updated` |
| `-n, --top` | 只显示前 N 个（汇总数字仍是全量） |
| `--token` | 直接指定 token（优先级最高） |
| `--save-token` | 把 token 写进配置文件 |
| `--check-token` | 验证 token + 显示剩余 API 配额 |
| `--no-gh-token` | 不自动读取 `gh` CLI 登录态 |
| `-j, --workers` | 并发数，默认 8 |
| `--cache` / `--no-cache` / `--max-age` / `--refresh` | 缓存控制 |
| `--offline` | 完全离线，只用缓存 / `--map` |
| `--map` | 本地星星表（JSON 或 CSV） |
| `-v, --verbose` | 输出引用出处（文件:行号）与错误原因 |
| `--quiet` | 不打印 stderr 统计信息 |

## 退出码

- `0` 正常（含「没找到仓库」的情况，stderr 会提示）
- `1` `--check-token` 体检时 token 无效 / 网络不通
- `2` 输入文件不可读

## 备注

- 输出默认带三位有效数字缩写（`225.6k`），JSON / CSV 里是精确整数。
- 同一个仓库在一份文件里出现多次只算一条，`-v` 会列出所有出处行号。
- 私有仓库需要 token 且有访问权限，否则按 404 处理（显示 `—`）。

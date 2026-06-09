# secweb 使用手册

> 详细操作指南. 快速上手见 README.md.

---

## 1. 系统要求

| 项 | 最低版本 | 说明 |
|---|---|---|
| 操作系统 | macOS / Linux | Windows 未测试 (理论可用 WSL) |
| Python | 3.10+ | `run.sh` 用 `python3` |
| 磁盘 | 200MB | venv + 依赖, 不含运行时数据 |
| 网络 | 可达 Anthropic API | 或本地 / 中转网关 |

需要以下任一**认证方式**:

- (推荐) `claude` CLI 已登录 — 进入 dashboard 后自动用 `~/.claude/` 认证态
- 或在 `.env` 填 `ANTHROPIC_AUTH_TOKEN=sk-ant-...` (Anthropic 官方 API key)
- 或自建网关 / 中转: 填 `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`

---

## 2. 第一次启动

```bash
cd <项目目录>
./run.sh
```

`run.sh` 会自动:
1. 创建 `.venv/` 虚拟环境
2. 装 `requirements.txt` 依赖 (首次 / 依赖变化时)
3. 复制 `.env.example` → `.env` (首次)
4. 启动 dashboard 在 `http://127.0.0.1:8765`

**首次启动后必改 `.env`**:

```bash
SECWEB_PASSWORD=changeme           # ← 必改, 这是 Web 登录密码
ANTHROPIC_AUTH_TOKEN=               # ← 选填, 留空走 ~/.claude/ 已登录态
```

改完 Ctrl+C 重启即可生效.

打开 `http://127.0.0.1:8765`, 输入 `SECWEB_PASSWORD` 登录.

---

## 3. UI 概览

```
┌──────────────────────────────────────────────────────┐
│ 顶部: 模型下拉 + 并发数 + 全局配置                       │
├──────────┬───────────────────────────────────────────┤
│ 左侧:     │ 右侧 4 个 Tab:                              │
│ - 项目列表 │  1. 实时日志 — claude 思考 + 工具调用流        │
│ - 任务列表 │  2. 报告 — 完成后扫描出的 report.md          │
│           │  3. 产物 — 任务 cwd 下所有 .md / 截图         │
│           │  4. 元数据 — session_id / 时长 / cost / 路径  │
└──────────┴───────────────────────────────────────────┘
```

---

## 4. 添加项目 (可选)

项目 = 多个相关任务的分组. 不创建项目时所有任务跑在 `runs/_/<task_id>/`.

**点 "+ 新建项目"** 填:

- **名称** — 公司名 / 业务线
- **描述** — 备注 (可选)
- **默认 prompt** — 留空用全局默认 `/hack {url} auto`
- **并发数** — 默认 3, 上限 32

项目创建后任务跑在 `runs/<project_id>/<task_id>/`. 同项目历史目标的 hostname
会自动注入到当前任务的授权书清单 (动态资产清单功能).

---

## 5. 提交目标

### 5.1 Web 目标 (普通 HTTPS URL)

```
https://example.com
https://api.example.com/v1
```

每行一个目标. 提交后:
- 自动注入 web 模式 rule (含浏览器门控 / 降级禁令)
- 默认走 `/hack {url} auto` 流水线
- 也可在"提示词预设"下拉选其他 skill (含你自定义的 skill)

### 5.2 小程序目标 (本地反编译目录)

```
/Users/me/wxapkg/com.example.miniapp
```

dashboard 自动检测特征文件 (`app.json` / `*.wxml` / `*.wxapkg`), 切换到
**小程序模式 rule** (无浏览器规则, 静态分析为主), 默认 prompt 自动切到
`/miniprogram-audit` 或 `/minisecprogram-audit` (你在下拉里选).

### 5.3 提示词预设

下拉自动列出 `~/.claude/skills/<name>/SKILL.md` 中的所有 skill, 加上 `/hack {url} auto (推荐)`.

| 预设 | 适用 |
|---|---|
| `/hack {url} auto (推荐)` | 完整流水线全自动 (recon → js-audit → ... → report) |
| `/hack {url}` | 同上但每 phase 之间会向用户确认 |
| `/recon {url}` | 仅 Phase 0 侦察 |
| `/js-audit {url}` | 仅 Phase 1 JS 审计 |
| `/idor {url}` | 仅 IDOR 测试 |
| `/sqli {url}` | 仅 SQL 注入测试 |
| `/miniprogram-audit <local-path>` | 小程序专项 |
| `/minisecprogram-audit <local-path>` | 小程序静态审计 (Agent 协作版) |
| 自定义 … | 选这个后输入框可任意编辑 |

---

## 6. 任务状态

| 状态 | 含义 | 操作 |
|---|---|---|
| `queued` (排队) | 等待并发槽 | 等待 |
| `running` (运行中) | claude 正在跑 | 看实时日志 |
| `needs_input` (待补充) ← **会闪黄** | claude 输出 `[NEEDS_USER_INPUT] <问题>` | 点进去贴答案 |
| `done` (完成) | 流水线跑完 + 报告写入 | 看报告 |
| `failed` (失败) | claude 报错或超时 | 看日志诊断 |
| `stopped` (已停止) | 你手动停的 | - |

带紫色 **"发现"** 标的任务表示报告里有真实漏洞 PoC.

---

## 7. needs_input 流程

claude 跑到一半若需要补充凭证 / 范围, 会输出:

```
[NEEDS_USER_INPUT] 请提供登录账号
```

任务状态变 `needs_input` (黄色). 点进去:
1. 看"实时日志"末尾, 确认 claude 要什么
2. 点"补充输入"按钮
3. 贴入答案 (如 cookie / 账号密码 / 任意纯文本)
4. claude 自动续跑 (同 session_id, 上下文不丢)

---

## 8. 自定义 skill

### 8.1 启用项目自带的小程序 skill (推荐先做)

本项目根目录自带 2 个小程序专项 skill (示例 + 开箱可用):

```
secweb-opensource/
├── miniprogram-audit/         ← 微信小程序专项审计 (源码获取 / isNeedLogin / SQLi 矩阵)
└── minisecprogram-audit/      ← 微信小程序静态安全审计 (Orchestrator + 7 Agent 并行)
```

启用方式: 复制到 `~/.claude/skills/`:

```bash
# 单条命令复制两个
cp -r miniprogram-audit minisecprogram-audit ~/.claude/skills/
```

强制刷新 dashboard 页面 (`Cmd+Shift+R` / `Ctrl+Shift+F5`), 下拉就会出现:
- `/miniprogram-audit {url}` (其中 `{url}` 填本地反编译目录路径)
- `/minisecprogram-audit {url}`

### 8.2 创建你自己的 skill

```bash
mkdir -p ~/.claude/skills/my-skill
```

创建 `~/.claude/skills/my-skill/SKILL.md`:

```markdown
---
name: my-skill
description: 一句话描述这个 skill (会显示在 dashboard 下拉的 label)
---

# My Skill

具体的方法论 / 流水线 / 测试要求 ...
```

强制刷新 dashboard 页面 (`Cmd+Shift+R` / `Ctrl+Shift+F5`), 下拉里就会出现
`/my-skill {url}`.

> 注意: skill 标识用**目录名** (与 Claude Code 实际加载行为一致), frontmatter
> 的 `name:` 仅作元数据展示. 二者不一致时以目录名为准.

详细 skill 编写指南见 `SKILL_AUTHOR_GUIDE.md`.

---

## 9. 故障排查

### 任务一直 queued

- 看顶部并发数, 默认 3. 当前 running 任务 ≥ 并发数时新任务排队
- 项目级并发独立, 同项目并发满才会排队

### AI 不调 Skill 工具 (历史故障, 已修)

dashboard v1.3+ 已通过 `setting_sources=["user"]` 修复. 如果重现:
检查 `app/runner_sdk.py:_ALLOWED_TOOLS` 含 `"Skill"`.

### 实时日志卡顿 (历史故障, 已修)

dashboard v1.4+ 已通过 WS 先发 / DB 后写 + 并发广播修复.

### 浏览器看不到新加的 skill

强制刷新 (`Cmd+Shift+R`), 或登出登录. 后端有 30 秒缓存, 刷新无效时等 30s.

### 任务 finished 但没看到报告

- 看"产物" tab, 任意 .md 都有
- 默认认 `report.md` / 文件名含 `report` / `finding` / `vuln` 的 markdown

### Anthropic API 报错

- 401: API key 错或过期
- 429: 限流, 降并发 / 等
- 5xx: 网关问题, 看 ANTHROPIC_BASE_URL

---

## 10. 安全注意事项

| 项 | 说明 |
|---|---|
| **不要暴露到公网** | 服务用 `permission_mode=bypassPermissions`, 让 skill 里 Bash 不被打断. **只在你信任的本机/局域网跑** |
| **不要测未授权目标** | 见 README "安全声明", 法律责任自负 |
| **API key 保护** | `.env` 已在 `.gitignore`, 不要 commit |
| **任务工作目录隔离** | 每个任务在 `runs/<pid>/<tid>/` 独立目录, 互不影响 |
| **session 续跑** | 同 task_id 的多次都用同一 `session_id`, 上下文连续 |

---

## 11. 配置项参考 (.env)

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECWEB_PASSWORD` | `changeme` | Web 登录密码 (必改) |
| `SECWEB_HOST` | `127.0.0.1` | 监听 IP, 别改 `0.0.0.0` |
| `SECWEB_PORT` | `8765` | 监听端口 |
| `SECWEB_DEFAULT_CONCURRENCY` | `3` | 默认并发 |
| `SECWEB_DEFAULT_PROMPT` | `/hack {url} auto` | 默认 prompt 模板, 必含 `{url}` |
| `SECWEB_CLAUDE_BIN` | `/usr/local/bin/claude` | claude CLI 路径 (preflight 自检用) |
| `SECWEB_TASK_TIMEOUT` | `0` | 单任务最长秒数, 0=无限 |
| `ANTHROPIC_BASE_URL` | (空) | 自建网关 URL, 留空用官方 |
| `ANTHROPIC_AUTH_TOKEN` | (空) | API key, 留空走 `~/.claude/` |
| `ANTHROPIC_MODEL` | (空) | 模型名, 如 `claude-opus-4-7[1M]`, 留空用 SDK 默认 |

---

## 12. 进阶: 后端架构概览

```
main.py
   ↓
app/web.py (FastAPI 路由 + WebSocket)
   ├── app/routers/* (auth / tasks / projects / settings / reports)
   ├── app/scheduler.py (asyncio.Semaphore 并发调度)
   │      ↓
   │   动态资产清单 (按 hostname) + 小程序模式检测
   │      ↓
   ├── app/runner_sdk.py (claude-agent-sdk 进程内调用)
   │      ↓
   │   6 层 hook 守卫 (TodoWrite / Skill / Stop / 横向扩展提醒等)
   │      ↓
   │   _emit (WS 先发, DB 后写, 并发广播)
   │      ↓
   ├── app/store.py (SQLite 持久化)
   └── app/ws.py (WebSocket Broadcaster)
```

详细模块说明见 README.md "关键文件" 段.

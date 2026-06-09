<h1 align="center">🛡️ secweb</h1>

<p align="center">
  <strong>面向 Claude Code 生态的自动化渗透测试调度平台</strong><br/>
  <em>把多目标 / 多 skill / 多并发渗透从「开 N 个终端手动跑」升级为「一个网页可视化运营」</em>
</p>

<p align="center">
  <a href="#-核心特性">特性</a> ·
  <a href="#-快速上手">快速上手</a> ·
  <a href="USAGE.md">使用手册</a> ·
  <a href="SKILL_AUTHOR_GUIDE.md">写自己的 skill</a> ·
  <a href="#-安全声明--security-notice">安全声明</a>
</p>

<p align="center">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue.svg"/>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg"/>
  <img alt="Status" src="https://img.shields.io/badge/status-active-success.svg"/>
</p>

---

## ✨ 核心特性

| 维度 | secweb 的做法 |
|---|---|
| 🤖 **AI 驱动** | 基于 Claude Agent SDK + 用户自定义 skill, 每个目标走完整渗透流水线 (recon → js-audit → auth-bypass → idor → sqli → ssrf → ssti → business-logic → validate → report) |
| 🎯 **多目标并发** | asyncio.Semaphore 项目级并发控制, 一次提交批量目标, 自动排队 / 调度 / 续跑 |
| 🪜 **动态 skill 加载** | 自动扫描 `~/.claude/skills/` 填充提示词预设, 加新 skill 刷新即生效, 0 改前端代码 |
| 📱 **小程序模式自适应** | 检测本地反编译目录自动切换专用 rule (无浏览器规则, 静态分析为主) |
| 🔒 **多层防护机制** | 6 层 hook 守卫 + 启动断言 + 横向扩展兜底, 防 AI 走捷径 / 编借口 / 漏测 |
| 📡 **实时观测** | WebSocket 推送 claude 思考流 + 工具调用流, 报告 / 产物 / 元数据 4 视图分离 |
| 🔄 **断点续跑** | 同 task_id 多次补充输入用同一 session_id, 上下文不丢, 浏览器 / 凭证 / cookie 全保留 |
| 📜 **可逆写操作** | 内置写操作测试底线 (create 用 secweb_test_ 标识 / delete 仅删自己创建的对象), 防污染生产数据 |

---

## 🚀 快速上手

```bash
git clone https://github.com/txluck/secweb.git
cd secweb
./run.sh
```

`run.sh` 会自动:
- 创建 `.venv/` 虚拟环境
- 安装 `requirements.txt`
- 复制 `.env.example` → `.env` (首次)
- 启动 dashboard 在 `http://127.0.0.1:8765`

**首次启动后必改**:

1. 编辑 `.env`:
   - `SECWEB_PASSWORD` — Web 登录密码 (默认 `changeme`, 必改)
   - `ANTHROPIC_AUTH_TOKEN` — 你的 Anthropic API key
   - 或保持空白, 让 SDK 走 `~/.claude/` 已登录态

2. 启用项目自带的 skill (推荐):
   - 项目根目录自带 2 个小程序审计 skill: `miniprogram-audit/` + `minisecprogram-audit/`
   - 复制到 `~/.claude/skills/`:
     ```bash
     cp -r miniprogram-audit minisecprogram-audit ~/.claude/skills/
     ```
   - 你的其他自定义 skill 也放 `~/.claude/skills/<name>/SKILL.md`
   - dashboard 启动会自动扫描并填充提示词预设下拉
   - 没装任何 skill 时退回写死的常见预设 (hack / recon / sqli 等)

3. 浏览器打开 `http://127.0.0.1:8765`, 输入密码登录.

📖 详细使用指南见 [USAGE.md](USAGE.md).

---

## ⚠️ 安全声明 / Security Notice

**本工具仅用于授权场景的安全测试**:

- ✅ 你拥有所有权或运营权的资产
- ✅ 公司/组织的内部安全评估
- ✅ 已与目标方签订书面渗透测试合同
- ✅ Bug Bounty 平台 (HackerOne / Bugcrowd 等) 在 scope 范围内的目标
- ✅ CTF 比赛 / 自有测试环境 / Vulnhub 等练习平台

**未授权对他人系统进行渗透测试可能违法**:

- 中国《刑法》第 285-286 条 (非法侵入 / 非法控制计算机信息系统罪)
- 美国 Computer Fraud and Abuse Act (CFAA, 18 U.S.C. § 1030)
- 欧盟 NIS Directive 及各成员国本地法规

本工具作者**不为非授权使用承担任何责任**. 使用本工具即表示你已理解并同意:
所有测试活动的合法性由使用者自行确认与承担.

---

## 它是怎么工作的

```
浏览器  ─┐                                ┌─▶ claude SDK + /hack url1
         │  FastAPI + SQLite + WebSocket  ├─▶ claude SDK + /hack url2     (并发受 Semaphore 控制)
         └─▶  调度器(asyncio.Semaphore) ──┴─▶ claude SDK + /hack url3
```

- 每个目标 = 一个独立任务，跑在独立工作目录 `runs/<project_id>/<task_id>/`
- 用 `claude-agent-sdk` 在进程内调用，session 持续, 浏览器 (playwright MCP) 不掉线
- Claude 如果需要补充凭证/cookie/范围，会按系统提示输出 `[NEEDS_USER_INPUT] <问题>`，任务进入 `needs_input` 状态
- 你在 UI 上回答后, dashboard 用 `client.query(answer)` 在同 session 续跑, 上下文不丢
- 完成后扫描工作目录，把最像最终报告的 `.md` 标记为漏洞报告

## 用法

1. 顶部设置并发数（默认 3）
2. 左侧粘贴批量 URL，每行一个 (也支持本地路径作目标, 如反编译后的小程序目录)
3. prompt 模板里 `{url}` 会被替换. 下拉自动列出 `~/.claude/skills/` 中的所有 skill, 也可手动自定义
4. 点"开始测试" → 任务自动进队列、按并发限制启动
5. 左侧任务列表实时刷新；点任意任务看右侧"实时日志 / 报告 / 产物 / 元数据"
6. 状态：排队 / 运行中 / 待补充 / 完成 / 失败 / 已停止；有发现的会有紫色"发现"标记
7. **待补充**的任务会闪黄；点进去 → "补充输入" → 把凭证/答案贴进去 → Claude 续跑

## 关键文件

| 文件 | 作用 |
|------|------|
| `app/web.py` | FastAPI 路由 + cookie 鉴权 + WebSocket |
| `app/scheduler.py` | asyncio.Semaphore 并发调度 + 动态资产清单 / 小程序模式检测 |
| `app/runner_sdk.py` | claude-agent-sdk 进程内调用 + 6 层 hook 守卫 |
| `app/guard_state.py` | hook 实现 (TodoWrite / Skill / Stop / 横向扩展提醒等) |
| `app/system_prompt.py` | 系统提示 + CLAUDE.md 模板 (web 模式 + 小程序模式) |
| `app/skill_contract.py` | slash command → SKILL.md inline 注入 |
| `app/store.py` | SQLite 持久化 |
| `static/index.html` + `app.js` + `style.css` | 前端单页 (Vue 3 CDN) |
| `data/secweb.db` | SQLite 数据 (运行时创建, .gitignore 已排除) |
| `runs/<project_id>/<task_id>/` | 每个任务的工作目录、Claude 写入的产物 |
| `授权书.md` | 渗透测试授权书模板 (含动态资产清单占位符 `{IN_SCOPE_ASSETS}`) |

## 配置 (.env)

| 变量 | 说明 |
|------|------|
| `SECWEB_PASSWORD` | Web 登录密码 |
| `SECWEB_HOST` / `SECWEB_PORT` | 监听地址，默认 `127.0.0.1:8765` |
| `SECWEB_DEFAULT_CONCURRENCY` | 默认并发数 |
| `SECWEB_DEFAULT_PROMPT` | 默认 prompt 模板，必须含 `{url}` |
| `SECWEB_CLAUDE_BIN` | claude CLI 路径 (仅 preflight 自检用) |
| `SECWEB_TASK_TIMEOUT` | 单任务最长运行秒数，`0` = 不限制 |
| `ANTHROPIC_BASE_URL` | (可选) Anthropic API endpoint 自建网关或中转, 留空用官方 |
| `ANTHROPIC_AUTH_TOKEN` | (可选) API key, 留空走 `~/.claude/` 已登录态 |
| `ANTHROPIC_MODEL` | (可选) 模型名, 如 `claude-opus-4-7[1M]` / `claude-sonnet-4-6` |

## 自定义 skill

dashboard 自动从 `~/.claude/skills/` 加载所有用户级 skill. 添加新 skill:

```bash
mkdir -p ~/.claude/skills/my-skill
cat > ~/.claude/skills/my-skill/SKILL.md <<'EOF'
---
name: my-skill
description: 一句话描述这个 skill 干什么 (会显示在 dashboard 下拉里)
---

# My Skill

具体的方法论 / 流水线 / 测试要求 ...
EOF
```

强制刷新 dashboard 页面 (`Cmd+Shift+R`), 下拉里就会出现 `/my-skill {url}`.

> 提示: skill 标识用**目录名** (与 Claude Code 实际加载行为一致), frontmatter
> 的 `name:` 仅作元数据展示. 二者不一致时以目录名为准.

## 注意事项

- **本地工具**: 服务用 `permission_mode="bypassPermissions"` 启动 SDK, 让 skill 里的 Bash 操作不被打断; **只在你信任的本机上跑**, 不要暴露到公网
- **认证**: SDK 用 `~/.claude/` 已登录账号或 `.env` 里的 token, 多个任务并发会同时消耗额度
- **session 续跑**: 同一 task_id 的多次 (首次 + 多次补充) 都用同一个 `session_id`, 上下文连续
- **报告识别**: 默认认为 `report.md` / 含 "report"/"finding"/"vuln" 的 markdown 是报告. 必要时调整 `runner_sdk.py:_scan_report`

## 后续可改进

- 多用户 / 角色权限
- 报告产物归档到对象存储
- 任务标签 / 跨项目检索
- Claude 用量统计 (解析 result 消息里的 token 数)
- 任务结果合并 (同目标多次跑取并集)

---

## 💬 联系作者 / Stay in Touch

如果这个项目对你有帮助, 欢迎 Star ⭐ + 关注公众号获取更新与安全研究分享:

<table>
  <tr>
    <td align="center">
      <strong>🌐 公众号</strong><br/>
      <em>关注获取最新版本 / 安全研究 / 渗透方法论</em><br/>
      <img src="docs/images/wechat-public.jpg" width="220" alt="公众号二维码"/>
    </td>
    <td align="center">
      <strong>💬 个人微信</strong><br/>
      <em>交流合作 / 漏洞讨论 / 项目共建</em><br/>
      <img src="docs/images/wechat-personal.png" width="220" alt="个人微信二维码"/>
    </td>
  </tr>
</table>

也欢迎在 [GitHub Issues](https://github.com/txluck/secweb/issues) 提 bug / 需求 / PR.

---

## 📜 License

MIT License — 详见 [LICENSE](LICENSE) 文件.

Copyright © 2026 [@txluck](https://github.com/txluck)


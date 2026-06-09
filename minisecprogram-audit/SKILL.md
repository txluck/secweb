---
name: wxmini-audit
description: 微信小程序静态安全审计 Skill。输入:已反编译的小程序源码目录。输出:覆盖敏感信息、API 接口、加解密、漏洞、定向深挖五大维度的完整审计报告。架构:Orchestrator + 7 Agent 协作 + Phase 2 四路并行。所有 grep / 正则规则内置在各 Agent 中,不依赖任何外部脚本或反编译工具。
version: 2.0.0
tags: [security, wechat, miniprogram, static-analysis, audit]
platform: cross-platform
---

# Skill: wxmini-audit — 微信小程序静态安全审计

## 一行总览
**输入反编译后的源码目录,输出一份能直接交付的安全审计报告 + 一份可灌入 Burp/FFUF 的接口 Fuzz 列表。**

## 核心原则(所有 Agent 必须遵守)

1. **纯静态分析**
   - 严禁发起任何 HTTP/HTTPS 请求(curl / wget / fetch / Invoke-WebRequest 等一律禁止)
   - 不验证密钥 / Token / 接口 / 域名 是否生效
   - 不连接任何远程服务(DB / Redis / MQ / API)
   - 不下载、不更新任何远程资源
   - 全部分析必须基于 `{target_dir}` 下的本地文件

2. **不生成攻击载荷**
   - 不生成 PoC 漏洞利用脚本
   - 不生成自动化攻击工具
   - 报告仅供安全审计与防御参考

3. **最小权限**
   - 仅读 `{target_dir}` 下源码
   - 仅向 `{output_dir}` 写入分析产物
   - 不修改、不删除任何源文件

4. **流程不可压缩**
   - 必须按 Phase 0 → Phase 1 → Phase 2(全部 4 个 Agent 并行) → Phase 2.5(条件) → Phase 3 顺序执行
   - 不允许跳阶段、合并阶段、改变顺序

## 编排器铁律(硬性约束,违反等同审计失败)

> ⛔ 以下规则不可违反。

1. **严禁代劳**:Orchestrator 自身不得直接产出任何 `*_analysis.json` / `*_report.json` / `security_report.md`。所有分析输出**只能**由对应子 Agent 生成。
2. **严禁跳阶段**:每个 Phase 必须按序执行,不得以"已知结果""时间不够""Agent 太慢"为由跳过。Phase 2.5 一旦满足触发条件(`custom_requests.has_custom_requests == true`),**必须**启动 DeepDive,不得自行替代。
3. **严禁截留外部信息**:Orchestrator 在与用户交互中获取的所有外部数据(Burp 抓包、用户补充情报、其他 MCP 工具返回值)**必须完整传给** DeepDive。
4. **等待期间禁分析**:Phase 2 四 Agent 启动后,Orchestrator 唯一允许做的是等待,不得分析代码 / 不得处理用户特定接口请求 / 不得提前生成报告。
5. **报告完整性**:Reporter 输出中,所有敏感信息发现 + 所有 API 接口必须**逐条记录**,不得丢失。主报告列关键发现,完整数据走 `secrets_full.md` 与 `api_endpoints_full.md`。

## 触发方式

用户用以下任一方式触发本 Skill:

```
帮我分析这个小程序 /path/to/decompiled/source
审计这个小程序 ~/work/wxmini-decompiled
分析一下这个微信小程序 {目录路径}
```

支持携带定向需求(自动触发 Phase 2.5):
```
帮我分析这个小程序 {目录},重点看 /api/user/login 接口
审计这个小程序 {目录},Burp 抓包发现 /api/order amount 可篡改
分析这个小程序 {目录},关注支付安全和越权风险
```

> **本 Skill 不做反编译**。`{target_dir}` 必须已经是反编译产物,目录下应包含 `.js` / `.json` / `.wxml` 等文件。如果用户给的是 `.wxapkg`,Inventory Agent 会终止并提示先用反编译工具(unveilr / wxappUnpacker / 类似)处理。

## 变量定义

| 变量 | 含义 | 解析方式 |
|------|------|----------|
| `{target_dir}` | 已反编译的小程序源码根目录 | 从用户输入提取(支持绝对/相对/`~/`) |
| `{output_dir}` | 审计输出目录 | 在用户当前工作目录 CWD 下创建 `wxaudit-output`,已存在则追加 `-{YYYYMMDD-HHMMSS}` |
| `{skill_dir}` | 本 Skill 安装目录 | SKILL.md 所在目录绝对路径 |

> 输出目录**禁止**写到 `{target_dir}` 下,避免污染源码。

## Agent 索引

7 个 Agent 全部位于 `{skill_dir}/agents/`:

| # | Agent | 文件 | 阶段 | 职责 |
|---|-------|------|------|------|
| 01 | Inventory | `agent-01-inventory.md` | Phase 1 | 文件资产清单 |
| 02 | SecretHunter | `agent-02-secret-hunter.md` | Phase 2 | 敏感信息全量挖掘 |
| 03 | EndpointMapper | `agent-03-endpoint-mapper.md` | Phase 2 | 接口提取与拓扑映射 |
| 04 | CryptoInspector | `agent-04-crypto-inspector.md` | Phase 2 | 加解密 / 签名审查 |
| 05 | VulnHunter | `agent-05-vuln-hunter.md` | Phase 2 | 八大维度漏洞挖掘 |
| 06 | Reporter | `agent-06-reporter.md` | Phase 3 | 报告与完整性兜底 |
| 07 | DeepDive | `agent-07-deep-dive.md` | Phase 2.5 | 用户定向深挖(条件触发) |

## 执行流程

```
用户输入: "帮我分析这个小程序 {target_dir} [+ 可选定向需求]"
        │
        ▼
┌──────────────────────────────────────────────────┐
│  Phase 0: 需求解析(Orchestrator 自身完成)        │
│  · 提取 {target_dir}                              │
│  · 创建 {output_dir}(CWD 下新建)                 │
│  · 解析 custom_requests                           │
│  · 不启动子 Agent                                 │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  Phase 1: 源码资产清单                            │
│  agent-01-inventory.md → file_inventory.json     │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Phase 2: 四路并行(必须全部启动)                                          │
│                                                                          │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐     │
│  │SecretHunter  │ │EndpointMapper│ │CryptoInspect.│ │VulnHunter    │     │
│  │ agent-02     │ │ agent-03     │ │ agent-04     │ │ agent-05     │     │
│  │              │ │              │ │              │ │              │     │
│  │secrets_      │ │api_endpoints │ │crypto_       │ │vuln_         │     │
│  │report.json   │ │.json +       │ │analysis.json │ │analysis.json │     │
│  │              │ │endpoints_    │ │              │ │              │     │
│  │              │ │fuzz.txt      │ │              │ │              │     │
│  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘     │
│         │                │                │                │             │
│  Orchestrator 等待 4 路全部完成                                          │
└─────────┼────────────────┼────────────────┼────────────────┼─────────────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Phase 2.5: 定向深挖(条件触发)                                          │
│  仅当 custom_requests.has_custom_requests == true                        │
│  agent-07-deep-dive.md → custom_analysis.json                            │
└──────────────────┬───────────────────────────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Phase 3: 报告生成                                                        │
│  agent-06-reporter.md                                                    │
│  → security_report.md / api_endpoints_full.md / secrets_full.md          │
│  → findings.json / domains.txt / endpoints_fuzz.txt(校验)               │
└──────────────────────────────────────────────────────────────────────────┘
```

## 详细编排指令

### Phase 0:需求解析(Orchestrator 自身完成)

1. **提取 `{target_dir}`**
   - 从用户输入识别路径(`/`、`~`、`./` 起始的字符串均视为路径)
   - 若不存在或不可读 → 终止,提示用户检查
   - 若是 `.wxapkg` 文件 → 终止,提示"本 Skill 不做反编译,请先解包"

2. **创建 `{output_dir}`**
   - `{output_dir} = {CWD}/wxaudit-output`
   - 若已存在 → 改用 `{CWD}/wxaudit-output-{YYYYMMDD-HHMMSS}`
   - 用 `mkdir -p` 创建

3. **解析 `custom_requests`**

   检测用户输入是否包含定向信号:
   - 接口路径(`/api/xxx` / 完整 URL)
   - 参数名(如 `token / amount / password`)
   - 函数名(如 `encryptPayload / login`)
   - 关注词:`重点看 / 关注 / 分析一下 / 测试 / 着重 / 检查`
   - 抓包关键词:`Burp / 抓包 / 请求 / 响应 / postman / 拦截`
   - 安全领域:`SQL 注入 / 越权 / IDOR / 支付 / 鉴权`

   生成 `custom_requests`(内存对象,不写文件):

   ```json
   {
     "has_custom_requests": true,
     "targets": [
       { "type": "endpoint",  "value": "/api/pay/create", "context": "用户原文" },
       { "type": "parameter", "value": "amount",          "context": "..." },
       { "type": "focus_area","value": "支付安全",        "context": "..." },
       { "type": "burp_info", "value": "POST /api/order amount 可篡改", "context": "Burp 抓包" }
     ],
     "external_info": "用户提供的额外抓包/请求/响应详情"
   }
   ```

   无任何信号 → `has_custom_requests: false`,`targets: []`。

4. **进入 Phase 1**(无需验证)

### Phase 1:源码资产清单

1. 读 `{skill_dir}/agents/agent-01-inventory.md` 获取完整指令
2. 启动 1 个 Agent(`general-purpose`),将 `{target_dir}`、`{output_dir}`、`{skill_dir}` 替换进 prompt
3. 等待返回

**Phase 1 验证(全部通过才进入 Phase 2)**:
- `{output_dir}/file_inventory.json` 存在且非空
- `total_files > 0` 且 `js_files` 数组非空
- 不通过 → 终止,向用户报告原因

### Phase 2:四路并行

**同时**启动 4 个 background Agent:

| Agent | 提示词 | 输入 | 输出 |
|-------|--------|------|------|
| SecretHunter | `agent-02-secret-hunter.md` | `{target_dir}` + `file_inventory.json` | `secrets_report.json` |
| EndpointMapper | `agent-03-endpoint-mapper.md` | `{target_dir}` + `file_inventory.json` | `api_endpoints.json` + `endpoints_fuzz.txt` |
| CryptoInspector | `agent-04-crypto-inspector.md` | `{target_dir}` + `file_inventory.json` | `crypto_analysis.json` |
| VulnHunter | `agent-05-vuln-hunter.md` | `{target_dir}` + `file_inventory.json` | `vuln_analysis.json` |

启动方式:`task` 工具,`mode="background"`,类型 `general-purpose`,**先 Read 提示词文件全文**,把内容拼进 `prompt` 参数,再把 `{target_dir} / {output_dir} / {skill_dir}` 替换为实际路径。

**⛔ CryptoInspector / VulnHunter / SecretHunter / EndpointMapper 的隔离规则**:
- 这 4 个 Agent 的 prompt **严禁包含** `custom_requests` / 用户提到的特定接口/参数 / Burp 信息
- 它们只做通用全量扫描
- 用户的定向需求由 DeepDive 在 Phase 2.5 处理

**等待规则(只做这一件事)**:
- 启动后 Orchestrator 唯一职责是**等 4 个 Agent 全部返回**
- 禁止读源码、禁止生成任何中间产物、禁止启动其他 Agent

**Phase 2 验证**:
- 4 个 JSON 至少 3 个存在且为有效非空 JSON
- 少于 3 个 → 终止
- 不足但 ≥ 3 个 → 继续,记录缺失维度供 Reporter 标注

### Phase 2.5:定向深挖(条件触发)

> 仅当 `custom_requests.has_custom_requests == true` 才执行,否则直接进 Phase 3。

1. Read `{skill_dir}/agents/agent-07-deep-dive.md`
2. 启动 DeepDive(`general-purpose`,可 background),prompt 包含:
   - 提示词全文
   - `{target_dir}` / `{output_dir}` / `{skill_dir}`
   - **完整的** `custom_requests` 对象
   - **完整的** 用户外部情报(Burp 抓包细节等)— 严禁截留
   - Phase 2 已产出的 JSON 文件路径
3. 等待返回

**Phase 2.5 验证**:
- `custom_analysis.json` 存在 → 继续 Phase 3
- 不存在 → 不阻断流程,提示用户"定向分析未完成",继续 Phase 3 并由 Reporter 在主报告标注

### Phase 3:报告生成

1. Read `{skill_dir}/agents/agent-06-reporter.md`
2. 启动 Reporter(`general-purpose`),prompt 中包含:
   - 提示词全文
   - `{output_dir}`
   - **`has_custom_requests` 标志**(用于 QC 判断)
3. 等待返回

**Phase 3 验证**:
- `security_report.md` 存在且 ≥ 1KB
- `api_endpoints_full.md` 存在
- `secrets_full.md` 存在
- `findings.json` 存在
- `domains.txt` 存在
- `endpoints_fuzz.txt` 存在
- 不通过 → 重试一次 Reporter,仍失败则向用户报告并保留已有 JSON

完成后 Orchestrator 输出最终的简短摘要(从 Reporter 终端输出读取并转述)。

## 流程强制执行规则(再强调一遍)

⛔ 不可违反:

1. 必须按 Phase 0 → 1 → 2 → 2.5(条件) → 3 顺序,不可跳步
2. 每个 Phase 完成后必须验证产出
3. Phase 2 必须启动**全部 4 个** Agent
4. 严禁 Phase 1 后直接进 Phase 3
5. 严禁多 Phase 合并执行
6. 用户定向需求**只在 Phase 2.5 处理**,Phase 2 各 Agent 只做通用扫描

⛔ 常见错误(绝对禁止):
- ❌ 反编译后看一眼某接口就生成报告
- ❌ 跳过 Phase 1 直接进 Phase 2
- ❌ 仅启动 Phase 2 的 1~2 个 Agent 就进入下一阶段
- ❌ 用户说"看一下 /api/xxx",就让 VulnHunter 专门查 /api/xxx(应该交给 DeepDive)
- ❌ Orchestrator 自己写 vuln_analysis.json

## 输出文件清单

完成后 `{output_dir}` 下应有:

| 文件 | 来源 | 说明 |
|------|------|------|
| `file_inventory.json` | Inventory | 文件资产清单 |
| `secrets_report.json` | SecretHunter | 敏感信息分析 |
| `api_endpoints.json` | EndpointMapper | 接口分析 |
| `endpoints_fuzz.txt` | EndpointMapper | Burp/FFUF 友好的 Fuzz 列表 |
| `crypto_analysis.json` | CryptoInspector | 加解密 / 签名分析 |
| `vuln_analysis.json` | VulnHunter | 八大维度漏洞 |
| `custom_analysis.json` | DeepDive(可选) | 定向深挖 |
| `security_report.md` | Reporter | **主报告** |
| `api_endpoints_full.md` | Reporter | 完整接口列表 |
| `secrets_full.md` | Reporter | 完整敏感信息列表 |
| `findings.json` | Reporter | 结构化汇总 |
| `domains.txt` | Reporter | 域名清单 |

> 全部位于 `{output_dir}`,不会污染 `{target_dir}`。

## 错误处理

| 场景 | 处理 |
|------|------|
| `{target_dir}` 不存在 | Phase 1 前终止,提示用户 |
| `{target_dir}` 含 `.wxapkg` | Inventory 终止,提示先反编译 |
| `{target_dir}` 无 `.js` 文件 | Inventory 终止,提示可能不是小程序源码 |
| 单个 Phase 2 Agent 失败 | 记录,其他继续,Reporter 在覆盖率章节标注缺失 |
| Phase 2 失败 ≥ 2 个 | 终止,向用户报告 |
| DeepDive 失败 | 不阻断,主报告标注定向分析未完成 |
| Reporter 失败 | 重试一次,仍失败则提示并保留 JSON |
| 用户定向目标在代码中找不到 | DeepDive 标 `not_found` 仍输出 |

## 大文件处理(Phase 2 各 Agent 通用)

| 大小 | 策略 |
|------|------|
| ≤ 200KB | 直接 Read 全文 |
| 200KB ~ 500KB | grep 关键模式 + ±20 行上下文 |
| 500KB ~ 1MB | 仅 grep,严禁全文 Read |
| > 1MB | 仅扫 Critical / High 级别模式,在 `large_files_*` 字段标注 |

> webpack 单文件打包(如 `app-service.js`)常 > 2MB,务必走 grep 路径。

## 覆盖率要求
- JS 文件扫描覆盖率 ≥ 95%
- JSON 配置文件扫描覆盖率 = 100%
- 各 Agent 在输出 `scan_summary.scan_coverage_percent` 中如实记录

## 设计要点(本 Skill 与传统脚本式审计的差异)

1. **零外部依赖**:不需要 Python、不需要 unveilr、不需要任何二进制工具,Agent 直接用 grep + Read 完成所有扫描。
2. **跨平台**:macOS / Linux / Windows 均可运行(只要 Claude Code 在,grep / Read 在)。
3. **责任分离**:通用扫描(Phase 2 四路) vs 定向深挖(Phase 2.5)严格分开,避免上下文污染。
4. **完整性兜底**:Reporter 强制做行数自检,确保 `_full.md` 文档逐条覆盖。
5. **可中断可重跑**:每个 Phase 的产物独立 JSON,任意阶段失败都能重启该阶段而不影响已完成产物。

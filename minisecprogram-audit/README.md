# wxmini-audit

> 微信小程序静态安全审计 Skill。**输入反编译后的源码目录,输出可交付的安全审计报告。**

## 一句话定位
**专注静态分析,不背反编译这口锅。** 你只需要把已经反编译好的小程序源码目录扔过来,Skill 自动跑完五大维度审计,输出主报告 + 完整接口列表 + 完整敏感信息列表 + Burp/FFUF 友好的 Fuzz 列表 + 结构化 JSON 汇总。

## 与传统方案的差异

| 维度 | 传统脚本式审计 | wxmini-audit |
|------|----------------|--------------|
| 反编译 | 内置 unveilr.exe / wxappUnpacker | **不做反编译,只接受解包后的源码** |
| 外部依赖 | Python + 二进制工具 | **零依赖**,只用 Claude 自带的 grep / Read |
| 平台 | 多数仅 Windows | **跨平台**(macOS / Linux / Windows) |
| 规则覆盖 | 脚本正则 + LLM 二次过滤 | LLM 直接走广覆盖正则 + 上下文复核,流程更短 |
| 通用扫 vs 定向 | 容易把用户定向需求和通用扫描混在一起 | **强制分离**,通用四路并行 vs 定向深挖 |
| 报告完整性 | 主报告易丢数据 | **强制行数自检**,完整数据走独立文档 |

## 特性

- **7 Agent 协作架构**:Inventory + SecretHunter + EndpointMapper + CryptoInspector + VulnHunter + Reporter + DeepDive
- **Phase 2 四路并行**:敏感信息 / 接口 / 加解密 / 漏洞 同时扫描
- **八大漏洞维度**:配置安全 / 认证授权 / 数据安全 / 业务逻辑 / WebView / 第三方组件 / 云开发 / 越权与水平权限
- **定向深挖按需触发**:用户提到具体接口/参数/Burp 抓包时,Phase 2.5 自动启动
- **零网络请求**:全程本地分析,不会触碰目标
- **不生成攻击代码**:只产出审计材料,合规友好

## 适用场景

- 红队 / 蓝队 SRC 漏洞挖掘前的源码侧深扫
- 内部安全审计 / 上线前安全检查
- 已经在 Burp 里观察到可疑流量,需要回到源码佐证
- 第三方小程序安全评估
- 安全研究、漏洞复现、教学

## 不适用场景

- 你给的还是一个 `.wxapkg`(本 Skill 不解包,请先用 unveilr / wxappUnpacker / wxapp-decoder 等工具处理)
- 你期望工具帮你打 Payload(本 Skill 不生成攻击代码,审计完拿着 `endpoints_fuzz.txt` + Burp 自己打)

## 项目结构

```
wxmini-audit/
├── SKILL.md                              # Orchestrator 主编排指令
├── README.md                             # 本文件
└── agents/
    ├── agent-01-inventory.md             # Phase 1 文件资产清单
    ├── agent-02-secret-hunter.md         # Phase 2 敏感信息全量挖掘
    ├── agent-03-endpoint-mapper.md       # Phase 2 接口提取与拓扑映射
    ├── agent-04-crypto-inspector.md      # Phase 2 加解密 / 签名审查
    ├── agent-05-vuln-hunter.md           # Phase 2 八大维度漏洞挖掘
    ├── agent-06-reporter.md              # Phase 3 报告与完整性兜底
    └── agent-07-deep-dive.md             # Phase 2.5 定向深挖(条件触发)
```

## 安装

把本目录扔到 Claude Code 的 Skill 目录下即可:

```bash
git clone <your-fork-url> wxmini-audit
# 或者直接复制到 ~/.claude/skills/wxmini-audit/
```

## 使用

### 1. 先把小程序源码反编译出来

本 Skill **不做反编译**。请用任一现成工具完成解包:

- [unveilr](https://github.com/wxapkg-tools/unveilr)
- [wxappUnpacker](https://github.com/xuedingmiaojun/wxappUnpacker)
- [wxapkg](https://github.com/wux1an/wxapkg)
- 任意你顺手的解包工具

得到一个含 `.js` / `.json` / `.wxml` 的目录即可。

### 2. 在 Claude Code 中触发 Skill

```
帮我分析这个小程序 /Users/me/work/wxmini-decompiled
```

或带上定向需求(自动触发 Phase 2.5):

```
帮我分析这个小程序 /Users/me/work/wxmini-decompiled,重点看 /api/user/login 接口
帮我分析这个小程序 ~/wxmini,Burp 抓包发现 /api/order amount 可篡改
帮我分析这个小程序 ./decompiled,关注支付安全和越权风险
```

### 3. 看输出

审计完成后,在你的当前工作目录会出现 `wxaudit-output/`:

| 文件 | 用途 |
|------|------|
| `security_report.md` | 主报告(关键发现 + 整体评估,优先看这个) |
| `api_endpoints_full.md` | 完整接口列表(逐条全量,贴进文档可用) |
| `secrets_full.md` | 完整敏感信息列表(含原值,渗透阶段使用) |
| `findings.json` | 结构化汇总(后续接 SOC / SIEM / 内部平台) |
| `domains.txt` | 去重的全部域名(可灌入子域枚举工具) |
| `endpoints_fuzz.txt` | Burp / FFUF 友好的 `METHOD URL` 列表 |
| `file_inventory.json` | 源码资产清单 |
| `secrets_report.json` | 敏感信息原始 JSON |
| `api_endpoints.json` | 接口原始 JSON |
| `crypto_analysis.json` | 加解密 / 签名 JSON |
| `vuln_analysis.json` | 八大维度漏洞 JSON |
| `custom_analysis.json` | 定向深挖 JSON(仅当你提了定向需求) |

## 审计覆盖维度

### 敏感信息(Critical → Info)
- 云厂商 AK/SK(阿里/腾讯/华为/AWS/Google/七牛/又拍 等)
- AppSecret / 微信支付 mch_key / 钉钉 / 企业微信凭证
- JWT / Bearer / Apollo / Nacos / 配置中心 token
- 内网 IP / 内网域名 / 测试环境 URL / HTTP 明文链路
- 个人信息样本(手机号、邮箱、身份证、银行卡)

### API 接口
- 完整 URL / 路径片段 / 模板字面量
- BaseURL ⨯ Path 智能关联,识别 dev/test/staging/prod 多环境
- 请求封装函数自动识别(axios.create / Taro.request / uni.request 同样适用)
- 路由表 / API 表批量提取
- 云函数 / 云数据库集合 / 云存储 / 云托管 全收集
- 域名按"业务/微信/支付/地图/统计/CDN/未分类"分组

### 加解密 / 签名
- crypto-js / jsencrypt / sm-crypto / forge / Web Crypto / 自实现哈希 全识别
- 算法 / 模式 / 填充 / Key / IV / 公私钥 全提取
- 签名算法 / 盐 / 时间戳 / nonce 全分析
- IV 全 0 / IV == Key / KDF 用 md5(常量) 等典型反模式自动告警

### 漏洞(八大维度)
1. **配置安全**:隐藏页面 / 调试模式 / 域名校验 / HTTP 明文
2. **认证授权**:登录态 / 前端鉴权 / 硬编码账号 / 敏感 API 调用
3. **数据安全**:本地存储 / 剪贴板 / 日志 / 退出残留
4. **业务逻辑**:金额篡改 / 短信轰炸 / 文件上传 / 优惠券
5. **WebView 安全**:URL 可控 / HTTP 加载 / postMessage / 跳转劫持
6. **第三方组件**:SDK 数据外传 / npm 包漏洞版本 / 插件
7. **云开发**:云函数枚举 / 数据库集合 / 存储路径 / 环境 ID
8. **越权与水平权限**:IDOR 候选清单 / ID 来源追溯 / 批量接口

### 定向深挖(可选)
用户提到具体接口 / 参数 / 函数 / 关注领域 / Burp 情报时,自动启动:
- 接口请求构造完整还原(URL / 方法 / header / body / query)
- 每个参数追溯到来源(用户输入 / Storage / 接口响应 / 硬编码)
- 前端校验逻辑 / 加密签名链路 / 响应处理
- 关联 Phase 2 已有发现(VULN-xxx / CRYPTO-xxx / SECRET-xxx)
- 给出"建议复测路径"列表

## 安全合规

1. **纯静态分析**:零网络请求,不验证密钥/Token/接口/域名,不连接任何远程服务
2. **不生成攻击代码**:不产 PoC、不产自动化攻击工具
3. **最小权限**:只读源码、只写输出目录,不修改任何文件
4. **数据不外传**:全程本地,不上传任何第三方

## 常见问题

**Q: 为什么不做反编译?**
反编译工具更新快、平台差异大、有时还涉及加密包(PC 微信 3.9+)。把这部分剥离出去,本 Skill 专注做"反编译之后的事",更稳定也更跨平台。

**Q: 我的小程序包是加密的,怎么办?**
PC 微信 3.9+ 默认加密 wxapkg,需要先用 `pc_wxapkg_decrypt` 等工具解密,或者从 Android 端(`/data/data/com.tencent.mm/MicroMsg/.../appbrand/pkg/`)拿未加密包。解出 `.js` / `.json` 后再喂给本 Skill。

**Q: 单文件 webpack 打包(`app-service.js` > 2MB)会不会卡死?**
不会。各 Agent 内置大文件分级策略:>1MB 强制走 grep,> 2MB 仅扫高优先级模式,Reporter 会在覆盖率章节如实标注。

**Q: 报告里发现了硬编码密钥,我能直接拿去验证吗?**
本 Skill 不会替你验证。你拿到原始 value(在 `secrets_full.md`)后,自行在合规授权范围内测试。**未授权访问他人资产违法**。

**Q: 跑出来的接口怎么直接灌进 Burp?**
`endpoints_fuzz.txt` 每行 `METHOD URL`,Burp Intruder 直接贴进去就能跑。FFUF 也兼容(`ffuf -w endpoints_fuzz.txt -u FUZZ`)。

## License

MIT

## 免责声明

本 Skill 仅供合法授权范围内的安全研究与审计使用。使用者需自行确保对目标小程序拥有合法的审计授权。任何未授权使用导致的法律后果由使用者自行承担,作者不承担任何责任。

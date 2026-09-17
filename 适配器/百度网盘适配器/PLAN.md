# PLAN.md —「光鸭云盘适配器」→「百度网盘适配器」改造方案

> 本文档基于对 `_har/百度网盘_完整操作.har`（1302 条请求，Chrome/Brave on Linux x86_64，2026-09-13 18:48–2026-09-14 02:45）的逆向分析，
> 以及对只读模板 `<项目根>/适配器/光鸭云盘适配器/`（44 文件 / 7058 行）的通读。
>
> **本阶段只做分析，未写任何业务代码。** 分析脚本与中间产物位于 `_har/`（已被 `.gitignore` 排除）。
> 光鸭模板目录未被修改，原始 HAR 未被修改。

---

## 0. 结论先行

| # | 结论 | 影响 |
|---|---|---|
| 1 | **百度网盘 Web 端没有客户端签名算法。** 全量 1302 条请求中，`sign` 只出现在缩略图 CDN 的 URL 上（`thumbnail0.baidupcs.com`），业务接口（`pan.baidu.com/api/*`）**一律不使用 sign**。 | 好消息：不需要逆向 JS 签名。认证 = Cookie + `bdstoken`。 |
| 2 | **认证模型与光鸭完全不同。** 光鸭是 `Bearer access_token` + `refresh_token` 的 OAuth 式模型；百度是 **纯 Cookie 会话（BDUSS）+ `bdstoken`**，无 refresh token、无 OAuth。 | `核心/认证/*` 三个模块需**整体重写**，不能沿用令牌刷新逻辑。 |
| 3 | ~~本 HAR 是 Chrome「脱敏导出」，Cookie 被剥离~~ → **已补齐并实测**。必需 Cookie 经真机验证为 **`BDUSS` + `STOKEN`**（2 个即可，`STOKEN` 是硬性必需项）。详见 §4.6。 | ✅ 原最大阻塞项 R1 已解除，Phase 0 可跳过 |
| 4 | **上传协议完全不同。** 光鸭走「阿里云 OSS V1 签名直传」；百度走「precreate → superfile2 分片 → create」三段式。 | `核心/接口/上传接口.py`（643 行）**整体废弃重写**。 |
| 5 | **百度对 MD5 使用「双轨制」**，用户可见的 md5 字段第 9 位存在异常字符（详见 §5.4）。 | ⚠️ 直接影响秒传与文件校验。见 R2。 |
| 6 | 光鸭骨架的分层（`启动.py → 界面 → 核心/接口+认证 → 核心/网络`）**干净且单向无环**，可作为百度适配器的骨架保留。 | 约 27% 的代码可原样保留或小改。 |

---

## 1. 分析产物与证据基础

### 1.1 脚本（`_har/`，临时工具，不入库）

| 脚本 | 作用 |
|---|---|
| `_har/解析.py` | 流式 HAR 解析器。`json.raw_decode` 滑动窗口逐条解析，内存恒定在单条 entry 量级（HAR1 143 MB / 1302 条，22 秒）。**强制脱敏**：敏感键只输出「长度+字符形态」指纹，不落盘明文。支持 `--har` / `--out` 解析任意 HAR。 |
| `_har/分析2.py` | 业务接口过滤（432/1300）、功能分类（25 类）、请求链重建、签名参数来源推导、必需请求头覆盖率。 |
| `_har/取证.py` | 针对性取证：md5 字符集、上传参数值域、登录链、未分类接口、filemetas target。 |
| `_har/提取JS.py` | 从 HAR 中提取客户端 JS 源码（上传器逻辑确认）。 |
| `_har/verify_cookie.py` | ✅ **Cookie 最小集验证器**：递进剔除阶梯测试，判定 `HTTP 200 且 errno==0`。 |
| `_har/verify_pcs_cookie.py` | ✅ **Q1c 探测**：验证 pcs 上传通道 Cookie 需求 + 本地/服务端 MD5 比对。**只做 precreate+superfile2，不调 `/api/create`**，不会在网盘产生文件。 |

### 1.2 分析结果（`_har/分析/`）

**HAR1（完整操作，1302 条）**：`01_域名与路径频次.md`、`02_query参数频次.md`、`03_请求头与Cookie.md`、
`06_请求明细.jsonl`（1.6 MB 机器可读明细）、`11_业务接口明细.md`、`12_请求链.md`、
`13_签名参数推导.md`、`14_必需请求头.md`、`15_关键接口样例.md`。

**HAR2（补充操作，866 条）**：`分析/补充/` 下的同名文件集。

### 1.3 流量构成

**HAR1（完整操作）**

| 域名 | 请求数 | 性质 |
|---|---|---|
| `pan.baidu.com` | 641 | **主业务 API** |
| `nd-static.bdstatic.com` | 244 | 静态资源（JS/CSS/字体） |
| `mbd.baidu.com` | 172 | 广告/营销 |
| `bddwd-cm01.pcs.baidu.com` | 80 | **分片上传通道**（40 POST + 40 CORS OPTIONS） |
| `staticsns.cdn.bcebos.com` | 56 | 静态图片 |
| `thumbnail0.baidupcs.com` | 37 | 缩略图 CDN |
| `passport.baidu.com` | 12 | **登录** |
| 其余 24 个域名 | 58 | 埋点/统计/广告/WebSocket |

**业务接口共 432 条**（过滤掉静态资源/埋点/广告 868 条）。

**HAR2（补充操作）**

| 域名 | 请求数 | 性质 |
|---|---|---|
| `pan.baidu.com` | 441 | **主业务 API** |
| `mbd.baidu.com` | 152 | 广告/营销 |
| `thumbnail0.baidupcs.com` | 128 | 缩略图 CDN |
| `nd-static.bdstatic.com` | 89 | 静态资源 |
| `passport.baidu.com` | 14 | 登录 / **风控验证** |
| `d.pcs.baidu.com` | 4 | **下载直链下发** |
| `bdbl-cm01.baidupcs.com` | 1 | **下载 CDN**（302 落地，非上传通道） |
| 其余 10 个域名 | 37 | 静态/埋点/广告 |

**HAR3（上传操作，86 条）**

| 域名 | 请求数 | 性质 |
|---|---|---|
| `bdbl-cm01.pcs.baidu.com` | 82 | **分片上传**（41 POST + 41 CORS OPTIONS） |
| `thumbnail0.baidupcs.com` | 4 | 缩略图 |

> ⚠️ 该抓包**只过滤了上传域名**，因此不含 `pan.baidu.com`（precreate/create/locateupload 均不在内）。
> 也**不含 >8MB 文件** —— 最大的是与 HAR1 同一个 5999010 字节 JPG（2 片），其余 39 个均为单片小文件。
> 但它提供了**分片大小的第二次独立确认**（精确 4194304）与**上传域名变化的决定性证据**（§5.2.2）。

**HAR4（转存操作，42 条）**

| 域名 | 请求数 | 性质 |
|---|---|---|
| `pan.baidu.com` | 34 | 业务 API（其中 25 条是 `/api/analytics` 埋点） |
| `mbd.baidu.com` | 7 | 广告 |
| `nd-static.bdstatic.com` | 1 | 静态资源 |

> 有效业务请求仅 **6 条**：`/share/transfer`、`/share/taskquery`×2、`/share/subscribe`×2、`/share/mountsinfo`。
> 抓取起点晚于分享页初始化，因此**提供 `sekey` 的请求未被录到**（见 Q13）。

---

## 2. 目录结构对比

### 2.1 现状

```
<项目根>/适配器/光鸭云盘适配器/          <项目根>/适配器/百度网盘适配器/
├── 启动.py                              ├── 启动.py
├── 项目配置.toml                        ├── 项目配置.toml
├── 核心/                                ├── 核心/
│   ├── 网络/                            │   ├── 网络/       ← 骨架已复制
│   │   ├── 网络客户端.py                │   ├── 认证/
│   │   ├── 设备身份.py                  │   ├── 接口/
│   │   └── 追踪上下文.py                │   ├── 上传/
│   ├── 认证/                            │   ├── 下载/
│   │   ├── 令牌仓库.py                  │   └── 埋点/
│   │   ├── 登录服务.py                  ├── 界面/          ← 骨架已复制
│   │   └── 认证服务.py                  ├── 数据/          ← gitignore
│   ├── 接口/                            ├── .gitignore
│   │   ├── 上传接口.py (643行)          └── _har/          ← gitignore
│   │   ├── 文件接口.py (294行)
│   │   ├── 云添加接口.py (262行)
│   │   ├── 分享接口.py (272行)
│   │   ├── 任务接口.py
│   │   ├── 资产接口.py
│   │   ├── 杂项接口.py
│   │   └── 埋点签名.py
│   ├── 上传/                            ← ⚠️ 全仓零引用的死代码
│   │   ├── 上传总调度.py
│   │   ├── 分片上传.py
│   │   ├── 秒传服务.py
│   │   └── 哈希计算器.py
│   ├── 下载/下载服务.py                 ← ⚠️ 未被使用（UI 内联 httpx）
│   └── 埋点/埋点上报.py                 ← ⚠️ 整包未被引用
└── 界面/                                └── 界面/
    ├── 主界面.py (507行)                    ├── 主界面.py
    ├── 字段映射.py (168行)                  ├── 字段映射.py
    ├── 格式化工具.py                        ├── 格式化工具.py
    ├── 样式.py / 公共线程.py                ├── 样式.py / 公共线程.py
    ├── 页面/ (7 个页面)                      ├── 页面/ (7 个页面)
    └── 弹窗/ (3 个弹窗)                      └── 弹窗/ (3 个弹窗)
```

### 2.2 目标结构（改造后）

**保留骨架，目录名不变**，只替换文件内容与新增两个模块：

```
核心/
├── 网络/
│   ├── 网络客户端.py       ← 重写：Cookie 注入 + 百度公共 query 参数 + dp-logid
│   ├── 设备身份.py         ← 小改：设备指纹改为 BAIDUID/cuid，去掉 smid
│   └── 追踪上下文.py       ← 原样保留（W3C traceparent，平台无关）
├── 认证/
│   ├── 会话仓库.py         ← 新（替代 令牌仓库.py）：BDUSS/bdstoken/uk 的持久化
│   ├── 登录服务.py         ← 重写：二维码扫码 + 手工 BDUSS 导入
│   └── 认证服务.py         ← 重写：/api/gettemplatevariable + /api/user/getinfo
├── 接口/
│   ├── 上传接口.py         ← 重写：precreate / superfile2 / create / rapidupload
│   ├── 文件接口.py         ← 重写：list / filemetas / categorylist
│   ├── 管理接口.py         ← 新（替代 云添加接口.py）：filemanager 的 delete/rename/move/copy
│   ├── 回收站接口.py       ← 新：/api/recycle/list/
│   ├── 分享接口.py         ← 重写：/share/pset + /share/taskquery
│   ├── 搜索接口.py         ← 新：/api/search
│   └── 杂项接口.py         ← 重写：quota / getsyscfg
├── 上传/
│   ├── 哈希计算器.py       ← 小改：分片 MD5（原为 SHA1/GCID，百度要 MD5）
│   ├── 分片上传.py         ← 重写：4MB 分片 + superfile2 multipart
│   ├── 秒传服务.py         ← 重写：rapidupload（含 content-md5/slice-md5）
│   └── 上传总调度.py       ← 小改：接线 + 改为调用新分片实现
└── 下载/下载服务.py        ← 重写：filemetas(dlink) + Range 流式 GET
```

**删除**：`核心/埋点/`（百度无需光鸭埋点，且原包已死代码）、`核心/接口/埋点签名.py`。

---

## 3. 模块映射表

**改造量图例**：🟢 原样保留　🟡 小改（常量/字段）　🟠 中改（参数化+换业务码）　🔴 重写

| 光鸭模块 | 行数 | 改造量 | 百度对应 | 关键变更点 |
|---|---|---|---|---|
| `核心/网络/追踪上下文.py` | 43 | 🟢 | 同 | W3C `traceparent`，纯标准，零改动 |
| `界面/样式.py` | 28 | 🟢 | 同 | 纯 Qt QSS |
| `界面/页面/基类.py` | 56 | 🟢 | 同 | `启动网络任务` 抽象与平台无关 |
| `核心/上传/哈希计算器.py` | 33 | 🟡 | 分片 MD5 | 百度要 **MD5**（光鸭是 SHA1/GCID）；需支持「首 256KB MD5」用于 `slice-md5` |
| `核心/网络/设备身份.py` | 65 | 🟡 | 设备指纹 | 存储路径 `~/.光鸭云盘` → `~/.百度网盘`；光鸭头 `did/dt/smid` → 百度无对应头，改为维护 `BAIDUID` |
| `界面/公共线程.py` | 108 | 🟡 | 同 | QThread + Signal 模型通用，仅改 logger 前缀 |
| `界面/格式化工具.py` | 95 | 🟡 | 同 | 文件大小/时间格式化通用；去掉对 `核心.认证.登录服务.默认国家码` 的反向 import（依赖倒置） |
| `核心/上传/上传总调度.py` | 117 | 🟡 | 上传编排 | 接线 + 改调百度三段式；修正 `cid = gcid` 赋值 bug |
| `核心/网络/网络客户端.py` | 436 | 🟠 | 同 | ①错误模型从 `code/msg` 改为 `errno/errmsg`；②公共 query 注入（见 §4.4）；③`dp-logid` 生成；④Cookie 会话注入；⑤去掉 `authorization` Bearer |
| `核心/接口/任务接口.py` | 60 | 🟠 | `/share/taskquery` | 百度异步任务轮询（删除/移动返回 `taskid`） |
| `核心/接口/资产接口.py` | 87 | 🟠 | `/api/quota` | 容量字段基本同构（`total`/`used`/`free`） |
| `核心/接口/杂项接口.py` | 36 | 🟠 | 杂项 | `/api/getsyscfg`、`/api/loginStatus` |
| `核心/下载/下载服务.py` | 57 | 🔴 | 下载 | `/api/filemetas?dlink=1` 取 `dlink` → Range 流式 GET |
| `核心/接口/上传接口.py` | 642 | 🔴 | 上传 | OSS V1 签名 → Baidu PCS 三段式（详见 §5） |
| `核心/认证/登录服务.py` | 361 | 🔴 | 扫码登录 | 6 个 Authing 端点 → 百度 passport 4 步（详见 §4.3） |
| `核心/认证/令牌仓库.py` | 341 | 🔴 | `会话仓库.py` | refresh_token 模型 → BDUSS/bdstoken 持久化（详见 §4.2） |
| `核心/认证/认证服务.py` | 33 | 🔴 | 会话自检 | `/v1/user/me` → `/api/gettemplatevariable` + `/api/loginStatus` |
| `核心/接口/文件接口.py` | 294 | 🔴 | 文件浏览 | 8 个光鸭端点 → `/api/list`、`/api/filemetas`、`/api/categorylist` |
| `核心/接口/分享接口.py` | 272 | 🔴 | 分享 | → `/share/pset` + `/share/taskquery`；`shareId/id` 双轨 → `shareid`/`shorturl` |
| `核心/接口/云添加接口.py` | 262 | 🔴 | 离线下载 | 百度 `/rest/2.0/services/cloud_dl`（本 HAR 仅 3 条样本，需补抓） |
| `核心/接口/埋点签名.py` | 11 | 🔴 | — | 删除（百度无对应） |
| `核心/埋点/埋点上报.py` | 92 | 🔴 | — | 删除 |
| `核心/上传/分片上传.py` | 128 | 🔴 | 分片上传 | oss2 分片 → superfile2 multipart；4 MB 分片 |
| `核心/上传/秒传服务.py` | 112 | 🔴 ✅ | 秒传 | `get_res_center_token`/码 156 → `/api/rapidupload`/`errno 404`；原 `KeyError: 'gcid'` 随 OSS 体系移除 |
| `界面/字段映射.py` | 168 | 🔴 | 字段映射 | ~190 条光鸭中文名 → 百度字段（见 §3.1） |
| `界面/主界面.py` | 507 | 🟠 | 同 | 导航结构保留，换文案与硬编码 Tab 索引 |
| `界面/页面/*.py`（7 个） | 2334 | 🟠 | 同 | 页面骨架保留，换字段来源与文案 |
| `界面/弹窗/*.py`（3 个） | 551 | 🟠 | 同 | 二维码弹窗可复用；短信登录改为 BDUSS 导入 |

**统计**（实测行数，全仓 7038 行；下表覆盖 7008 行，未计 `启动.py` 的 30 行）：

| 改造量 | 文件数 | 行数 | 占比 |
|---|---|---|---|
| 🟢 原样保留 | 3 | 127 | 1.8% |
| 🟡 小改 | 5 | 418 | 5.9% |
| 🟠 中改 | 17 | 3690 | 52.4% |
| 🔴 重写 | 13 | 2773 | 39.4% |
| **合计** | **38** | **7008** | **99.6%** |

**界面层共 3526 行（50.1%）**，其中页面/弹窗 2620 行基本只需换字段来源与文案，是改造中性价比最高的部分。
**真正的重写成本集中在 13 个 🔴 文件（2773 行）**，其中 `上传接口.py`（642）+ `登录服务.py`（361）+ `令牌仓库.py`（341）三者为最大单体。

### 3.1 文件对象字段映射（来自 `/api/list` 实测 schema）

| 百度字段 | 类型 | 光鸭对应 | 中文名 |
|---|---|---|---|
| `fs_id` | int | `id` | 文件 ID |
| `path` | str | `path` | 完整路径 |
| `server_filename` | str | `name` | 文件名 |
| `isdir` | 0/1 | `resType==2` | 是否目录 |
| `size` | int | `size` | 大小 |
| `server_mtime` | int | `modifyTime` | 修改时间 |
| `server_ctime` | int | `createTime` | 创建时间 |
| `md5` | str | `hash` | MD5（⚠️ 第 9 位异常，见 §5.4） |
| `category` | int | `mineType` | 分类（1视频/2音频/3图片/4文档/5应用/6其他/7种子） |
| `oper_id` | int | — | 操作 ID |

**注意**：光鸭用的是 `resType==2` 判目录，百度用 `isdir`；光鸭字段名有拼写错误 `mineType`（应为 mimeType），改造时建议一并修正为 `category`。

---

#### §3.1 补充：Q10 字段语义（✅ 2026-09-14 Phase 2 实测解决）

**采样方式**：递归遍历 49 个目录，收集 **406 条**文件对象（289 文件 / 117 目录），
统计每个字段的取值分布并与扩展名交叉验证。复现见 `_har/spike_file_list.py`。

##### ① 完整字段清单（实测 32 个）

| 类别 | 字段 |
|---|---|
| **全类型都有** | `fs_id` `path` `server_filename` `isdir` `size` `category` `server_mtime` `server_ctime` `server_atime` `local_mtime` `local_ctime` `real_category` `unlist` `wpfile` `oper_id` `owner_id` `owner_type` `share` `is_scene` `from_type` `pl` `tkbind_id` `black_tag` `extent_int2` `extent_int8` `extent_tinyint7` |
| **仅文件有** | `md5` `thumbs` `docpreview` `lodocpreview` `is_root` |
| **仅目录有** | `dir_empty` `empty` |

> ⚠️ **两条容易踩的坑**（已据此实现）：
> 1. **目录对象也有 `category`，且恒为 `6`**；`size` 恒为 `0`。
>    → **判目录只能用 `isdir == 1`**；用 category 判会把所有目录误判为「其它」。
> 2. **`/api/categorylist` 与 `/api/filemetas` 的返回键都是 `info`**，只有 `/api/list` 是 `list`。

##### ② 关键字段语义

| 字段 | 取值 | 语义（实测依据） |
|---|---|---|
| `isdir` | 0 / 1 | **是否目录**（1=目录）。目录的 size=0、category=6 |
| `category` | 1/3/4/5/6 | 文件分类，见下方对照表 |
| `real_category` | `''` `png` `rar` `pptx/xlsx/docx/zip` … | **真实类型字符串**，比 `category` 更细（11 种取值）。目录恒为 `''` |
| `oper_id` | `0` / `455281002` | **最后操作者的 uk**。`455281002` 正是本人 uk（181 条恰为我刚上传的文件）；`0` = 无操作记录 |
| `owner_id` | `0` / 各 uk | **文件所有者 uk**。`0` = 自己 |
| `owner_type` | 0 / 1 | **0 = 自己拥有**（配 `owner_id=0`，224 条）；**1 = 他人拥有**（配 `owner_id=<对方 uk>`，182 条，来自分享转存） |
| `share` | 0 / 1 | 是否已处于分享中（406 条中 3 条为 1） |
| `is_scene` | 0 / 1 | 是否场景/来源文件 |
| `dir_empty` | 0 / 1 | **仅目录**：目录是否为空（0=非空 66 个，1=空 51 个） |
| `empty` | 0 / 1 | **仅目录**，与 `dir_empty` 同时出现，疑为冗余 |
| `unlist` | 0 | 是否从列表隐藏。**本次 406 条全为 0**，未覆盖 1 的情况 |
| `wpfile` | 0 | 是否 WPS 在线文档。**全为 0** |
| `pl` | 0/1/2/4 | ⚠️ **语义未确定**。分布：`1`×178、`0`×155、`2`×69、`4`×3。与 `isdir`/`owner_type` 相关但非决定关系（`pl=2` 绝大多数是目录，但也有 7 个文件）。**疑似权限等级或路径层级**，本次未能定论 |
| `from_type` | 0 | 来源类型。全为 0 |
| `tkbind_id` | 0 | 图库绑定 ID。全为 0 |
| `black_tag` | 0 | 黑名单标记。全为 0 |
| `server_atime` | 0 为主 | 访问时间，绝大多数未记录 |
| `local_mtime` / `local_ctime` | 时间戳 | **客户端上报**的本地时间（与 server_* 可能不同） |
| `thumbs` | `{"icon": url}` | **仅部分文件**（115/406）有缩略图地址 |
| `docpreview` / `lodocpreview` | — | 仅 38 个文件有，文档预览信息 |
| `is_root` | — | 仅 3 个文件有，语义待定 |
| `extent_int2` / `extent_int8` / `extent_tinyint7` | 0 为主 | 保留扩展字段 |

##### ③ `category` 对照表（406 条样本 + 扩展名交叉验证）

| 值 | 含义 | 实测覆盖的扩展名 |
|---|---|---|
| `1` | 视频 | `mp4` |
| `2` | 音频 | ⚠️ **本次样本未覆盖**（按百度公开约定） |
| `3` | 图片 | `png`(74) `jpg`(2) |
| `4` | 文档 | `pdf`(19) `txt`(17) `xml` `docx` |
| `5` | 应用 | `exe`(21) `apk`(16) |
| `6` | 其它 | `rar`(42) `zip`(37) `py`(33) `7z` `sublime-package` `crx` `iso`、无扩展名(9)、**以及全部 117 个目录** |

> 光鸭旧表里的「7=种子」在本次样本中**未出现**；`6` 实际涵盖了所有未归类格式。

##### ④ 分页特性（影响 UI 实现）

`/api/list` 响应**不返回 `total`**（实测键只有 `errno`/`guid`/`guid_info`/`list`/`request_id`），
因此**无法预知总页数**。UI 只能按「本页条目数是否等于 `num`」推断是否还有下一页
（`界面/页面/文件浏览页.py` 已按此实现）。

---

## 4. 认证模型对比

### 4.1 总览

| 维度 | 光鸭云盘 | 百度网盘 |
|---|---|---|
| 凭证形态 | `access_token`（JWT，7200s）+ `refresh_token` | **Cookie：`BDUSS`**（长期，数月）+ `STOKEN` |
| 传输方式 | `Authorization: Bearer <token>` 请求头 | **Cookie 头**（HAR 中被脱敏剥离） |
| CSRF 令牌 | 无 | **`bdstoken`**（32 位 hex，来自 `/api/gettemplatevariable`） |
| 刷新机制 | `refresh_token` 静默续期 | **无刷新概念**，BDUSS 失效即重新登录 |
| 用户标识 | `sub`/`uid`（JWT claim） | `uk`（int，来自 `gettemplatevariable`/`getinfo`） |
| 登录方式 | 设备码 / 扫码 / 短信（Authing） | **二维码扫码** 或 手工粘贴 BDUSS |
| 设备头 | `did` / `dt` / `smid` / `traceparent` | 无设备头；依赖 Cookie 内的 `BAIDUID` |
| 业务错误码 | `code`（0 成功，147 上传中，156 秒传命中） | **`errno`**（0 成功，-6 未登录，404 秒传未命中，-9 文件不存在…） |

### 4.2 会话仓库（替代令牌仓库）

光鸭 `令牌仓库.py` 的刷新回调 / 过期判断 / 并发锁机制**在百度模型下全部无用**，可精简为：

```
数据/会话.json
{
  "bduss":   "<redacted>",     // 长期凭证，等价于密码
  "bduss_bfess": "<redacted>", // BDUSS 的 BFE 边缘节点变体（可选冗余，实测可互换）
  "stoken":  "<redacted>",     // ⚠️ 实测为「所有业务请求」的硬性必需项，非仅分享场景
  "bdstoken": "<32位hex>",     // CSRF，会随会话变化，需定期重取
  "uk":      455281002,        // 用户 ID
  "baiduid": "<redacted>",     // 设备标识（非必需，服务端会自动下发）
  "更新时间": 1789325407
}
```

**每个请求必须同时携带 `BDUSS` + `STOKEN`**（§4.6 实测：缺 `STOKEN` 时 `errno: -6`）。
`STOKEN` 在光鸭的认证模型里没有任何对应物，是本次改造新增的必需概念。

**必须解决的继承缺陷**：光鸭骨架存在「**令牌不回流**」致命 bug —— `网络客户端.访问令牌` 只是登录时的一次性快照，全仓 4 个 `设置访问令牌` 调用点无一将刷新后的新令牌回灌，导致会话超 2 小时后持续 401。
百度模型下 `bdstoken` 同样会变，**必须在网络客户端注入 `获取会话` 回调，每请求前实时取值**，而不是构造时快照。

### 4.3 登录链路（实测四步）

```
① GET  passport.baidu.com/v2/api/getqrcode
        ?lp=pc&qrloginfrom=pc&gid=<GUID>&apiver=v3&tpl=netdisk&tt=<ms>
   → 返回 { qrcode: <base64图片>, sign: <hex32>, channel_id: <hex32>, ... }

② GET  passport.baidu.com/v2/api/qrcode?sign=<sign>&lp=pc&qrloginfrom=pc
   → 二维码图片（PNG）

③ GET  passport.baidu.com/channel/unicast
        ?channel_id=<hex32>&gid=<GUID>&tpl=netdisk&apiver=v3&tt=<ms>   ← 轮询
   → 状态：0 未扫 / 1 已扫待确认 / 2 已确认（返回 BDUSS）

④ GET  passport.baidu.com/v3/login/main/qrbdusslogin
        ?bduss=<BDUSS>&u=<urlencode(跳转)>&loginVersion=v5&qrcode=1&tpl=netdisk&apiver=v3&time=<s>
   → 302，Set-Cookie: BDUSS=...  （⚠️ 本 HAR 中 Set-Cookie 已被剥离）

⑤ GET  passport.baidu.com/v3/login/api/auth/?return_type=5&tpl=netdisk&u=https://pan.baidu.com/disk/main
   → 302 → https://pan.baidu.com/disk/main          # 建立 pan 域会话

⑥ GET  passport.baidu.com/v3/login/api/auth?return_type=3&tpl=netdisk
        &u=https://pcs.baidu.com/rest/2.0/pcs/file?method=plantcookie&source=pcs&...
   → 302 → pcs.baidu.com/rest/2.0/pcs/file?method=plantcookie&source=pcs
   → 再对 pcsdata.baidu.com 重复一次
   # 作用：把 BDUSS 会话「种植」到 pcs / pcsdata 子域，供上传下载通道使用
```

**扫码后必须执行步骤 ⑤⑥**，否则 `pcs.baidu.com` 与 `bddwd-*.pcs.baidu.com`（上传通道）会 401。

### 4.4 每个 pan.baidu.com 业务请求的公共参数

实测每条业务请求都带（顺序无关）：

| 参数 | 值 | 说明 |
|---|---|---|
| `clienttype` | `0` | 客户端类型（`loginStatus` 用 `1`） |
| `app_id` | `250528` | 网盘 Web 版固定 app id |
| `web` | `1` | Web 端标记 |
| `channel` | `chunlei` | 渠道标识（部分接口用 `web`） |
| `dp-logid` | `<16位前缀><4位序号>` | 客户端生成。见下 |
| `bdstoken` | `<32位hex>` | **仅写操作需要**（precreate/create/rapidupload/filemanager/share） |

**`dp-logid` 格式（已实测确认）**：`<16位会话前缀><4位递增序号>`。同一页面加载内前缀恒定（22 个不同前缀 = 22 次页面加载），序号从 `0001` 起随每次请求递增（观测范围 1..316）。
前缀看似随机数字串（如 `2611190077238122`、`9730890010474785`）。实现建议：进程内生成一次随机 16 位前缀 + 线程安全自增计数器。

**`superfile2` 的 `logid` 参数（已实测确认）**：`base64("<15位数字>.<17位随机数字>")`，例如 `MTc4OTMyNTQwNzUyODAuMTY2NjkzMDc5ODkyODU5NDI=` → `17893254075280.16669307989285942`。

### 4.5 必需请求头

| 请求头 | 必需性 | 值 |
|---|---|---|
| `User-Agent` | **必需** | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36`（抓包环境为 Brave 151 / Linux） |
| `Referer` | **必需** | `https://pan.baidu.com/`（登录页为 `https://pan.baidu.com/login?...`） |
| `Origin` | 跨域请求必需 | `https://pan.baidu.com`（superfile2 上传播带） |
| `Cookie` | **必需** | **`BDUSS=<...>; STOKEN=<...>`（实测最小集，见 §4.6）。建议额外附带 `BDUSS_BFESS` 作为 BFE 边缘节点冗余** |
| `Accept` | 建议 | `application/json, text/plain, */*` |
| `X-Requested-With` | 建议 | `XMLHttpRequest` |
| `Content-Type` | 按需 | `application/x-www-form-urlencoded`（precreate/create/filemanager/share）或 `multipart/form-data`（superfile2） |

**不需要**：`sec-*` 系列（浏览器安全头）、CORS `OPTIONS` 预检（Python 客户端直接 POST，可省去 40 条 OPTIONS）。

### 4.6 ✅ 必需 Cookie 最小集（真机实测结论）

**验证方法**：`_har/verify_cookie.py`，对 `GET /api/gettemplatevariable?fields=["bdstoken","uk","servertime"]&clienttype=0&app_id=250528&web=1` 做「逐步剔除」阶梯测试，判据为 `HTTP 200 且 errno == 0`。

**实测结果**（输入为 34 个 cookie 的完整集合）：

| # | Cookie 子集 | 数量 | HTTP | errno | 结果 |
|---|---|---|---|---|---|
| ① | 全量 | 34 | 200 | 0 | ✅ 通过 |
| ② | `BDUSS` + `BDUSS_BFESS` + `STOKEN` | 3 | 200 | 0 | ✅ 通过 |
| ③ | **`BDUSS` + `STOKEN`** | **2** | 200 | 0 | ✅ **通过（最小集）** |
| ④ | `BDUSS` + `STOKEN` + `BAIDUID` | 3 | 200 | 0 | ✅ 通过 |
| ⑤ | `BDUSS_BFESS` + `STOKEN` | 2 | 200 | 0 | ✅ 通过 |
| ⑥ | 仅 `BDUSS` | 1 | 200 | **-6** | ❌ 失败 |
| ⑦ | 仅 `BDUSS_BFESS` | 1 | 200 | **-6** | ❌ 失败 |
| ⑧ | `BDUSS` + `BDUSS_BFESS`（无 STOKEN） | 2 | 200 | **-6** | ❌ 失败 |
| ⑨ | 仅 `STOKEN` | 1 | 200 | **-6** | ❌ 失败 |

**三条结论**：

1. **`STOKEN` 是硬性必需项** —— 这是反直觉的发现。传统认知里 `STOKEN` 只用于分享场景，但实测表明**缺少 `STOKEN` 时即使 `BDUSS` 完全有效也返回 `errno: -6`**（未登录）。说明百度新版接口对网盘业务普遍校验 `STOKEN`。**实现时必须在每个业务请求上同时携带 `STOKEN`**，这是光鸭模型里完全没有的概念。
2. **`BDUSS` 与 `BDUSS_BFESS` 可互换**（互为备份）。单独任一个 + `STOKEN` 都通过；两者同时带也无害。建议**同时携带两者**，以应对百度 BFE 边缘节点在不同机房对 cookie 名的偏好差异。
3. **设备类与埋点类 Cookie 全部非必需**。`BAIDUID` / `BIDUPSID` / `PSTM` / `H_PS_PSSID` / `BA_HECTOR` / `Hm_lvt_*` / `BDRCVFR[...]` 等 32 个均可剔除，服务端会自动重新下发。

**推荐的实现取值**：`BDUSS; BDUSS_BFESS; STOKEN`（3 个，两个 BDUSS 变体做冗余）。绝对最小可用为 `BDUSS; STOKEN`。

> ⚠️ `errno: -6` 是百度的「未登录」错误码，应作为会话失效的判定依据，触发重新登录流程。这一条已可直接用于 Phase 1.1 的错误模型设计。


---

## 5. 上传协议对比

### 5.1 光鸭（现状）

```
① 获取资源中心令牌  POST /userres/v2/get_res_center_token   业务码 156 = 秒传命中
② 阿里云 OSS V1 签名  Authorization: OSS <AccessKeyId>:<Signature>
③ 整体 PUT（<100MB）或分片 PUT（10MB 分片，oss2 SDK）
④ 轮询任务状态（业务码 147 = 上传中）
```

### 5.2 百度（目标）

```
① 秒传尝试（可选）
   POST https://pan.baidu.com/api/rapidupload?rtype=1&bdstoken=...
   Content-Type: application/x-www-form-urlencoded
   body: path, content-length, content-md5, slice-md5, target_path, local_mtime,
         uploadid（可选）, data_time, data_offset, data_content     ← 后三个必需！
   响应: errno=0 命中并生成文件；errno=404 = 未命中，回落分片上传
   ⚠️ 字段细节见 §5.2.1「秒传必需字段」，缺 data_* 会得到 errno:2（参数错误）

② 预创建
   POST https://pan.baidu.com/api/precreate?rtype=1&bdstoken=...
   body: path=/x.jpg, autoinit=1, block_list=["<分片MD5>",...], target_path=/, local_mtime=<s>
   响应: { errno:0, return_type:1, block_list:[...], uploadid:"P1-<base64>", request_id }

③ 分片上传（每片一次）
   POST https://bddwd-cm01.pcs.baidu.com/rest/2.0/pcs/superfile2
        ?method=upload&logid=<base64>&app_id=250528&channel=chunlei&web=1&clienttype=0
        &path=/x.jpg&uploadid=<②的uploadid>&uploadsign=0&partseq=<N>&dp-logid=...
   Content-Type: multipart/form-data; 字段名 file
   响应: { md5:"<服务端算的该片MD5>", partseq:"0", uploadid:"...", request_id }

④ 创建文件
   POST https://pan.baidu.com/api/create?isdir=0&rtype=1&bdstoken=...
   body: path=/x.jpg, size=<字节>, uploadid=<②>, block_list=[<③返回的MD5列表>], target_path=/, local_mtime=<s>
   响应: { errno:0, fs_id, md5, path, size, ctime, mtime, category, from_type }
```

**关键参数细节（全部实测确认）**：

| 项 | 值 | 备注 |
|---|---|---|
| 分片大小 | **4 MB**（4194304） | ✅ **两次独立确认**：① Phase 1.6 合成 9 MB 文件 → 3 片（4194304/4194304/1048576）全 200 全匹配；② 上传 HAR 中 5999010 字节文件 → 2 片，`Content-Length` 4194496 − 192(multipart 开销) = **精确 4194304** |
| `uploadsign` | **恒为 `"0"`**（80/80） | precreate 响应**不含** `uploadsign` 字段；JS 中 `e.uploadSign = i.uploadsign` 取不到则为 undefined，URL 拼成 `uploadsign=0`。实现时直接写死 `"0"` |
| `rtype` | 恒为 `1` | |
| `isdir` | 恒为 `0` | 上传文件 |
| `autoinit` | `1` | |
| `block_list`（precreate **请求**） | **客户端计算**的分片 MD5，干净 hex | ⚠️ precreate **不校验**它：实测传一个第 9 位被污染的 md5 依然返回 `errno:0` |
| `block_list`（precreate **响应**） | **`[0, 1, 2, ...]` 占位序号**，不是 MD5 | ✅ 已核验：HAR1 中 4 条响应分别为 `[0,1]` / `[0]` / `[0]` / `[0]`。**不要把它当作 MD5 列表回传** |
| `block_list`（create **请求**） | **服务端 superfile2 返回**的 MD5 列表 | 与 precreate 请求的值**不同**（见 §5.4） |
| `uploadid` | `"P1-<base64>"` / `"N1-<base64>"`，**恒为 63 字符** | 由 precreate 下发。实测 63 字符，前缀 `P1-`(个人空间) / `N1-` |
| 上传域名 | **动态！必须从 `locateupload` 取 `server[0]`** | 🔴 **Q5 已实测解决**：同账号同机 ~1 小时内，HAR1 用 `bddwd-cm01.pcs.baidu.com`、上传 HAR 用 `bdbl-cm01.pcs.baidu.com` —— **域名确实会变，绝不可硬编码**。来源已确证（见 §5.2.2） |

> ✅ **Q1c 已实测验证（2026-09-14）**：`superfile2` **只需 `BDUSS` + `STOKEN`**，无需 `plantcookie`、无需 `BAIDUID` 等其它 Cookie。
> 实测 `POST https://bddwd-cm01.pcs.baidu.com/rest/2.0/pcs/superfile2` 返回 `HTTP 200`，
> 且**最小集与全量 Cookie 的结果完全一致**。复现脚本：`_har/verify_pcs_cookie.py`（不调 `/api/create`，不会产生文件）。

#### 5.2.1 秒传（rapidupload）必需字段 ✅ Phase 1.6 实测打通

**从上传器 JS 提取的官方构造逻辑**：

```js
o = { uploadid, path, "content-length": size,
      "content-md5": this._encryptMd5(contentMD5),    // ← 必须传加密态！
      "slice-md5":   this._encryptMd5(sliceMD5) }     // ← 必须传加密态！
a = Math.floor(Date.now()/1000)                       // data_time = 秒级时间戳
c = parseInt(makeMD5(uk + o["content-md5"] + a).substr(0,8), 16)
        % (size - 262144 + 1)                         // data_offset 确定性公式，非随机
o.data_content = base64(file.slice(c, c + 262144))    // 取 256KB 并 base64
```

**Python 实现要点**：

```python
data_time    = int(time.time())
偏移         = int(hashlib.md5(f"{uk}{content_md5_enc}{data_time}".encode())
                   .hexdigest()[:8], 16) % (大小 - 262144 + 1)
data_content = base64.b64encode(内容[偏移 : 偏移 + 262144]).decode()
```

**实测结果（`_har/spike_rapidupload.py`）**：

| 场景 | 结果 |
|---|---|
| 本地文件、云端不存在 | `errno: 404` ✅ 符合预期 |
| **真实上传后再秒传同一文件** | **`errno: 0` ✅ 命中！秒传链路打通** |
| 缺 `data_time`/`data_offset`/`data_content` | `errno: 2`（参数错误）← 最初的失败原因 |
| `data_content` 用占位内容（非真实字节） | `errno: 2` ← **服务端确实校验 data_content** |

**🔴 实测限制：小于 256 KB 的文件无法秒传**

Phase 3+4 验证时发现，1 KB 文件调 `rapidupload` 稳定返回 **`errno:2`**（参数错误）：

| 文件大小 | rapidupload 结果 |
|---|---|
| 1 KB | ❌ `errno:2` |
| 300 KB | ✅ `errno:404`（未命中，正常） |
| 1 MB | ✅ `errno:0`（命中） |

**原因**：持有证明 `data_content` 固定取 256 KB，而偏移公式的分母为
`size - 256KB + 1`。文件小于 256 KB 时该值为负，**无法构造出有效的偏移与片段**。
（上传器 JS 对此也未做特殊处理，`file.slice()` 会返回空内容。）

**处理**：`秒传服务.试秒传()` 在 `大小 < 256 KB` 时**直接返回 False 跳过秒传**，
由调用方回落分片上传，并记录日志。小文件本身上传极快，跳过秒传无实质损失。

**其它实测结论**：

- `slice-md5` = `加密md5(md5(首 256 KB))` —— 实验 4 用此值成功命中，**公开约定成立**
- `content-md5` = `加密md5(md5(整文件))`
- `uploadid` **非必需**（带与不带都能得到正确的 404/0）
- `data_offset` 是**确定性公式**而非随机数 → 可用已知内容复现，对断点续传友好
- 服务端按 `content-md5` 判断文件是否已存在，**与 `path` 无关** → 秒传可用于同文件换名/换目录

#### 5.2.2 ✅ 上传域名是动态的，必须从 `locateupload` 获取（Q5 实测解决）

**实测对比（同账号、同机器、约 1 小时内）**：

| 抓包 | 时间 | 实际上传域名 | 该 HAR 中 `locateupload` 返回的 `server` |
|---|---|---|---|
| `完整操作.har` | 02:55 | `bddwd-cm01.pcs.baidu.com` | 第 1 次 `['bdbl-cm01…','c7…']`；**第 2 次 `['bddwd-cm01…','c7…']`** |
| `上传操作.har` | 03:47 | `bdbl-cm01.pcs.baidu.com` | （该 HAR 只抓了上传域，无此调用） |
| `补充操作.har` | 03:30 | （无上传） | 3 次均为 `['bdbl-cm01…','c7…']` |
| **Phase 1.6 探测** | 03:5x | `bddwd-cm01`（硬编码自 HAR1） | `['bdbl-cm01…','c7…','c.pcs…']` |
| **Phase 3+4 验证** | 04:1x | **`xafj-cm11.pcs.baidu.com`** | `['xafj-cm11…','c7…','c.pcs…']` |

> 📌 **已观测到 3 个不同的上传节点**：`bddwd-cm01` / `bdbl-cm01` / `xafj-cm11`。
> 这彻底坐实了「必须动态解析」的结论 —— 无论硬编码哪一个都会在某个时刻失效。

**决定性证据**：`完整操作.har` 中 `locateupload` 被调用了 2 次，第 1 次返回 `bdbl-cm01`、第 2 次返回 `bddwd-cm01`，
而**随后的实际上传用的正是第 2 次的 `server[0]`**。上传器 JS 完全印证这一点：

```js
_locateUpload = function(e) {
    Sc.get(this._options.locateUploadUrl, function(n) {
        null != n.server && (Object.assign(t.conf, {
            serverUrlIndex: 0,
            clientIp:   n.client_ip,
            serverHost: n.server[0],        // ←★ 直接用 server[0]
            serverUrlList: n.server
        }), t.conf.serverUrlList.push(t.conf.defaultServerHost),
           t._setServerUrl(e), n.timestamp = +new Date,
           Oc.setItem(this.uploadStorageKey, JSON.stringify(n)));   // 缓存
    }, "json")
}
// 缓存有效期判断：+new Date - n.timestamp < 6e4   （60 秒，与响应里的 expire:60 一致）
```

**结论（Phase 4 必须照此实现）**：

```
① GET https://d.pcs.baidu.com/rest/2.0/pcs/file?method=locateupload
   → { host:"c.pcs.baidu.com", server:["<上传节点>","c7.pcs.baidu.com"],
       bak_server:[...], quic_server:[...], prov, isp, expire:60 }
② 上传域名 = server[0]         ← 不是 host，不是 bak_server
③ 缓存 60 秒（expire），过期后重新获取
④ 失败回退：换用 server[1..n]
```

> ⚠️ **本文档此前给出的「保留 `bddwd-cm01` 为默认值」的建议是错的**，已作废。
> 该域名在 1 小时内就变成了 `bdbl-cm01`，硬编码必然在某个时间点失效。
> 另注：`_har/verify_pcs_cookie.py` 中仍硬编码 `bddwd-cm01`（当时可用），
> 后续应改为先调 `locateupload`。
>
> ✅ `locateupload` **不需要认证**（Phase 1.6 实测：不带任何 Cookie 也返回 `error_code:0`），
> 因此可以在登录前就调用。

### 5.3 上传器 JS 证据（从 HAR 中提取）

`nd-static.bdstatic.com/m-static/v20-main/home/js/chunk-vendors.5e633232.js` 中的 `preCreate` / `create` / `rapidUpload` 实现确认：

```js
// preCreate
{ path, autoinit: 1, block_list: JSON.stringify(blockList), target_path }
// 成功后
e.uploadId   = i.uploadid
e.uploadSign = i.uploadsign
// create
{ path, size, uploadid, block_list } (+ target_path, 可选 uploadsign)
```

并观察到 `_options.no_report_upload` 之类的开关，说明 Web 端存在多条上传路径。上传器资源路径为 `.../h5Uploader/_nomd5_nomod/file.js`（**`_nomd5_nomod`** = 不计算 MD5、不修改文件）。

### 5.4 ✅ MD5 双轨制（**已彻底解决**，2026-09-14 Phase 1.6）

> 本节曾标记为「本方案最大的技术未决项」。**现已完全解开** —— 从 HAR 内嵌的上传器 JS 中
> 提取到了官方 `_encryptMd5` 实现，并用真实 ground truth 逐位验证通过。

#### 5.4.1 结论

百度对 MD5 使用**两套表示**，各有明确用途：

| 表示 | 用途 | 出现在 | 形态 |
|---|---|---|---|
| **纯净 MD5** | 上传协议 | `precreate.block_list`、`superfile2` 响应 `md5`、`create` 请求 `block_list` | 标准 32 位小写 hex |
| **加密 MD5**（展示态） | 用户可见 | `/api/list`、`/api/filemetas`、`/api/create` 响应、缩略图 URL、**`rapidupload` 请求的 `content-md5`/`slice-md5`** | 第 9 位被替换为 `g`..`v` |

#### 5.4.2 官方算法（从 JS 提取，`chunk-vendors.js` 与 `aipan-tools.js` 两处一致）

```js
_encryptMd5 = function(e) {
    if (e.length != 32) return e;
    for (var t = 0, n = e.length; t < n; t++)
        if (!(e[t] >= 0 && e[t] <= 9 || e[t] >= "a" && e[t] <= "f")) return e;   // 非纯小写hex → 原样返回
    for (var i = e.substr(8,8) + e.substr(0,8) + e.substr(24,8) + e.substr(16,8),  // ① 4×8 字符重排
             r = "", o = 0, s = i.length; o < s; o++)
        r += (parseInt(i[o], 16) ^ 15 & o).toString(16);                            // ② 逐位 XOR (o % 16)
    var a = String.fromCharCode("g".charCodeAt() + parseInt(r[9], 16));             // ③ 第9位 → chr('g'+值)
    return r.substr(0,9) + a + r.substr(10);
}
```

**Python 参考实现**（Phase 4 应移入 `核心/上传/哈希计算器.py`）：

```python
import re

def 加密md5(e: str) -> str:
    """官方 _encryptMd5：纯净 md5 → 展示态 md5。非纯小写 hex 输入原样返回。"""
    if len(e) != 32 or not re.fullmatch(r"[0-9a-f]{32}", e):
        return e
    i = e[8:16] + e[0:8] + e[24:32] + e[16:24]          # ① 4×8 重排
    r = "".join(format(int(i[o], 16) ^ (15 & o), "x") for o in range(32))  # ② XOR (o%16)
    return r[:9] + chr(ord("g") + int(r[9], 16)) + r[10:]                   # ③ 第9位

def 解密md5(c: str) -> str:
    """逆运算（①②自逆，仅第9位需还原）。"""
    ch = c[9]
    h = format(ord(ch) - ord("g"), "x") if "g" <= ch <= "v" else ch
    s = c[:9] + h + c[10:]
    i = "".join(format(int(s[o], 16) ^ (15 & o), "x") for o in range(32))
    return i[8:16] + i[0:8] + i[24:32] + i[16:24]
```

**这解释了此前观测到的全部现象**：

| 此前的观测 | 现在的解释 |
|---|---|
| 非 hex 字符**只在第 9 位** | 只有第 9 位被替换为 `chr('g'+值)` |
| 字符集合恰为 `g`..`v`（16 个字母 = 4 bit） | 值域 0..15 → `g`+0 .. `g`+15 = `g`..`v` |
| 与其它 31 位**无任何校验和关系** | 它是 `XOR` 后的 `r[9]` 的函数，而 `r` 已混合全部输入位 |
| 83 个样本中 40 个「干净」、43 个「异常」 | **来源不同**：40 个来自 `superfile2` 响应（纯净），43 个来自展示态字段（加密） |
| 「同分片文件 superfile2 与 create 响应 md5 逐位全不同」 | 两者本就是**不同表示**（纯净 vs 加密），非同一算法的差异 |

#### 5.4.3 决定性验证（三种独立证据）

**证据 A — HAR 自洽**：对同一文件，`rapidupload.content-md5` 与 `create` 响应 `md5` 各自解密后应相等。

**证据 B — 真实 ground truth（最强）**：本地生成内容已知的文件，实际上传后比对：

```
本地纯净 md5        = 86a750a076a37339776556582ad8cf99
加密md5(本地纯净)   = 7780365e0v0c9d4f2bfb8afefece9bb7
create 实际返回 md5 = 7780365e0v0c9d4f2bfb8afefece9bb7
★ 逐位相等          = True          ← 算法与官方实现完全一致
```

**证据 C — round-trip 自洽**：`解密md5(加密md5(x)) == x` 对多组输入成立（含全 0 / 全 f 边界）。

#### 5.4.4 各字段该用哪种表示（Phase 4 实现要点）

| 字段 | 用哪种 | 说明 |
|---|---|---|
| `precreate.block_list[i]` | **纯净** | `md5(第 i 片)`；✅ 实测 precreate 不校验其正确性 |
| `superfile2` 响应 `md5` | **纯净** | 服务端返回，直接用于下一行 |
| `create.block_list[i]` | **纯净** | 用 `superfile2` 返回的 md5 列表 |
| `rapidupload.content-md5` | **加密** | `加密md5(md5(整文件))` |
| `rapidupload.slice-md5` | **加密** | `加密md5(md5(首 256 KB))` |
| 界面显示 md5 | **加密** | 即 `/api/list` 原样返回的值；若要显示纯净值需 `解密md5()` |

> ⚠️ **单分片文件的 `block_list` 陷阱（实测踩坑）**：
> 对于单分片文件，`block_list[0]` 必须是 **`md5(整文件)`**，**不是** `md5(首256KB)`。
> 我曾误传首 256KB 的 md5，导致 `precreate`/`superfile2` 均成功但 **`create` 报错**。
> `slice-md5`（首 256 KB）**只用于 `rapidupload`**，两者不可混用。

---

## 6. 其余业务接口对照

### 6.1 文件浏览

```
GET /api/list?dir=/&order=time&desc=1&num=100&page=1
    &clienttype=0&app_id=250528&web=1&dp-logid=...
→ { errno:0, list:[{fs_id, path, server_filename, isdir, size, md5, category,
                    server_mtime, server_ctime, ...}], request_id, guid }

GET  /api/filemetas?target=["/"]&dlink=0&text=1&...   # target 是 JSON 数组字符串
POST /api/filemetas?clienttype=0&app_id=250528&web=1&channel=chunlei&version=<ms>
     body: target=["/Screenshots"]                     # POST 形态，用于批量取元信息
→ { errno:0, info:[{...同上..., dlink?}], request_id }

GET /api/categorylist?...&order=time&desc=1&num=100&page=1&category=6
→ { errno:0, info:[{...}], guid_info, guid, request_id }      # 注意返回键是 info 不是 list
```

> ✅ **修正（Phase 5 前置探测，2026-09-14）**：本文档早先写「`dlink=1` 推测已证伪」——
> **那个结论是错的**。当时只依据「两次 HAR 未出现 `dlink=1`」就下了判断，属**把「没抓到」当成「不存在」**。
> 真机实测：**`/api/filemetas?dlink=1` 完全可用，且返回的 dlink 与 `/api/download` 同构**。
> 这反而是绕开客户端签名难题的关键路线，详见 §6.6 与 R10。
>
> ✅ `category` 取值参考：`6` = 其它（实测返回 100 条 `info`）。其余分类号待补。

### 6.2 文件管理（删除/重命名/移动/复制）✅ 已补全

全部走同一个端点 `POST /api/filemanager`，靠 **query 的 `opera` 参数**区分操作。
**关键差异：`filelist` 的 JSON 结构随 `opera` 变化**——`delete` 传字符串数组，其余传对象数组。

```
① 重命名  opera=rename
POST /api/filemanager?async=2&onnest=fail&opera=rename&bdstoken=...
     &clienttype=0&app_id=250528&web=1&dp-logid=...
body: filelist=[{"id":411921386376817,"path":"/IMG_20260904_150754.jpg","newname":"威海看海.jpg"}]
       └─ 三个键：id(fs_id) / path(原路径) / newname(新文件名，仅名字不含路径)
→ { errno:0, info:[], taskid:873975867891608, request_id }

② 移动    opera=move
body: filelist=[{"path":"/威海看海.jpg","dest":"/我的资源","newname":"威海看海.jpg"}]
       └─ 三个键：path / dest(目标目录) / newname；✅ 注意 move **不带 id**
→ { errno:0, info:[], taskid:555234091881418, request_id }

③ 复制    opera=copy
body: filelist=[{"path":"/类似的网站_v7.1.3.crx","dest":"/Screenshots","newname":"类似的网站_v7.1.3.crx"}]
       └─ 与 move 结构完全一致
→ { errno:0, info:[], taskid:715499143739261, request_id }

④ 删除    opera=delete        ← 结构不同！
POST /api/filemanager?async=2&onnest=fail&opera=delete&bdstoken=...&newVerify=1&...
body: filelist=["/Screenshots/Screenshot_2026-09-14_01-29-12.png"]
       └─ ✅ **纯字符串数组（路径）**，不是对象数组
→ { errno:0, info:[], taskid:1096981316399599, request_id }

# 异步任务轮询（所有 opera 共用）
GET /share/taskquery?taskid=<taskid>&clienttype=0&app_id=250528&web=1&dp-logid=...
→ 第 1 次: { errno:0, status:"running", progress:1,  task_errno:0, request_id }
→ 第 2 次: { errno:0, status:"success", list:[...], total:1, task_errno:0, request_id }
   └─ 实测轮询间隔约 5s，2 次即 success
```

**`opera` 取值汇总（实测确认 4 个）**：`rename` / `move` / `copy` / `delete`。
`restore` / `deleteforever` 不在 `filemanager` 上——回收站的还原与彻底删除是**独立端点**，见 §6.3。

### 6.3 回收站 ✅ 已补全

```
① 列表
GET /api/recycle/list/?num=100&page=1&clienttype=0&app_id=250528&web=1&dp-logid=...
→ { errno:0, list:[{...}], timestamp:1789327529, request_id }

② 还原
POST /api/recycle/restore?channel=chunlei&async=1&bdstoken=...&clienttype=0&app_id=250528&web=1&dp-logid=...
body: fidlist=[584299975796218]          # fid 数组（回收站条目的 fs_id）
→ { errno:0, faillist:[], taskid:0, request_id }
   └─ ⚠️ 注意返回 **`faillist`**（失败列表）而非 `info`；`taskid:0` 表示同步完成，无需轮询

③ 彻底删除
POST /api/recycle/delete?channel=chunlei&async=1&bdstoken=...&clienttype=0&app_id=250528&web=1&dp-logid=...
body: fidlist=[566873365733941]
→ 首次: { errno:132, err_msg:"", authwidget:{...}, verify_scene:2, request_id }   ← ⚠️ 风控拦截！
→ 验证后重试: { errno:0, err_msg:"", request_id }
```

> 🔴 **重要风控发现：彻底删除回收站文件会触发短信二次验证。**
> 实测第一次 `POST /api/recycle/delete` 返回 **`errno:132`** + `verify_scene:2`，随后前端走了一整套验证流程：
>
> ```
> GET passport.baidu.com/v2/sapi/authwidgetverify
>       ?action=getapi&authtoken=<32字符>&bankcheck=1&apiver=v3&lang=zh-CN&jsonp=1
>    → { errno:'110000', ppAuthName:'recyclebin', bdToken:..., ppEncryptuid:... }
>      └─ ppAuthName:'recyclebin' 表明这是「回收站操作」专属的验证场景
> GET passport.baidu.com/v3/api/conf/getupsms        → 短信通道配置
> GET passport.baidu.com/v2/sapi/authwidgetverify
>       ?type=mobile&action=send&authtoken=<同一个>
>    → 发送短信验证码
> GET passport.baidu.com/v2/sapi/authwidgetverify
>       ?type=mobile&action=check&vcode=<6位>&authtoken=<同一个>&secret=
>    → { errno:'110000', authsid:'', secret:'', traceid:'' }
> ```
>
> **对实现的影响**：`整体删除`（清空回收站 / 彻底删除）在 Phase 3 **无法自动化**，必须保留人工介入路径。
> 建议：UI 上对「彻底删除」给出明确提示，并在遇到 `errno:132` 时提示用户去网页端完成验证，而不是尝试自动绕过。
> `errno:132` 应作为一个独立的错误类型在错误模型里建模。

### 6.4 分享 ✅ 已补全

```
① 创建分享（已在 §6.4 原文，见下）
POST /share/pset?channel=chunlei&bdstoken=...&clienttype=0&app_id=250528&web=1&dp-logid=...
body: is_knowledge=0, public=0, period=0|<天数>, pwd=<4位>,
      eflag_disable=true, linkOrQrcode=link, channel_list=[], schannel=4,
      fid_list=[933791872893556]          # fs_id 列表
→ { errno:0, link:"https://pan.baidu.com/s/1LaFlYKlcychx64Na_dmrnQ",
    shorturl:"...", shareid:45163615061, pwd:"xxxx", ctime, expiredType, expiretime,
    qrcodeurl:"https://pan.baidu.com/api/wechat/qrcode?shorturl=...&pwd=...",
    createsharetips_ldlj:"复制这段内容后打开百度网盘手机App，操作更方便哦" }

② 分享列表
GET /share/record?channel=chunlei&clienttype=0&app_id=250528&web=1&dp-logid=...
     &num=100&page=1&order=ctime&desc=1&is_batch=1
→ { errno:0, count:25, list:[{...}], nextpage:0, newno:"",
    createsharetips_ldlj:"...", show_msg:"", request_id }
   └─ ✅ `count` 是总数，`nextpage` = 0 表示没有下一页（分页终止条件）
   └─ 实测 page=1 返回 25 条，page=2 返回 0 条

③ 取消分享
POST /share/cancel?channel=chunlei&bdstoken=...&clienttype=0&app_id=250528&web=1&dp-logid=...
body: shareid_list=[49310074036]          # shareid 数组（注意不是 fs_id）
→ { errno:0, newno:"", show_msg:"", request_id }
   └─ ✅ 实测取消后 `share/record` 的 count 由 25 降到 20

④ 查询分享的短链与提取码
GET /share/surlinfoinrecord?channel=chunlei&clienttype=0&app_id=250528&web=1&dp-logid=...
     &shareid=2826424237&sign=<hex32>&bdstoken=...
→ { errno:0, shorturl:"hqUiutm", pwd:"x", isElink:0, newno:"", show_msg:"", request_id }
   └─ ⚠️ 需要 `sign`（hex32）参数，来源未知，待查

⑤ 共享空间分享记录（企业/共享空间场景）
GET /mid_mucw/share/record?app_id=118511220&clienttype=0&channel=chunlei&dp-logid=...
     &page=1&num=30&web=1&order=ctime&desc=1&is_batch=1&sharedSpaceId=2000027878985
→ { errno:0, count:0, list:[], nextpage:0, ... }
   └─ 注意 `app_id=118511220`（与个人版的 250528 不同）

⑥ ✅ 转存外链文件（Phase 1.6 补充 HAR 实测）
POST /share/transfer?shareid=29011479432&from=1101796850381
     &sekey=<44字符，含 + 号的 base64>&ondup=newcopy&async=1
     &channel=chunlei&web=1&app_id=250528&bdstoken=...
     &logid=<base64>&clienttype=0&dp-logid=...
Content-Type: application/x-www-form-urlencoded
body: fsidlist=["1089586921933862"]      # JSON 数组字符串，分享内的 fs_id
      path=/模拟器游戏/全机种              # 目标目录（自己网盘里的路径）
→ { errno:0, newno:"", show_msg:"文件转存中",
    task_id:"538472814613766", request_id:... }
   └─ ⚠️ 返回的是 **`task_id`（字符串）**，不是 `taskid`（数字）！

⑦ 转存任务轮询（与 §6.2 同一端点，但参数名不同！）
GET /share/taskquery?taskid=538472814613766&channel=chunlei&web=1&app_id=250528
     &bdstoken=...&logid=...&clienttype=0&dp-logid=...
→ { errno:0, task_errno:0, status:"running", progress:10 }
→ { errno:0, task_errno:0, status:"success", list:[...], total:1 }
   └─ 实测 3 秒内完成；轮询间隔约 2.6s

⑧ 分享订阅（转存成功后前端自动调用，非必需）
POST /share/subscribe?method=query&channel=chunlei&web=1&app_id=250528&bdstoken=...&logid=...
body: list=[{"uk":1101796850381,"share_id":29011479432}]
→ { data:{...}, errno:0, newno:"", show_msg:"", request_id }

POST /share/subscribe?method=add&...
body: list=[{"uk":1101796850381,"share_id":29011479432}]
→ { data:[...], errno:0, newno:"", show_msg:"", request_id }

⑨ 分享挂载信息（转存后前端调用，非必需）
POST /share/mountsinfo?time=1&rand=1&version=1&channel=chunlei&web=1&app_id=250528&bdstoken=...&logid=...
body: share_uk=1101796850381
      share_id=29011479432
→ { errno:0, mbox_info:null, newno:"", server_time:1789329169, show_msg:"", request_id }
```

**`/share/transfer` 参数说明**：

| 参数 | 位置 | 值来源 | 说明 |
|---|---|---|---|
| `shareid` | query | 分享页 | 分享 ID（数字） |
| `from` | query | 分享页 | **分享者的 uk**（不是自己的） |
| `sekey` | query | ⚠️ **来源未知** | 44 字符 base64（含 `+`）。详见下方缺口说明 |
| `ondup` | query | 固定 | `newcopy` = 重名时新建副本 |
| `async` | query | 固定 | `1` = 异步，返回 `task_id` |
| `logid` | query | 客户端生成 | base64(`<32位hex>:FG=1`)，**贯穿 transfer/taskquery/subscribe/mountsinfo 共用同一个** |
| `fsidlist` | body | 分享文件列表 | JSON 数组字符串 |
| `path` | body | 用户选择 | 目标目录（自己的路径） |

#### ✅ `sekey` 来源已解明（Q13，2026-09-14）

**结论：`sekey` 是 `/share/verify` 响应里的 `randsk`，且必须 URL 解码。**

```
POST /share/verify?surl=<短码>&bioc=1&t=<毫秒>&channel=chunlei&web=1
     &app_id=250528&clienttype=0&bdstoken=...
body: pwd=<提取码>&vcode=&vcode_str=
→ { errno:0, err_msg:"", request_id:..., randsk:"Tiu6lMaFeDt4fwewkmmU0hKE%2ByMvPYSJn5ckIWQHwXo%3D" }
   sekey = unquote(randsk)        ← 44 字符，含 + 和 =
   同时 Set-Cookie: BDCLND=<randsk>; path=/; domain=pan.baidu.com
```

**它在常规响应体里不叫 `sekey`** —— 这正是此前两次 HAR 全文检索 `sekey` 均无收获的原因。

**🔑 第二条关键发现：`BDCLND` cookie 是必需的**

`/share/list` 与 `/share/transfer` **依赖 `/share/verify` 种下的 `BDCLND` cookie**：

| 请求 | 不带 BDCLND | 带 BDCLND |
|---|---|---|
| `GET /share/list` | ❌ `errno:-9` | ✅ `errno:0`（返回 share_id / uk / list） |

⚠️ **顺序不能反**：必须 **verify → list → transfer**。

⚠️ **本项目需手动携带**：`网络客户端` 为注入会话 Cookie 会设置显式的 `cookie` 请求头，
这会**覆盖 httpx 自带的 cookie jar**，导致 `Set-Cookie` 种下的 `BDCLND` 不会被自动带上。
`分享接口._分享请求头()` 已显式拼接它。

**JS 侧佐证**（`preload-helper.bj1169hv.js`）：
```js
fg = e => { document.cookie = "BDCLND=" + e.replace(/[;\r\n]/g,"") + "; path=/" }
__ = (e,t,n,r) => { Po("local", e+t+"_bdclnd", n); Po("local", e+t+"_pwd", r); fg(n) }
```
——`BDCLND` 的值就是 sekey，同时写进 localStorage 的 `<key>_bdclnd`。
这正对应分享页内联脚本的注释「按 path/cookie/storage 兜底读取」。

**另外两个参数由 `/share/list` 一次性提供，无需解析分享页 HTML**：
```
GET /share/list?shorturl=<短码>&page=1&num=20&root=1&web=1&app_id=250528&channel=chunlei&...
→ { errno:0, share_id:22000728871, uk:1101796850381, list:[{fs_id,path,isdir,...}] }
   transfer 的 shareid = share_id ；transfer 的 from = uk
```

**完整转存链路（4 步，全部实测走通）**
```
① POST /share/verify   → randsk（URL 解码 = sekey）+ 种下 BDCLND
② GET  /share/list     → share_id + uk + 文件清单          （需 BDCLND）
③ POST /share/transfer → task_id（字符串）                  （需 BDCLND + sekey）
④ GET  /share/taskquery?taskid=...  → running → success
```

其余分享功能已全部覆盖。

### 6.5 用户 / 会话

```
GET /api/gettemplatevariable?fields=["bdstoken","token","uk","isdocuser","servertime"]&...
→ { errno:0, result:{ bdstoken:"<32hex>", token:"<32hex>", uk:455281002,
                      isdocuser:1, servertime:1789325407 } }

GET /api/user/getinfo?need_boardinfo=1&user_list=[<uk>]&...
→ { errno:0, records:[{ uk, uname, avatar_url, vip_type:2, vip_level:6,
                        priority_name:"x89****841", ... }] }
   ⚠️ **必需参数（Phase 1 实测补充）**，缺任一个返回 `errmsg:"params error"`：
        need_boardinfo = 1
        user_list      = [<uk>]     # JSON 数组，uk 来自会话

GET /api/quota?...
→ { errno:0, total:13201655726080, used:9733275771364, free:0,
    expire:false, recyclestatus:0, server_time:1789325408 }
```

**`/api/gettemplatevariable` 是会话引导入口** —— 登录后第一件事就是取 `bdstoken` + `uk`。

> 📌 **Phase 1 实测补充：`/api/loginStatus` 的公共参数与业务接口不同**
> ```
> GET /api/loginStatus?clienttype=1&channel=web&version=0&app_id=250528&web=1
> → { errno:0, login_info:{ bdstoken, photo_url, sec_mobile, svip10_id,
>                           uk, uk_str, username, vip_identity }, ... }
> ```
> 注意 `clienttype=1`（业务接口是 `0`）、`channel=web`（业务接口是 `chunlei`）。

### 6.6 搜索 / 下载 / 离线下载 ✅ 已补全

#### 搜索

```
GET /api/search?clienttype=0&app_id=250528&web=1&dp-logid=...
     &order=time&desc=1&num=100&page=1&recursion=1&key=<关键词>
→ { errno:0, list:[{...}], has_more:0, display_count:0, need_ai_search:false, request_id }
   └─ `recursion=1` = 递归搜索子目录（实测返回 275 条）
   └─ `has_more` 是分页终止条件；`need_ai_search` 提示是否走 AI 搜索

GET /api/categorylist?...&order=time&desc=1&num=100&page=1&category=6
→ { errno:0, info:[...], guid_info:"", guid:0, request_id }
```

> `/aipan/search`、`/aisearch/conversation/tasklist` 属于 **AI 搜索（云一朵）**，与普通文件搜索是两套体系，
> 本项目**不实现** AI 搜索。

#### 下载直链（★ 完整三级链路，已实测）

```
① 取直链
GET /api/download?fidlist=[1091211382042161]&type=dlink&vip=2&sign=<56字符>&timestamp=<10位秒>
    &clienttype=0&app_id=250528&web=1&dp-logid=...
→ { errno:0, dlink:["https://d.pcs.baidu.com/file/<md5>?fid=...&rt=pr&sign=...&expires=8h&..."],
    request_id }

② 一级跳转（d.pcs.baidu.com）
GET https://d.pcs.baidu.com/file/<md5>
    ?fid=455281002-250528-1091211382042161&rt=pr&sign=<70字符>&expires=8h
    &chkv=1&chkbd=1&chkpc=&dp-logid=...&dstime=...&r=...&vip=2&response-cache-control=private
→ HTTP 302  Location: https://bdbl-cm01.baidupcs.com/file/<md5>?bkt=...&...

③ 二级落地（下载 CDN，真正的文件流）
GET https://bdbl-cm01.baidupcs.com/file/<md5>
    ?bkt=en-2bd419aa17f4904f971a37d47730b9167d0a5&fid=...&time=...&sign=<82字符>
    &to=401&size=46915493&sta_dx=46915493&sta_cs=0&sta_ft=zip&...
    &vuk=455281002&pkey=en-...&expires=8h&fin=...&fn=...&by=themis&ccn=CN
→ HTTP 200（文件字节流，实测 size=46915493 ≈ 46.9 MB）
```

> 注意 ② 的 `fid` 格式是 `uk-appid-fs_id` 三段拼接（`455281002-250528-1091211382042161`）。

**✅ 首选路线（免签名，Phase 5 直接可用）**

2026-09-14 真机探测确认，**不需要破解任何签名算法**：

```
① GET /api/filemetas?target=["<路径>"]&dlink=1&text=1
   → { errno:0, info:[{ ..., dlink:"https://d.pcs.baidu.com/file/<md5>?fid=...&sign=...&expires=8h&..." }] }
   服务端**已经签好**了 dlink，客户端无需自己算 sign

② GET <dlink>
   请求头必须带： User-Agent + Referer:https://pan.baidu.com/ + Cookie(BDUSS+BDUSS_BFESS+STOKEN)
   → 302 → bdbl-cm01.baidupcs.com → HTTP 200/206 字节流
   支持 Range（实测 bytes=0-1023 → HTTP 206，收到 1024 字节）
```

**⚠️ dlink 必须带 Cookie**：实测不带 Cookie 会返回
`HTTP 403 {"error_code":31045,"error_msg":"user not exists"}`，
且**不会** 302 跳转到 CDN。这一点与上传通道不同（上传的 superfile2 只需 Cookie 但不做二次校验）。

**🔴 已废弃的路线：`/api/download` 需要客户端计算 `sign`**

| 事实 | 值 |
|---|---|
| 参数 | `sign`（56 字符，标准 base64 字符集，含 `+` `/` `=`）+ `timestamp`（10 位秒级） |
| 唯一值数 | 2 个样本 **2 个不同值** → **逐请求变化** |
| 是否在响应体中出现 | **否**（两次 HAR 全文检索，无任何响应体包含它） |
| 是否等于 bdstoken | **否**（`bdstoken` 全 HAR 唯一，与此 sign 无交集） |
| 结论 | **客户端生成，算法未知** |

`56` 个 base64 字符 ≈ 41 字节，不符合常见哈希长度（MD5=32 hex / SHA1=40 hex / SHA256=44 b64），
疑似 RSA 加密或自定义编码。**这把下载功能卡住了**——详见 R10。

**缓解方向（Phase 5 待验证）**：
1. 先测 **`/api/download` 省略 `sign`/`timestamp` 是否可用**（可能只是可选的风控标记）；
2. 测能否用 `/api/filemetas` 拿到可用直链（但两次 HAR 均未见 `dlink=1`，希望不大）；
3. 若均不可行，退化为「导出直链由浏览器完成」或直接调用 ② 的 `d.pcs.baidu.com` 链路（若其 `sign` 可由 ① 免签名获取）。

| 其它下载相关 | 端点 | 样本量 |
|---|---|---|
| 接入点发现 | `GET d.pcs.baidu.com/rest/2.0/pcs/file?method=locateupload` | ✅ 5 条 |
| 离线下载 | `/rest/2.0/services/cloud_dl` | ⚠️ 仅 3 条，不足 |
| 缩略图 | `https://thumbnail0.baidupcs.com/thumbnail/<md5>?fid=<uk-appid-fsid>&rt=pr&sign=<68字符>&expires=8h&chkbd=0&chkv=0` | ✅ 165 条 |

> **`locateupload` 不校验认证**：实测**不带任何 Cookie** 也返回 `error_code:0`（仅 `client_ip` 变为 `117.173.0.0`）。
> 因此它**不能**用作会话有效性探针。返回内容为地域路由信息：
> `host: c.pcs.baidu.com`、`server: ['xafj-cm11.pcs.baidu.com','c7.pcs.baidu.com']`、
> `bak_server: ['c.pcs.baidu.com']`、`quic_server: ['https://c3.pcs.baidu.com']`、`prov: sichuan`、`isp: cmnet`、`expire: 60`。

---

## 7. 分阶段实施步骤

### Phase 0 — 前置准备

| # | 任务 | 状态 |
|---|---|---|
| 0.1 | ~~补齐 Cookie 抓包~~ | ✅ **已完成**（`_har/cookies.txt`，34 个 cookie） |
| 0.2 | ~~确认最小 Cookie 集~~ | ✅ **已完成**：`BDUSS` + `STOKEN`，详见 §4.6 |
| 0.3 | ~~补抓缺失接口~~ | ✅ **已完成**。共 4 份 HAR：完整操作（1302）/ 补充操作（866）/ **上传操作（86）** / **转存操作（42）**。§6.2/§6.3/§6.4/§6.6 已全部补齐，Q4/Q5/转存均获解答。**剩余唯一缺口：`/share/transfer` 的 `sekey` 来源**（见 Q13） |
| 0.4 | ~~修正 `项目配置.toml`~~ | ✅ **已完成**（commit `bf2f5ef`）：`name=baidu-adapter`、`requires-python=">=3.10,<3.13"`、删除失效的 `[project.scripts]`、移除阿里云 `oss2`、`qrcode` 补 `[pil]` |
| 0.5 | **补抓上传操作用于复核 Q4/Q5** | ⬜ **待做** —— 见下方说明 |

> ⚠️ **补充 HAR 中没有任何上传操作。** 已用关键词全文核验：`precreate` / `superfile2` / `/api/create` /
> `rapidupload` / `uploadid` / `partseq` / `uploadsign` / `bddwd` **全部 0 命中**；域名列表中也无上传通道。
> 文件 `bdbl-cm01.baidupcs.com` 出现 1 次，但它是**下载 CDN**（`/file/<md5>`），不是上传通道。
> 因此 **Q4（分片大小是否固定 4 MB）与 Q5（上传域名是否变化）仍未获解答**。
> 不过 Q1c 已用**主动探测**的方式独立验证（见 §5.2），不依赖该 HAR。

**验收**：✅ **已达成** —— `_har/verify_cookie.py` 用 `BDUSS + STOKEN` 成功调用 `/api/gettemplatevariable` 拿到 `errno:0`、`uk` 与 32 位 `bdstoken`。

> 运行环境：系统 Python 3.14.7 无 pip 且为 externally-managed，已在项目内建 `.venv/`（已 gitignore）并安装 `httpx 0.28.1`。
> 复现：`.venv/bin/python _har/verify_cookie.py`

### Phase 1 — 网络层 + 会话（**含关键技术 spike**）

| # | 任务 | 状态 |
|---|---|---|
| 1.1 | 重写 `核心/网络/网络客户端.py`：①Cookie 会话注入；②公共 query 参数自动注入（`clienttype`/`app_id`/`web`/`channel`/`dp-logid`）；③`bdstoken` 按需注入；④错误模型改为 `errno`/`errmsg`；⑤**注入 `获取会话` 回调，每请求实时取 `bdstoken`**（修复光鸭的令牌不回流缺陷） | ✅ **已完成** |
| 1.2 | 新写 `核心/网络/dp_logid.py`：16 位前缀 + 4 位自增序号（含线程安全 + `logid` 生成） | ✅ **已完成** |
| 1.3 | 新写 `核心/认证/会话仓库.py`：`数据/会话.json` 的读写；BDUSS 导入/导出/清除 | ✅ **已完成** |
| 1.4 | 重写 `核心/认证/登录服务.py`：二维码四步 + `plantcookie` 子域种植 + 手工 BDUSS 导入兜底 | ✅ **已完成**（扫码链路**未经真机验证**，见下） |
| 1.5 | 重写 `核心/认证/认证服务.py`：`/api/loginStatus` 会话自检 + `/api/gettemplatevariable` 取 bdstoken/uk + `/api/user/getinfo` | ✅ **已完成** |
| 1.6 | **🔬 SPIKE-MD5**：实测 `rapidupload` 命中条件、`slice-md5` 语义、分片大小 | ✅ **已完成 —— 且超额解决 R2/Q3**：从上传器 JS 提取到官方 `_encryptMd5` 并用真实 ground truth 验证一致；秒传实测打通（`errno:0`）；分片确认 4 MB。详见 §5.2.1 与 §5.4 |
| 1.7 | **🔬 SPIKE-上传通道**：确认上传域名是否固定 | ✅ **已完成**：**不固定，必须从 `locateupload` 的 `server[0]` 动态获取**（§5.2.2），上传器 JS 逐字印证，缓存 60s |

**Phase 1.1~1.5 验证结果（2026-09-14，真机）**

冒烟脚本 `_har/verify_phase1.py`（使用临时会话文件，不写入 `数据/会话.json`）：

| 项 | 结果 |
|---|---|
| 全部模块导入（无循环依赖） | ✅ |
| `dp_logid` 20 位格式 / 前缀恒定 / 序号自增 | ✅ |
| `dp_logid` 线程安全（8 线程 × 50 次 = 400 次无重号） | ✅ |
| `logid` = base64(`<14位>.<17位>`) | ✅ |
| 会话仓库解析 `cookies.txt` → 命中 5 个（BAIDUID/BFESS/BDUSS/BFESS/STOKEN） | ✅ |
| 剔除 BAIDUID 后仍有效（符合 §4.6） | ✅ |
| `GET /api/gettemplatevariable` → `uk=455281002`、`bdstoken` 形态 hex32 | ✅ |
| bdstoken/uk 自动回写会话仓库 | ✅ |
| `GET /api/loginStatus` → `login_info` 8 个键 | ✅ |
| `GET /api/user/getinfo` → `uname=x894**841`、`vip_level=6`、`vip_type=2` | ✅ |
| **令牌不回流修复**：清空 bdstoken 后可被刷新器补回 | ✅ |
| 公共参数注入（app_id/clienttype/web/channel/dp-logid） | ✅ |
| Cookie 头含 BDUSS + STOKEN | ✅ |
| `ast.parse` 全部 52 个文件 | ✅ 0 错误 |
| `_har/verify_cookie.py` 回归（最小集仍为 BDUSS+STOKEN） | ✅ 未被破坏 |

> ⚠️ **1.4 扫码链路有两处未验证/待适配**：
> 1. **抓包中 `getqrcode`/`unicast`/`qrbdusslogin` 的响应体未被 Chrome 记录**
>    （只有 Content-Length），字段名系按 passport 公开协议 + 参数形态推断，
>    代码已做 JSONP 与多字段名容错，但**未经真机走通**。
> 2. `界面/弹窗/二维码弹窗.py` 目前是**本地用 qrcode 库生成**二维码，
>    而百度是**服务端下发图片**（base64/imgurl）。契约已改为回传服务端图片，
>    但弹窗渲染逻辑属 Phase 7，**尚未适配** —— 当前会显示错误的二维码。
>
> 兜底路径 `登录服务.手工导入cookie()` 已可用（会话仓库导入逻辑已实测）。

**验收**：`启动.py` 能启动；扫码登录成功；主界面「账号信息页」显示真实 `uname`/`vip_level`/容量。
→ **部分达成**：核心层已实测可用；界面层需 Phase 7 适配后才能启动。

### Phase 2 — 文件浏览（只读）✅ **已完成（2026-09-14）**

| # | 任务 | 状态 |
|---|---|---|
| 2.1 | 重写 `核心/接口/文件接口.py` | ✅ `/api/list`（单页+迭代器+取全部）、`/api/filemetas`（GET+POST）、`/api/categorylist`、`取缩略图URL`；新增 `是目录()` 等解析辅助 |
| 2.2 | 重写 `界面/字段映射.py` | ✅ 按 §3.1 + Q10 全表替换；**判目录改为 `isdir == 1`**；移除对 `核心.接口.云添加接口`/`分享接口` 的依赖（改本地映射） |
| 2.3 | 重写 `界面/页面/文件浏览页.py` | ✅ 列目录 / 双击进子目录 / 面包屑（可点击逐级返回）/ 返回上级 / 分页（上下页 + 每页数量）。**未实现功能改为明确提示 Phase 3~6**，不再调用未实现接口 |
| 2.4 | 解 Q10 字段语义 | ✅ 406 条真实样本交叉分析，结论见 §3.1 补充 |

**验证**（`_har/spike_file_list.py`，真机、最小 Cookie 集）：

| 项 | 结果 |
|---|---|
| `GET /api/list?dir=/&num=10` | ✅ `errno=0`，返回 10 项 |
| 响应键 | `errno/guid/guid_info/list/request_id` —— **无 `total`** |
| 必填字段齐全 | ✅ 本次样本 28 字段，无缺失 |
| 目录 size 恒 0 / category 恒 6 | ✅ 印证「不能用 category 判目录」 |
| 分页无重叠 | ✅ num=5 第 1/2 页 fs_id 完全不同 |
| 自动翻页取全部 | ✅ 根目录共 34 项 |
| `GET /api/filemetas` | ✅ 返回键是 `info`，1 条元信息（33 字段） |
| `POST /api/filemetas` | ✅ 请求 3 条 → 返回 3 条 |
| `GET /api/categorylist` (3/4/6) | ✅ 均 `errno=0`，且返回的都是文件（不含目录） |
| `ast.parse` 全量 | ✅ 53 个文件 0 错误 |

**验收**：✅ 达成 —— 浏览根目录 / 进入子目录 / 面包屑返回 / 分页加载 全部可用。
⚠️ 界面层未做 GUI 实跑（`.venv` 为 Python 3.14 装不了 PySide6，见 Phase 0.4 注）。

### Phase 3 — 文件管理 ✅ **已完成（2026-09-14）**

| # | 任务 | 状态 |
|---|---|---|
| 3.1 | 新写 `核心/接口/管理接口.py`：`filemanager` 的 4 个 opera + 新建文件夹 | ✅ **delete** 传纯字符串数组；**rename** 带 `id`；**move/copy** 带 `dest` 不带 `id`（§6.2 的结构差异已严格实现） |
| 3.2 | 新写 `核心/接口/回收站接口.py`：`/api/recycle/list/`、`/api/recycle/restore` | ✅ 返回 **`faillist`** 而非 `info`；`taskid:0` 表示同步完成 |
| 3.3 | 重写 `核心/接口/任务接口.py`：`/share/taskquery` 轮询 | ✅ `等待任务()` / `等待完成()`；同时检查 `status` 与 `task_errno` |
| 3.4 | **`/api/recycle/delete` 不提供自动化** | ✅ 刻意不实现。模块内附 `彻底删除提示` 文案与 `是风控错误()` 判定，UI 层据此引导用户去网页端 |

### Phase 4 — 上传 ✅ **已完成（2026-09-14）**

| # | 任务 | 状态 |
|---|---|---|
| 4.1 | 重写 `核心/上传/哈希计算器.py` | ✅ 搬入官方 `加密md5()`/`解密md5()`（§5.4.2）；单次读盘算出 整文件 MD5 / 首 256KB MD5 / 4MB 分片 MD5（`计算全部哈希`） |
| 4.2 | 重写 `核心/上传/秒传服务.py` | ✅ 按 §5.2.1 公式算 `data_time`/`data_offset`/`data_content`；`errno:404` 作正常未命中分支 |
| 4.3 | 重写 `核心/上传/分片上传.py` | ✅ `superfile2` multipart，`uploadsign="0"`；**上传域名走 `locateupload` 的 `server[0]`，60s 缓存，失败切 `server[1..n]`**（无任何硬编码） |
| 4.4 | 重写 `核心/接口/上传接口.py` | ✅ 薄封装，委托总调度；保留 `上传文件(路径, 父目录ID, 进度回调)` 旧签名供 `公共线程.py` 调用 |
| 4.5 | 重写 `核心/上传/上传总调度.py` | ✅ 全流程编排（算哈希→秒传→precreate→分片→create）；**光鸭遗留的 `cid = gcid` 赋值错误随 OSS/gcid 体系一并移除** |
| 4.6 | 并发控制 + 断点续传 | ⬜ **未做** —— 当前为顺序上传；分片失败有 3 次重试 + 切换备用域名 |
| 4.7 | 复核 Q4（分片大小） | ✅ 已由 Phase 1.6 + 上传 HAR 两次独立确认 = 4 MB |

**Phase 3+4 联合验收（`_har/spike_phase34.py`，真机）**：

| 步骤 | 结果 |
|---|---|
| 新建目录 A / B | ✅ `errno=0`，均出现在根目录 |
| 上传 1 KB 文件（单分片） | ✅ `errno=0`，fs_id 拿到，服务端可见 |
| 上传 9 MB 文件（3 分片 4M+4M+1M） | ✅ `errno=0`，服务端 size 与本地一致 |
| **秒传同一内容（换名）** | ✅ **`errno:0` 命中，`是否秒传=True`** |
| 重命名 | ✅ 新名出现、旧名消失 |
| 移动 A→B | ✅ B 有、A 无 |
| 复制 B→A | ✅ 副本在 A、源仍在 B |
| 删除 → 回收站可见 | ✅ 回收站匹配到该项 |
| 从回收站还原 | ✅ `faillist` 为空，文件重回 A |
| 动态上传域名 | ✅ 本次解析到 **`xafj-cm11.pcs.baidu.com`**（第三个不同值，见 §5.2.2） |
| `ast.parse` 全量 | ✅ 56 个文件 0 错误 |

**验收**：✅ Phase 3 与 Phase 4 的主要目标全部达成（并发/断点续传除外，见 4.6）。

### Phase 5 — 下载 ✅ **已完成（2026-09-14）**

重写 `核心/下载/下载服务.py`，走 R10 解出的免签名路线：

```
① GET /api/filemetas?target=["<路径>"]&dlink=1&text=1   → info[0].dlink（服务端已签好）
② GET <dlink> + UA + Referer + Cookie + 可选 Range
   → 302 → bdbl-cm01.baidupcs.com → 200/206 字节流
```

| # | 任务 | 状态 |
|---|---|---|
| 5.1 | `取直链(远端路径)` | ✅ `filemetas?dlink=1`，响应键是 **`info`** |
| 5.2 | `下载到文件(路径, 保存路径, 进度回调, 断点续传)` | ✅ 保持光鸭旧调用形态；`Range: bytes=<已下载>-` 续传，服务端不支持 Range（返回 200）时自动退化为从头下载 |
| 5.3 | `下载到目录(路径, 保存目录, 进度回调)` | ✅ **文件名自动去重**：已存在则加 ` (1)` / ` (2)`，**绝不覆盖** |
| 5.4 | 流式落盘 + 进度回调 | ✅ 64 KB 分块，`回调(已下载, 总大小)`；总大小从 `Content-Range` 解析 |
| 5.5 | 分层破口修复 | ✅ `文件浏览页.py` 的「下载选中」按钮与双击文件已接上 `下载服务`。<br>⚠️ **诚实说明**：光鸭骨架里那段「内联 `import httpx` 绕过下载服务」的代码，**已在 Phase 2 重写该页时被整体删除**，本阶段不是"改回来"而是"把占位按钮接上真实服务" |
| 5.6 | Cookie 必需 | ✅ 缺 Cookie → `403 {"error_code":31045,"error_msg":"user not exists"}` |

**Phase 5 真机验收（`_har/spike_phase5.py`）**

| 步骤 | 结果 |
|---|---|
| 造 5 MB 文件并上传 | ✅ `errno=0`，本地 MD5 `513eb64f…` |
| 取免签名直链 | ✅ `d.pcs.baidu.com/file/<md5>?fid=…&sign=…&expires=8h…` |
| 下载并比对 MD5 | ✅ **下载 MD5 == 本地 MD5**，5242880 字节完全一致 |
| 进度回调 | ✅ 调用 80 次，末次 `(5242880, 5242880)` |
| 重复下载去重 | ✅ `x.bin` → `x (1).bin` → `x (2).bin`，三个文件共存、内容均正确 |
| `Range: bytes=0-1048575` | ✅ **HTTP 206**，正好 1048576 字节，`Content-Range: bytes 0-1048575/5242880`，且与原文件前 1 MB **逐字节一致** |
| `Range: bytes=1048576-`（续传） | ✅ HTTP 206，收到 4194304 字节；**前半 + 续传后半 = 原文件（MD5 一致）** |
| 负面对照：不带 Cookie | ✅ 403 `31045 user not exists` |
| 自动清理 | ✅ 网盘文件已删（残留 0）、本地临时目录已清 |
| `ast.parse` 全量 | ✅ 60 个文件 0 错误 |

**验收**：✅ 达成 —— 下载、断点续传、去重、分层接线全部可用。

同时修复光鸭骨架「`文件浏览页.py` 内联 `import httpx` 绕过下载服务」的分层破口。

**验收**：单文件下载、断点续传、下载进度显示。

### Phase 6a — 分享（非转存）✅ **已完成（2026-09-14）**

| # | 任务 | 状态 |
|---|---|---|
| 6a.1 | 重写 `核心/接口/分享接口.py`：创建 `/share/pset` | ✅ ⚠️ **`pwd` 是必需的**（空串报 `pwd length param error`），不传时自动生成 4 位；实测**创建响应不回显 pwd**，模块补了 `返回的提取码` 键 |
| 6a.2 | 分享列表 `/share/record` | ✅ 分页按 `nextpage` 终止；**条目字段名与其它接口不同**，已提供 `取shareid()`/`取提取码()`/`取链接()`/`取文件数()` 适配 |
| 6a.3 | 取消分享 `/share/cancel` | ✅ body `shareid_list=[...]` |
| 6a.4 | 提取码查询 `/share/surlinfoinrecord` | ✅ **实测省略 sign 返回 `errno:2`**（Q12 结论）。模块抛 `提取码需签名`，`支持提取码查询()` 恒为 False，界面层据此标「暂不支持」 |
| 6a.5 | 转存 `/share/transfer` | ✅ **见 Phase 6b**（Q13 已解，已完成） |

**Phase 6a 真机验收（`_har/spike_phase6.py`）**：

| 步骤 | 结果 |
|---|---|
| 上传测试文件 | ✅ `errno=0` |
| 创建分享（带 4 位提取码） | ✅ `errno=0`，拿到 link / shorturl / shareid；提取码与传入一致 |
| 列表能看到 | ✅ 按 `shareId` 匹配到；`passwd=3124` 与创建时一致 |
| 提取码查询（Q12 探测） | ✅ 确认**省略 sign 不可行**（`errno=2`） |
| 取消分享 | ✅ `errno=0` |
| 列表消失 | ✅ `count` 21 → 20，条目不再出现 |
| 自动清理 | ✅ 分享已取消、测试文件已删、根目录残留 0 |
| `ast.parse` 全量 | ✅ 58 个文件 0 错误 |

**⚠️ 分享列表字段名的坑（实测 40 个字段，三组极易混淆）**：

| 列表里的字段 | 容易误写成 | 说明 |
|---|---|---|
| `shareId` | `shareid` / `share_id` | **大写的 I**，对外分享 ID |
| `passwd` | `pwd` | 提取码；`pwd` 只在**创建请求**里 |
| `shortlink` | `shorturl` | `shortlink` 是**完整 URL**；`shorturl` **只是短码**（如 `1LaFlYKlcychx64Na_dmrnQ`），不是 URL |

**验收**：✅ 达成（转存除外，见 6a.5）。

### Phase 6b — 转存 ✅ **已完成（2026-09-14）**

| # | 任务 | 状态 |
|---|---|---|
| 6b.1 | `分享接口.解析分享链接()` | ✅ 支持完整链接 / 带 `?pwd=` / 无 scheme / 裸短码；自动去掉新版链接的 `1` 前缀 |
| 6b.2 | `分享接口.校验分享()` | ✅ `POST /share/verify` → **`randsk` 经 `unquote` 得到 sekey**，并捕获 `BDCLND` cookie |
| 6b.3 | `分享接口.取分享信息()` | ✅ `GET /share/list` → `share_id` + `uk` + 文件清单（**必须带 BDCLND**） |
| 6b.4 | `分享接口.一键转存()` | ✅ 四步编排 + `taskquery` 轮询到 success；支持 `只转存fsid` 与进度回调 |
| 6b.5 | `分享接口._分享请求头()` | ✅ 显式拼接 `BDCLND`（网络客户端的显式 cookie 头会覆盖 httpx jar） |

**Phase 6b 真机验收（`_har/spike_phase6b.py`）**

用 HAR 里那份真实公开分享 `https://pan.baidu.com/s/1_8kHQUxSm8BnpQCWjP7kxg`（提取码 `pxi8`）：

| 步骤 | 结果 |
|---|---|
| 解析分享链接 | ✅ surl 正确去 `1` 前缀；自动提取出 `pwd` |
| `POST /share/verify` | ✅ sekey len=44 且含 `+`/`=`；顺带捕获 BDCLND |
| 负面对照：错误提取码 | ✅ 被正确拒绝（抛 `提取码错误`） |
| `GET /share/list` | ✅ `errno=0`，`share_id=22000728871`、`uk=1101796850381`、1 项 |
| 建目标目录 | ✅ `errno=0` |
| 一键转存 | ✅ transfer `errno=0`，`task_id=230939535486675` |
| 轮询 | ✅ `running` → **`success`** |
| 目标目录验证 | ✅ 出现 `FC原版全集按首字母分类（2736个游戏，无需更新）`，与分享清单一致 |
| 自动清理 | ✅ 目录内文件与目录均已删，根目录残留 0 |
| `ast.parse` 全量 | ✅ 59 个文件 0 错误 |

**验收**：✅ 达成 —— 转存功能完整闭环。

### Phase 7 — 界面适配与收尾 ✅ **已完成（2026-09-14）**

#### A. 死代码与缺陷清理

| # | 任务 | 状态 |
|---|---|---|
| 7.1 | 全量文案替换（`光鸭` / `guangyapan`） | ✅ **0 行残留**（29 个 .py + 项目配置.toml）。含 logger 名、窗口标题、URL、注释、docstring |
| 7.2 | logger 根名 `光鸭云盘` → `百度网盘` | ✅ 全部改为 `百度网盘.*` 分层命名 |
| 7.3 | 删除死代码 | ✅ 删除 `核心/埋点/`、`核心/接口/埋点签名.py`，另发现并删除孤儿模块 `核心/网络/设备身份.py`（Phase 1 重写后已无引用） |
| 7.4 | 修复继承缺陷 | ✅ ①`超时秒` 死参数 → 换成真正生效的 `连接超时秒`；②非 2xx 由原生 `httpx.HTTPStatusError` → 统一 `接口错误`；③错误码白名单语义明确为「默认仅 `(0,)`，放宽需逐次显式声明」；④`格式化工具.py` 反向 import 复核无残留 |
| 7.5 | 硬编码 Tab 索引 → 具名常量 | ✅ 新增 `Tab_文件浏览/Tab_上传记录/Tab_分享/Tab_账号信息/Tab_日志`，替换全部魔法数字 |
| 7.6 | 回收站「彻底删除」提示 | ✅ 按钮改为「彻底删除说明」，点击只弹 `彻底删除提示` 文案引导去网页端；`回收站接口` 刻意不提供自动化方法 |

#### B. GUI 关键适配

| # | 任务 | 状态 |
|---|---|---|
| 7.7 | 二维码弹窗适配 | ✅ 新增 `登录服务.取登录二维码()` → `{qrcode_b64, sign, channel_id, imgurl}`；弹窗改为 **`QPixmap.loadFromData(base64.b64decode(...))`**，**彻底移除对 `qrcode` 库的依赖**（依赖表已删该项）。保留**四态文案**：等待扫码 / 已扫描 / 已确认 / 已过期；新增 `扫码状态` 信号贯通登录线程 → 弹窗 |
| 7.8 | 文件浏览页功能接线 | ✅ **13 个按钮 + 双击全部指向真实接口**（见下表） |

**文件浏览页接线明细**

| 控件 | 指向 |
|---|---|
| 🏠 根目录 / ⬆ 上级 / 面包屑 / 分页 | `文件接口.取文件列表` |
| 🔄 刷新 | 重新拉取当前页 |
| 📁+ 新建文件夹 | `管理接口.新建文件夹`（`/api/create` `isdir=1`） |
| 📤 上传文件 / 📁 上传文件夹 | `上传接口.上传文件/上传文件夹`（含秒传、进度回调） |
| 📥 下载选中 / **双击文件** | `下载服务.下载到目录`（免签名直链 + 去重） |
| 🔗 分享选中 | `分享文件请求` 信号 → 主界面弹对话框 → `分享页.执行创建分享` |
| 🗑 删除选中 | `管理接口.删除` + `任务接口.等待任务` |
| ♻️ 回收站 | `回收站接口.取全部` + 批量 `还原` |
| 🔥 彻底删除说明 | 只弹提示，**不调用接口**（风控） |

#### C. 未适配页面的处置（经确认后执行）

原骨架 6 个页面中，4 个页面 + 2 个弹窗为平台专用代码，阻断启动。处置如下：

| 文件 | 处置 | 理由 |
|---|---|---|
| `界面/页面/分享页.py` | 🔄 **重写** | 底层接口已就绪（Phase 6a/6b） |
| `界面/页面/账号信息页.py` | 🔄 **重写** | 底层接口已就绪（`getinfo` / `quota` / `loginStatus`） |
| `界面/弹窗/分享对话框.py` | 🔄 **重写** | 对应 `/share/pset` |
| `界面/页面/上传记录页.py` | 🔄 **重写为本地记录** | 百度未提供上传记录查询接口；改为读取 `数据/上传记录.json`（由 `上传总调度` 在每次成功上传后追加） |
| `界面/页面/云添加页.py` | ❌ **删除** | 离线下载协议未摸清（仅 3 条样本） |
| `界面/弹窗/短信登录对话框.py` | ❌ **删除** | 百度无短信登录流程；改为「导入 Cookie」入口 |
| `核心/接口/云添加接口.py` / `资产接口.py` / `杂项接口.py` | ❌ **删除** | 平台专用且已无引用 |

**主界面由 6 Tab 精简为 5 Tab**（移除「云添加」）。

#### D. 文档

| # | 任务 | 状态 |
|---|---|---|
| 7.9 | `README.md` | ✅ 新建：安装、**Cookie 获取步骤与最小集**、功能清单、项目结构、已知限制、故障排查、开发注意事项 |
| 7.9 | `项目配置.toml` | ✅ version → 1.0.0；**移除 `qrcode[pil]` 依赖**（改用服务端下发图片）；保留 `requires-python <3.13` 并注明本机 3.14 离屏实测通过 |
| 7.9 | `.gitignore` | ✅ 复核：`数据/`（含会话.json + 上传记录.json）、`_har/`、`.venv/` 均已覆盖 |

#### Phase 7 验收（4 项全过）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | `ast.parse` 全量 | ✅ **52 个文件，0 错误** |
| 2 | `QT_QPA_PLATFORM=offscreen timeout 15 python 启动.py` | ✅ **退出码 124**（事件循环正常） |
| 3 | `grep -rn '光鸭\|guangyapan' 核心/ 界面/ 启动.py 项目配置.toml` | ✅ **0 行** |
| 4 | 凭证脱敏 | ✅ 无明文凭证 |

**附加的深度界面自检**（离屏实例化，非仅启动）：

```
✅ 主窗口实例化成功，标题：百度网盘管理工具
   Tab 数 = 5：[0]📁文件浏览 [1]📤上传记录 [2]🔗分享 [3]👤账号信息 [4]📋调试日志
   文件浏览页 13 个按钮全部存在且已接线
✅ 二维码弹窗：服务端 base64 PNG 解码渲染成功（未使用 qrcode 库）
   状态1 文案 = 请在手机上点击「确认登录」
   超时 文案 = 请关闭本窗口后重新发起扫码登录
✅ 分享对话框：默认提取码 4 位、有效期 5 档
```

> ⚠️ **诚实说明两处偏差**：
> 1. **「分层破口修复」的实际内容与预设不同**：原骨架 `文件浏览页.py` 里那段「内联
>    `import httpx` 绕过下载服务」的代码，**已在 Phase 2 重写该页时被整体删除**。
>    本阶段做的是把 Phase 2 留下的占位按钮**接上真实服务**，而不是「改回来」。
> 2. **GUI 仅验证到离屏模式**：带真实显示服务器的运行未在本机验证
>    （本机只有 Python 3.14，装的是 PySide6 6.11.2 的 `cp310-abi3` 稳定 ABI wheel）。

---

## 8. 风险清单

| ID | 风险 | 等级 | 影响 | 缓解措施 |
|---|---|---|---|---|
| **R1** | ~~HAR 脱敏导致 Cookie 完全缺失~~ → **已解除**。用户已补齐 `_har/cookies.txt`，并经真机实测确定最小集为 `BDUSS` + `STOKEN`（§4.6） | 🟢 已解决 | — | 保留 `_har/verify_cookie.py` 作为会话有效性自检工具；接入 CI 或启动自检 |
| **R2** | ~~MD5 双轨制未解~~ → ✅ **彻底解决（Phase 1.6）**。从 JS 提取到官方 `_encryptMd5` 并实现之，用真实 ground truth 逐位验证通过；秒传已实测打通（`errno:0`）；分片 4 MB 确认 | 🟢 **已解决** | — | 参考实现见 §5.4.2，**Phase 4 移入 `核心/上传/哈希计算器.py`**。务必遵守 §5.4.4 的「用哪种表示」对照表与单分片 `block_list` 陷阱 |
| **R3** | **`bdstoken` 时效性未知**。实测同一会话内 `bdstoken` 恒定（HAR1 85/85；HAR2 唯一值仅 1 种），但何时失效、失效后 `errno` 为何值均未知 | 🟡 中 | 写操作（上传/删除/分享）间歇失败 | 网络层对写操作捕获 `errno` 并自动重取 `bdstoken` 重试一次（Phase 3.1） |
| **R4** | **风控与反爬 — 已确认会实际触发**。2026-09-14 实测：`POST /api/recycle/delete`（彻底删除）首次返回 **`errno:132`** + `verify_scene:2`，强制走短信二次验证（`authwidgetverify`，`ppAuthName:'recyclebin'`） | 🔴 高 | **彻底删除功能无法自动化**；高频操作可能触发更大范围风控 | ①严格复刻 UA/Referer/Origin；②请求频率限制（建议 ≤5 req/s）；③保留 `dp-logid` 等追踪参数；④**绝不实现任何绕过验证码的逻辑**，遇 `errno:132` 明确提示用户去网页端完成验证；⑤把 `errno:132` 建成独立错误类型 |
| **R5** | **上传通道域名是动态的** —— ✅ 已确认并给出解法。同机 1 小时内从 `bddwd-cm01` 变为 `bdbl-cm01` | 🟢 已解决 | 硬编码必然失效 | **必须实现 `locateupload` → `server[0]` 的动态获取 + 60s 缓存 + `server[1..n]` 回退**（§5.2.2）。⚠️ **禁止硬编码任何 `*-cm01.pcs.baidu.com`** |
| **R6** | ~~接口样本不足~~ → **已基本解决**。补充 HAR 已覆盖 `filemanager` 的 4 个 `opera`、回收站 `restore`/`delete`、分享 `record`/`cancel`/`surlinfoinrecord`、搜索、下载三级链路 | 🟢 基本解决<br>🟡 两处缺口 | — | **剩余缺口**：①`/share/transfer` 转存无样本；②**上传操作完全未捕获**（Q4/Q5）。补抓见 Phase 0.5 |
| **R7** | ~~光鸭骨架缺陷被继承~~ → **大部分已修复**。①令牌不回流 → ✅ Phase 1.1 双回调修复；②业务码白名单并集 → ✅ 改为 `errno` 白名单；③`秒传服务.py:96` `KeyError:'gcid'` 与 `上传总调度.py:90` `cid=gcid` → ✅ Phase 4 随 OSS/gcid 体系整体移除；④4 处重复公共请求头 → ⬜ Phase 7.4；⑤非 2xx 抛原生异常 → ⬜ Phase 7.4 | 🟢 大部分已解决 | — | 剩余两项并入 Phase 7.4 |
| **R8** | ~~`项目配置.toml` 元数据矛盾~~ | 🟢 已解决 | — | commit `bf2f5ef` 已修正 |
| **R9** | **合规与使用条款**。逆向 Web 接口可能违反百度网盘用户协议；BDUSS 等价于账号密码，泄露即账号沦陷 | 🟡 中 | 账号风险、法律风险 | ①`数据/会话.json` 已在 `.gitignore` 中；②**严禁**把 BDUSS 写入日志/提交/上报；③仅限个人学习研究用途，不分发 |
| **R10** | ~~`/api/download` 需要客户端计算 `sign`~~ → ✅ **已解决（且无需破解算法）**。真机实测 4 组变体：省略 sign → `errno:2`；sign 空串 → `errno:2`；sign=bdstoken → `errno:113`；**`/api/filemetas?dlink=1` → `errno:0`，直接返回服务端签好的 dlink** | 🟢 **已解决** | — | Phase 5 走 `filemetas?dlink=1`，**完全绕开客户端签名**。注意 dlink 下载必须带 Cookie（否则 403/31045）。探测脚本：`_har/spike_download_sign.py` |

### 8.1 已排除的风险（好消息）

- ✅ **业务接口基本无客户端签名**：两次 HAR 共 2166 条请求中，`pan.baidu.com/api/*` 的**绝大多数**接口零签名，
  认证靠 Cookie + `bdstoken` 即可。**唯一例外是 `/api/download`**（见 R10）。
- ✅ **上传协议已充分验证**：`superfile2` 用最小 Cookie 集直接可用，无需 `plantcookie`，且服务端 md5 = 本地明文 md5。
- ✅ **无 WebSocket 依赖**：`webpush.pan.baidu.com/socket.io/` 仅 2 条，属消息推送，非核心功能。
- ✅ **无 protobuf/自定义序列化**：全部 JSON 或 form-urlencoded。
- ✅ **CORS `OPTIONS` 预检可省略**：全是浏览器行为，Python 客户端直接 POST。
- ✅ **`locateupload` 完全不需要认证**（无 Cookie 也返回 `error_code:0`），可作为纯路由信息接口使用。
- ⚠️ ~~无验证码流程~~ → **已推翻**：彻底删除回收站会触发短信二次验证（`errno:132`），见 R4 / §6.3。

---

## 9. 待验证问题清单

按优先级排序，建议在对应 Phase 开始前解决：

| # | 问题 | 验证方式 | 状态 |
|---|---|---|---|
| ~~Q1~~ | ~~必需 Cookie 的最小集合是什么？~~ | ~~浏览器实测逐项剔除~~ | ✅ **已解决**：`BDUSS` + `STOKEN`（§4.6） |
| ~~Q1c~~ | ~~上传通道（`bddwd-*.pcs.baidu.com`）是否也只需 `BDUSS`+`STOKEN`？~~ | ~~用最小集直接调 `superfile2`~~ | ✅ **已解决（2026-09-14 实测）**：`superfile2` 返回 HTTP 200，**最小集与全量 Cookie 结果完全一致**，无需 `plantcookie`。脚本 `_har/verify_pcs_cookie.py`（不调 `/api/create`） |
| ~~Q7~~ | ~~`filemanager` 各 `opera` 值的确切请求体格式？~~ | ~~补抓 rename/move/copy~~ | ✅ **已解决**：4 个 `opera` 全部实测（§6.2）。关键差异：`delete` 传**字符串数组**，`rename`/`move`/`copy` 传**对象数组** |
| ~~Q8~~ | ~~回收站还原与彻底删除的端点？~~ | ~~补抓~~ | ✅ **已解决**：`/api/recycle/restore`、`/api/recycle/delete`（§6.3）。**并从中发现 `errno:132` 短信验证风控** |
| ~~Q9~~ | ~~分享列表/取消/**转存**的端点？~~ | ~~补抓~~ | ✅ **已解决**：`/share/record`、`/share/cancel`、`/share/surlinfoinrecord`、**`/share/transfer`（转存，含 `task_id` + `taskquery` 轮询）**，另发现 `/share/subscribe`、`/share/mountsinfo`。详见 §6.4 ⑥⑦⑧⑨ |
| **Q1b** | **`STOKEN` 的有效期与失效表现？** 是否与 `BDUSS` 同时失效？失效后 `errno` 是否也是 `-6`？ | 长时间挂机后重跑 `verify_cookie.py` | ⬜ 待验证（影响会话失效判定） |
| ~~Q2~~ | ~~秒传能否命中？`slice-md5` 是否为首 256KB 的 MD5？~~ | ~~用已知文件实测~~ | ✅ **已解决（Phase 1.6）**：真实上传后秒传同一文件 **`errno:0` 命中**；`slice-md5 = 加密md5(md5(首256KB))` 成立。详见 §5.2.1 |
| ~~Q3~~ | ~~用户可见 md5 第 9 位异常的真值还原规则？~~ | ~~同文件两种表示做 ground truth~~ | ✅ **已解决（Phase 1.6）**：从上传器 JS 提取到官方 `_encryptMd5`（4×8 重排 → 逐位 XOR `(o%16)` → 第 9 位替换为 `chr('g'+值)`），并用真实 ground truth 逐位验证通过。参考实现见 §5.4.2 |
| ~~Q4~~ | ~~分片大小是否固定 4 MB？~~ | ~~上传 >8MB 文件~~ | ✅ **已解决（Phase 1.6）**：9 MB 文件实测 3 片 = 4194304 / 4194304 / 1048576，全部 HTTP 200 且 md5 全匹配 |
| ~~Q5~~ | ~~上传域名是否按地域变化？~~ | ~~异地/多次会话抓包~~ | ✅ **已解决（上传 HAR）**：**域名确实动态变化**（同机 1 小时内 `bddwd-cm01` → `bdbl-cm01`）。已确证来源为 `locateupload` 的 **`server[0]`**，上传器 JS 逐字印证，缓存 60s。**必须动态获取，不可硬编码**。详见 §5.2.2 |
| ~~Q10~~ | ~~`unlist` / `wpfile` / `oper_id` / `category` 等字段语义？~~ | ~~对照多文件样本~~ | ✅ **已解决（Phase 2）**：406 条样本交叉分析，32 个字段全表 + category 对照表 + 分页特性，见 §3.1 补充。**唯一未定论：`pl`**（取值 0/1/2/4，疑似权限等级或路径层级） |
| ~~Q11~~ | ~~`/api/download` 的 `sign` 是否必需？~~ | ~~省略该参数测试~~ | ✅ **已解决（Phase 5 前置探测）**：**必需**（省略→`errno:2`，填错→`errno:113`）。但 `/api/filemetas?dlink=1` 免签名可用，Phase 5 改走该路线。详见 §6.6 |
| ~~Q12~~ | ~~`/share/surlinfoinrecord` 的 `sign`（hex32）从何而来？~~ | ~~补抓或试省略~~ | ✅ **已解决（Phase 6a）**：实测**省略 sign 返回 `errno:2`**，确认为必需。`分享接口.取提取码()` 会抛 `提取码需签名`。**变通**：创建时自己记住提取码，或从分享列表的 **`passwd`** 字段读取（实测一致） |
| ~~Q13~~ | ~~`/share/transfer` 的 `sekey` 从何而来？~~ | ~~补抓完整转存过程~~ | ✅ **已解决（Phase 6b）**：sekey = **`/share/verify` 响应的 `randsk`（URL 解码后）**，实测与该分享 transfer 实际使用的值 **sha1 完全一致（1fd96f5626）**。另发现 `/share/list` 依赖 verify 种下的 **`BDCLND` cookie**（不带则 `errno:-9`）。详见 §6.4 |

---

## 附录 A：HAR 分析产物索引

| 文件 | 内容 |
|---|---|
| `_har/解析.py` | 流式解析器。支持 `--har <文件>` / `--out <目录>`，可解析任意 HAR |
| `_har/分析2.py` | 业务接口深挖（分类/请求链/签名推导/必需头） |
| `_har/取证.py` | 针对性取证（md5 字符集、上传参数值域、登录链） |
| `_har/提取JS.py` | 客户端 JS 提取（`--list` / `--grep` / `--dump`） |
| `_har/verify_cookie.py` | ✅ Cookie 最小集验证器（递进剔除阶梯测试） |
| `_har/verify_pcs_cookie.py` | ✅ Q1c 探测：pcs 上传通道 Cookie 验证 + 明文 MD5 比对（**不调 `/api/create`**） |
| `_har/百度网盘_完整操作.har` | 原始抓包 1（1302 条，含上传，Cookie 已脱敏） |
| `_har/百度网盘_补充操作.har` | 原始抓包 2（866 条，含管理/回收站/分享/搜索/下载，**无上传**） |
| `_har/百度网盘_上传操作.har` | 原始抓包 3（86 条，**仅上传域**，用于复核分片大小与上传域名） |
| `_har/百度网盘_转存操作.har` | 原始抓包 4（42 条，含 **`/share/transfer` 转存**） |
| `_har/cookies.txt` | 34 个 Cookie（已 gitignore） |
| `_har/分析/01_域名与路径频次.md` | HAR1 域名与接口路径频次 |
| `_har/分析/06_请求明细.jsonl` | HAR1 共 1300 条脱敏明细 |
| `_har/分析/12_请求链.md` | HAR1 请求链重建（13 条） |
| `_har/分析/13_签名参数推导.md` | HAR1 签名/令牌/时间戳参数来源判定 |
| `_har/分析/14_必需请求头.md` | HAR1 按功能分类的请求头覆盖率 |
| `_har/分析/15_关键接口样例.md` | HAR1 关键接口样例 |
| `_har/分析/补充/01_域名与路径频次.md` | HAR2 域名与路径频次 |
| `_har/分析/补充/06_请求明细.jsonl` | HAR2 共 866 条脱敏明细 |
| `_har/分析/上传/06_请求明细.jsonl` | HAR3 共 86 条脱敏明细 |
| `_har/分析/转存/06_请求明细.jsonl` | HAR4 共 42 条脱敏明细 |
| `_har/spike_file_list.py` | ✅ Phase 2 文件浏览真机联调（6 组断言） |
| `_har/spike_phase34.py` | ✅ Phase 3+4 联合真机验证（建目录/增删改移/上传/秒传/还原 + 自动清理） |
| `_har/spike_download_sign.py` | ✅ R10 探测（4 组 sign 变体，定位到免签名路线） |
| `_har/spike_phase6.py` | ✅ Phase 6a 分享真机验证（创建/列表/取消/提取码探测 + 自动清理） |
| `_har/spike_phase6b.py` | ✅ Phase 6b 转存真机验证（verify→list→transfer→轮询→清理） |
| `_har/spike_phase5.py` | ✅ Phase 5 下载真机验证（MD5 比对/Range 206/续传/去重/负面对照） |
| `_har/百度网盘_转存完整.har` | 原始抓包 5（339 条，含分享页 HTML 与完整转存链路） |
| `_har/share_page_source.html` | 分享页内联脚本片段（定位 `__DISK_SHARE_V2_LOCALS__` 机制） |
| `光鸭云盘适配器_架构摘要报告.md` | 光鸭模板 629 行架构分析（含逐行耦合点） |

> ⚠️ **分析 `sign` 等被脱敏字段时必须回到原始 HAR。**
> `解析.py` 会把 `sign` / `bdstoken` / `bduss` 等键的值替换为 `<redacted:形态>` 占位符，
> 因此 `06_请求明细.jsonl` 中**同名字段的值恒等**——若在 jsonl 上比较这类字段会得出「恒定」的错误结论。
> 比较真实值请直接用 `iter_entries()` 读原始 HAR（本文档 §6.6 的 sign 分析即如此完成）。

## 附录 B：脱敏说明

本方案及全部分析产物遵循以下脱敏规则：
- `BDUSS` / `STOKEN` / `PTOKEN` / `bdstoken` / `token` / `sign` / `pwd` 等敏感参数的**值一律不落盘**，只记录「长度 + 字符形态」指纹（如 `hex32`、`b64ish4`）。
- `Cookie` 头只保留 cookie **名**。
- 响应体做正则二次脱敏后才写入摘要文件。
- `数据/`、`_har/`、`*.har`、`cookies.txt` 均已在 `.gitignore` 中排除。

> 本文件中的示例值（如 `1f9698adfqee0a68a5640844bc8714b9`、`MTc4OTMyNTQwNzUyODAuMTY2NjkzMDc5ODkyODU5NDI=`）为 md5 与请求追踪 ID，**非凭证**，保留是为了说明编码格式。

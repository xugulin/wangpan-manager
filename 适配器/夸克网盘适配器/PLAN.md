# 夸克网盘适配器 · 重构计划

> 版本：v1（2026-09-14）
> 基线提交：`8f461d4`（初始骨架，已清理敏感文件）
> 目标：把基于「光鸭云盘」的 GUI 骨架，改造成对接**夸克网盘**的可用适配器

## 0. 文档说明

| 角色 | 路径 | 权限 |
|---|---|---|
| 架构模板（保持分层与命名风格） | `<项目根>/适配器/光鸭云盘适配器/` | **只读** |
| 夸克实现参考（业务逻辑来源） | `<外部参考项目>/QuarkPan-master/` | **只读** |
| 改造目标（本仓库） | `<项目根>/适配器/夸克网盘适配器/` | 可写 |

- 本计划所有结论均来自对三个目录的**实际代码勘察**（`grep`/`read`），关键处标注 `文件:行号`。
- 两个参考目录**不会被修改**。本仓库改动一律走 Git 提交。
- 术语约定：**光鸭侧**指当前仓库（原光鸭骨架，46 个文件、7116 行）；**QuarkPan** 指 `QuarkPan-master`（46 个文件、10731 行 Python）。
- ✅ **能力决策已确认**（2026-09-14）：Q1/Q2/Q3 **全部选 A** —— 删除「云添加」、删除「回收站 + 上传记录」、短信登录**改造为手动 Cookie 登录**。详见 §6.1。**阶段一已无阻塞项。**

---

## 1. 目录结构对比

### 1.1 本仓库 / 光鸭侧（目标结构，保留）

按「网络 → 认证 → 接口 → 上传/下载 → 埋点」分层的**中文命名包**，外加独立 GUI 层：

```
夸克网盘适配器/
├── 启动.py                      # GUI 入口（含 Python 3.14 GC 规避）
├── 项目配置.toml                # PEP 621 元数据 + 依赖
├── 核心/
│   ├── 网络/  网络客户端.py 设备身份.py 追踪上下文.py
│   ├── 认证/  登录服务.py 令牌仓库.py 认证服务.py
│   ├── 接口/  文件接口.py 上传接口.py 分享接口.py 任务接口.py
│   │          资产接口.py 云添加接口.py 杂项接口.py 埋点签名.py
│   ├── 上传/  上传总调度.py 秒传服务.py 分片上传.py 哈希计算器.py
│   ├── 下载/  下载服务.py
│   └── 埋点/  埋点上报.py
├── 界面/
│   ├── 主界面.py 样式.py 字段映射.py 格式化工具.py 公共线程.py
│   ├── 页面/  文件浏览页.py 上传记录页.py 云添加页.py 分享页.py
│   │          账号信息页.py 日志页.py 基类.py
│   └── 弹窗/  短信登录对话框.py 二维码弹窗.py 分享对话框.py
└── 数据/                       # 运行时凭证目录（.gitignore 已忽略）
```

> **阶段一删除清单（Q1/Q2=A）**：`核心/接口/云添加接口.py`、`核心/接口/杂项接口.py`、`核心/接口/埋点签名.py`、`核心/埋点/`（整目录）、`核心/网络/设备身份.py`、`核心/网络/追踪上下文.py`、`界面/页面/云添加页.py`、`界面/页面/上传记录页.py`。
> **改造**：`界面/弹窗/短信登录对话框.py` → `界面/弹窗/Cookie登录对话框.py`（Q3=A）。
> 删除后 `核心/接口/` 保留 5 个文件，`界面/页面/` 保留 4 页，`界面/弹窗/` 保持 3 个。

### 1.2 QuarkPan（逻辑来源，无 GUI）

按「auth → core → services → cli」分层的**英文命名包**，GUI 层不存在（用 Typer + Rich 做命令行）：

```
QuarkPan-master/
├── cli.py                       # 入口薄壳
├── pyproject.toml  requirements.txt  README.md
├── examples/                    # 6 个用法示例
└── quark_client/
    ├── config.py                # Config / get_default_headers / get_config_dir
    ├── client.py                # QuarkClient（门面，~40 个方法）
    ├── exceptions.py            # 8 个异常类
    ├── core/api_client.py       # QuarkAPIClient（httpx 封装、重试、错误判定）
    ├── auth/  login.py(QuarkAuth) api_login.py(APILogin) simple_login.py(SimpleLogin)
    ├── services/ file_service.py file_upload_service.py file_download_service.py
    │             share_service.py batch_share_service.py name_resolver.py
    ├── utils/  logger.py qr_code.py
    └── cli/     main.py interactive.py utils.py commands/(7 个命令模块)
```

### 1.3 关键结构差异

| 维度 | 光鸭侧 | QuarkPan | 对重构的影响 |
|---|---|---|---|
| 分层粒度 | 网络/认证/接口/上传/下载 5 层 | core/auth/services 3 层 | 需把 services 拆回 接口/上传/下载 |
| 命名语言 | 中文标识符 | 英文标识符 | 保持中文（架构模板风格） |
| 门面对象 | 无（直接用 `网络客户端`） | `QuarkClient` 门面 | 建议**不引入**门面，沿用现分层 |
| GUI | PySide6，6 个标签页 | 无（CLI） | GUI 层**全部**由本仓库自研 |
| 异步模型 | QThread（`界面/公共线程.py`） | 同步阻塞 | GUI 侧用现成 QThread 包装即可 |
| 语言特性 | Python ≥3.10 | Python ≥3.8 | 保留 3.10+ 类型标注 |
| 代码量 | 核心 ~2900 行 / 界面 ~2500 行 | ~10700 行（含 CLI/示例 ~4000 行） | 真正可复用逻辑约 3500 行 |

---

## 2. 核心差异总览（决定重构策略）

这是整个重构最关键的表。**两边不是"换个域名"的关系**，认证模型与上传算法都不一样。

| 维度 | 光鸭侧（现状） | 夸克（QuarkPan 实现） |
|---|---|---|
| **认证模型** | OAuth2 **设备码**流程 + `refresh_token` 自动续期 | **Cookie**（`cookies.json`），**无 refresh token** |
| 登录方式 | 扫码授权（设备码轮询）、**短信验证码** | 扫码（`APILogin`）、**手动粘贴 Cookie**（`SimpleLogin`）；无短信 |
| 凭证存储 | `数据/令牌.json`（回退 `~/.光鸭云盘/访问令牌.json`） | `<config_dir>/cookies.json`（`QUARK_CONFIG_DIR` 或 **`cwd()/config`，相对路径会随运行目录漂移**） |
| 凭证字段 | `access_token` / `refresh_token` / `sub` | `__pus` / `__kps` / `__uid`（**全仓库无 `__puus`**，勿照传闻实现） |
| API 基址 | `https://api.guangyapan.com` | `https://drive-pc.quark.cn/1/clouddrive` |
| 分享基址 | 同域名 | `https://drive.quark.cn/1/clouddrive` |
| 账号基址 | `https://account.guangyapan.com` | `https://pan.quark.cn/account` |
| 端点风格 | `/userres/v1/file/get_file_list`（绝对路径） | `file/sort`（相对路径 + base_url 拼接） |
| 公共 query | 无 | `pr=ucpro&fr=pc&uc_param_str=&__t=<ms>&__dt=1000` |
| 成功判定 | 业务码 `code ∈ (None,0,147,156,200)` | JSON `status == 200` 且 `code == 0`；HTTP 401/403 → 认证失败 |
| 列表接口 | `/userres/v1/file/get_file_list`，字段 `parentId` | `GET file/sort`，字段 `pdir_fid`，`_page/_size/_sort` |
| 建目录 | `/userres/v1/file/create_dir`（`parentId`+`dirName`） | `POST file`（`pdir_fid`+`file_name`+`dir_init_lock`+`dir_path`） |
| 下载地址 | `/userres/v1/get_res_download_url` | `POST file/download` |
| 上传 | **oss2 SDK** 分片 + **MD5 秒传** | **httpx 手动 PUT** + **MD5+SHA1** 且带 `X-Oss-Hash-Ctx`（增量 SHA1 上下文） |
| 上传分片 | oss2 默认 | 单分片阈值 5MB，分片 4MB（`file_upload_service.py:99,324`） |
| **秒传** | **有**（MD5 命中，业务码 `156`） | **参考实现并未实现**：`_pre_upload` 只看 `status` 真值、完全忽略服务端秒传返回（`file_upload_service.py:208`） |
| **断点续传** | 无 | **无**（README 宣称支持，但代码里没有 `Range`/`Content-Range`/半成品文件逻辑，属虚假宣传） |
| 请求重试 | 连接层指数退避（3 次） | **完全没有重试**；`Config.MAX_RETRIES`/`RETRY_DELAY`/`DEFAULT_PAGE_SIZE`/`MAX_PAGE_SIZE`/`DOWNLOAD_CHUNK_SIZE`/`DOWNLOAD_DIR`/`ACCOUNT_URL` 均为**死配置**（`config.py:40-62`） |
| 分享创建 | `share_file` 直接返回 `shareUrl` | `POST share` → `GET task` 轮询 → `share/mypage/detail` |
| 我的分享 | `/userres/v1/get_share_list` | `GET share/mypage/detail` |
| 分享转存 | 无 | `share/sharepage/token` → `detail` → `save` |
| 离线下载（云添加） | **有**（直链 / BT 磁力 / 种子） | **无实现** |
| 回收站 | **有**（列表 + 清空） | **无实现**（仅 CLI 一处提示文案） |
| 上传记录 | **有**（`get_user_action` / `query_uploading_tasks_stat`） | **无实现** |
| 埋点上报 | **有**（`analysis-rcv.guangyapan.com`） | **无实现** |
| 设备指纹 | `did`/`dt`/`smid` + `设备身份.py` 持久化 | **不需要**（Cookie 即身份） |
| 追踪上下文 | `traceparent`（W3C） | 不需要 |
| 依赖 | httpx, qrcode, PySide6, **oss2** | httpx, typer, rich, pydantic, qrcode, tqdm（**无 oss2**） |

### 2.1 由差异推出的三条策略结论

1. **认证层是"重写"而非"改地址"**：`令牌仓库`（OAuth 令牌 + 自动刷新）在夸克没有对应物，必须改为 **Cookie 仓库**（加载/保存/过期判断 + 登录态校验）。`登录服务` 的短信流程无对应实现，需改造为「扫码 + 手动 Cookie」两种方式。
2. **上传层是最大技术风险**：夸克的 `X-Oss-Hash-Ctx` 增量 SHA1 上下文（`file_upload_service.py:651-792`，约 140 行）没有可靠实现——**参考实现本身对未知文件就是错的**（硬编码映射 + 无 padding 的近似 SHA1，详见 §8-1）。所以大文件分片**不能靠"照抄"解决**，必须先实测、再决定「重写」还是「降级为不支持大文件分片」。同时 `oss2` 依赖应移除，改用 httpx 手动 PUT。
3. **有 5 项能力在参考实现里不存在**（云添加/回收站/上传记录/埋点/短信登录），必须先做**产品决策**（见 §6.1），否则会阻塞阶段划分。

---

## 3. 模块级映射关系

### 3.1 `核心/网络/` → `core/api_client.py` + `config.py`

| 光鸭侧（现状） | QuarkPan 对应 | 处置 |
|---|---|---|
| `网络客户端`（`网络客户端.py:166`） | `QuarkAPIClient`（`api_client.py:16`） | **改写**：保留类名与中文方法名，替换基址/请求头/成功判定 |
| `_构造公共请求头`（`:207`） | `_build_headers`（`api_client.py:68`）+ `get_default_headers`（`config.py:21`） | **改写**：去掉 `authorization/did/dt/smid/traceparent`，改为 `cookie` + UA + origin/referer |
| `_检查业务码`（`:227`） | `_make_request` 内联判定（`api_client.py:151-166`） | **改写**：`code ∈ (None,0,…)` → `status == 200` |
| `_执行带重试`（`:105`） | 无（QuarkPan 无重试，仅超时/网络异常包装） | **保留**光鸭的连接层退避重试（更健壮） |
| `接口基础地址`/`账号基础地址`（`:30-31`） | `Config.BASE_URL`/`SHARE_BASE_URL`/`ACCOUNT_URL`（`config.py:38-40`） | **替换**常量 |
| `设备身份.py`（`获取设备ID`/`获取设备指纹`/`获取设备类型`） | **无对应** | **删除**（夸克靠 Cookie 标识身份） |
| `追踪上下文.py`（`生成追踪头` 等 4 函数） | **无对应**（`traceparent` 是光鸭专有） | **删除** |

### 3.2 `核心/认证/` → `auth/`

| 光鸭侧 | QuarkPan 对应 | 处置 |
|---|---|---|
| `登录服务`（`登录服务.py:56`） | `APILogin`（`api_login.py:20`）+ `SimpleLogin`（`simple_login.py:16`） | **改写为双通道**：扫码走 `APILogin` 等价逻辑；新增手动 Cookie |
| `登录服务.请求设备授权码`（`:107`） | `APILogin.get_qr_code`（`api_login.py:94`） | **替换**（设备码 → 二维码 token） |
| `登录服务.轮询令牌`（`:121`） | `APILogin.wait_for_login`（`:347`）+ `check_login_status`（`:255`） | **替换**（OAuth token 轮询 → 扫码状态轮询） |
| `登录服务.短信登录_*`（4 个方法，`:186-336`） | **无对应** | **改为「手动 Cookie 登录」**（Q3=A）；4 个短信方法删除 |
| `令牌仓库`（`令牌仓库.py:133`，含自动刷新） | `QuarkAuth._save_cookies/_load_cookies/_is_cookies_expired`（`login.py:33/45/79`） | **改写为 `凭证仓库`**：JSON 结构从 token 改为 cookie 列表 |
| `令牌仓库._默认刷新实现`（`:65`） | **无对应**（夸克无 refresh token） | **删除** |
| `认证服务.取当前用户`（`认证服务.py:18`） | 无直接对应；可用 `capacity`（`file_service.py:240`）或 `pan.quark.cn/account` 验证 | **改写**为「取账号信息（容量/会员）」 |
| — | `QuarkAuth.login` 的多级回退（`login.py:107`） | **新增**：自动（先用已存 cookie → 扫码 → 手动） |

### 3.3 `核心/接口/` → `services/`

| 光鸭侧 | QuarkPan 对应 | 处置 |
|---|---|---|
| `文件接口`（`文件接口.py:64`） | `FileService`（`file_service.py:13`） | **改写**（端点+字段全换，方法名保留） |
| `文件接口.取文件列表`（`:77`） | `FileService.list_files`（`:25`） | 端点 `/userres/v1/file/get_file_list` → `GET file/sort` |
| `文件接口.创建文件夹`（`:110`） | `FileService.create_folder`（`:103`） | → `POST file`，字段 `parentId/dirName` → `pdir_fid/file_name` |
| `文件接口.删除文件`（`:152`） | `FileService.delete_files`（`:131`） | → `POST file/delete` |
| `文件接口.取下载地址`（`:163`） | `FileService.get_download_urls`（`:580`）/ `FileDownloadService`（`:13`） | → `POST file/download` |
| `文件接口.取用户操作`（`:179`） | **无对应** | **删除**（Q2=A） |
| `文件接口.取上传记录`/`迭代上传记录`（`:200/211`） | **无对应** | **删除**（Q2=A） |
| `文件接口.取上传任务统计`（`:235`） | **无对应** | **删除** |
| `文件接口.取回收站列表`/`清空回收站`（`:246/272`） | **无对应** | **删除**（Q2=A） |
| `文件接口.取缩略图URL`（`:287`） | **无对应** | **保留**（夸克缩略图字段待逆向，见 §6.2） |
| `文件接口.创建目录`（`:143`） | 无（`FileService.resolve_path`/`NameResolver`） | **改造**为路径解析 |
| `上传接口`（`上传接口.py:193`） | `FileUploadService`（`file_upload_service.py:16`） | **改写**（见 §3.4） |
| `分享接口`（`分享接口.py:105`） | `ShareService`（`share_service.py:13`） | **改写** |
| `分享接口.创建分享`（`:127`） | `ShareService.create_share`（`:75`） | 需新增 **task 轮询** 环节 |
| `分享接口.取分享列表`（`:197`） | `ShareService.get_my_shares`（`:173`） | → `GET share/mypage/detail` |
| `分享接口.取消分享`（`:220`） | `ShareService.delete_share`（`:607`） | → `POST share/delete` |
| `分享接口.生成分享URL`（`:268`） | `ShareService.parse_share_url`（`:196`） | **改写**为夸克链接格式 `pan.quark.cn/s/<share_id>` |
| `分享接口.取分享模板`（`:113`） | **无对应** | **删除** |
| `任务接口`（`任务接口.py:22`） | 内联在各 service 的 `GET task` 轮询（`share_service.py:129`、`file_service.py:446`） | **保留并升级**为通用任务轮询器 |
| `资产接口`（`资产接口.py:26`） | `FileService.get_storage_info`（`:240`，`GET capacity`） | **改写**：`/assets/v1/get_assets` → `capacity` |
| `资产接口.取流量统计`（`:69`） | **无对应**（夸克无直链流量概念） | **删除** |
| `云添加接口`（`云添加接口.py:58`） | **无对应** | **删除**（Q1=A） |
| `杂项接口`（`杂项接口.py:10`，全局配置/横幅/未读消息） | **无对应** | **删除** |
| `埋点签名.py`（`计算埋点签名`） | **无对应** | **删除** |

### 3.4 `核心/上传/` + `核心/下载/` → `file_upload_service.py` / `file_download_service.py`

| 光鸭侧 | QuarkPan 对应 | 处置 |
|---|---|---|
| `上传总调度.上传文件`（`上传总调度.py:34`） | `FileUploadService.upload_file`（`:28`） | **改写**（流程骨架可复用） |
| `秒传服务.取上传令牌`（`秒传服务.py:72`） | `FileUploadService._pre_upload`（`:174`）+ `_get_upload_auth`（`:471`） | 端点 → `POST file/upload/pre` / `file/upload/auth` |
| `秒传服务.检查是否可闪电上传`（`:99`） | **无对应**（参考实现未实现秒传，`:99` 只是 `<5MB` 单分片分支） | **需自行实现** |
| `分片上传器.上传`（`分片上传.py:28`） | `_upload_multiple_parts`（`:310`） | **重写**：oss2 → httpx 手动 PUT |
| `分片上传器.按任务ID取文件信息`（`:120`） | `_wait_for_task` / `GET task` | **改写** |
| `哈希计算器.计算文件MD5`（`哈希计算器.py:11`） | `_calculate_file_hashes`（`:150`，MD5+**SHA1**） | **扩展为双算法** |
| — | `_calculate_incremental_hash_context`（`:651-792`） | **★ 需重写，不可移植**（参考实现本身不可靠，见 §8-1） |
| — | `_update_file_hash`（`:793`，`file/update/hash`） | **新增** |
| — | `_finish_upload`（`:872`，`file/upload/finish`） | **新增** |
| `上传接口.OSS直传`/`OSS直传流式`（`:355/391`） | `_upload_part_to_oss`（`:818`） | **重写**：去 oss2，用 httpx PUT |
| `下载服务.下载到文件`（`下载服务.py:32`） | `_download_file_stream`（`file_service.py:833`） | **改写**（含 `_generate_safe_filename` 同名去重） |

### 3.5 `核心/埋点/` → （无）

| 光鸭侧 | QuarkPan | 处置 |
|---|---|---|
| `埋点上报`（`埋点上报.py:38`） | **无对应** | **删除整个 `核心/埋点/` 目录** |

### 3.6 `界面/`（GUI 层，QuarkPan 无对应，全部本仓库自研）

| 光鸭侧 | QuarkPan 对应 | 处置 |
|---|---|---|
| `主界面`（`主界面.py:53`） | 无 | **保留**，改文案 + 改登录方式 |
| `公共线程.扫码登录线程`（`公共线程.py:20`） | 无（QuarkPan 同步阻塞） | **改写**：适配新登录服务的返回结构 |
| `公共线程.网络查询线程`/`批量上传线程`（`:44/62`） | 无 | **保留**（线程模型不变） |
| `字段映射.py`（`翻译字段名`/`格式化值`） | 无 | **整体重写**（字段名全换，见 §4.4） |
| `格式化工具.py`（5 个函数） | 无（可参考 `cli/utils.py` 的 `format_size` 等） | **保留**，仅改 `默认新文件夹名` 注释 |
| `短信登录对话框`（`短信登录对话框.py:21`，288 行） | **无对应** | **改造为「手动 Cookie 登录」对话框**（Q3=A，复用 QDialog 骨架） |
| `二维码弹窗`（`二维码弹窗.py:14`） | 无（CLI 用 `utils/qr_code.py` 打 ASCII/本地图片） | **保留**，改文案；可参考 `display_qr_code` |
| `分享对话框`（`分享对话框.py:17`） | 无 | **保留**，删掉夸克不支持的字段（见 §4.5） |
| `文件浏览页`（`文件浏览页.py:30`） | 无 | **保留**；删除回收站相关按钮与方法（Q2=A），其余不变 |
| `上传记录页`（`上传记录页.py:21`） | **无对应** | **删除**（Q2=A） |
| `云添加页`（`云添加页.py:22`） | **无对应** | **删除**（Q1=A） |
| `分享页`（`分享页.py:24`） | 无 | **保留**，改列表字段与链接格式 |
| `账号信息页`（`账号信息页.py:24`） | 无 | **保留**，改数据来源为 `capacity`/account |
| `日志页`（`日志页.py:10`） | 无（`utils/logger.py` 是控制台） | **保留** |
| `页面基类`（`基类.py:13`） | 无 | **保留** |

---

## 4. 需要替换的具体清单

### 4.1 类名替换

| 现名 | 目标 | 说明 |
|---|---|---|
| `网络客户端` | `网络客户端`（**类名不变**） | 内部改基址/头/判定，中文命名保持 |
| `登录服务` | `登录服务`（**类名不变**） | 内部改为扫码 + 手动 Cookie |
| `令牌仓库` | **`凭证仓库`** | 语义从 OAuth 令牌变为 Cookie 集合 |
| `认证服务` | `认证服务`（**类名不变**） | 改为取容量/账号信息 |
| `文件接口`/`上传接口`/`分享接口`/`任务接口`/`资产接口` | **类名全部不变** | 只换实现与端点 |
| `云添加接口`/`杂项接口` | — | **删除**（Q1=A） |
| `埋点上报` | — | **删除** |
| — | **`上传令牌`/`上传凭证`/`取令牌结果`**（`秒传服务.py:16/25/60`） | **需重定义**为夸克 upload auth 结构 |
| — | **`分片上传器`**（`分片上传.py:22`） | **重写**为 httpx 版 |

外部参考类（**不引入**，仅作逻辑来源）：`QuarkClient`、`QuarkAPIClient`、`QuarkAuth`、`APILogin`、`SimpleLogin`、`FileService`、`FileUploadService`、`FileDownloadService`、`ShareService`、`BatchShareService`、`NameResolver`。

### 4.2 函数名 / 方法名替换

| 现状 | 目标 | 依据 |
|---|---|---|
| `网络客户端.账号请求`（`:358`） | `账号请求` 保留，路径改 `pan.quark.cn/account` | `config.py:40` |
| `网络客户端.原始PUT`/`流式PUT`（`:365/380`） | 保留，用于 OSS 直传 | `file_upload_service.py:818` |
| `登录服务.请求设备授权码` | → `取登录二维码` | `APILogin.get_qr_code` |
| `登录服务.轮询令牌` | → `等待扫码确认` | `APILogin.wait_for_login` |
| `登录服务.完成扫码登录` | → `扫码登录` | `APILogin.login` |
| `登录服务.短信登录_*`（4 个） | → `手动Cookie登录`（Q3=A） | `auth/simple_login.py:28` |
| `令牌仓库.获取访问令牌`（`:288`） | → `取Cookie` | `QuarkAuth.get_cookies` |
| `令牌仓库.设置刷新回调`（`:158`） | 删除 | 无 refresh |
| `文件接口.取文件列表` | 保留名，参数 `parentId/page/pageSize` → `pdir_fid/_page/_size/_sort` | `file_service.py:25` |
| `文件接口.创建文件夹` | 保留名，体改 `pdir_fid/file_name/dir_init_lock/dir_path` | `file_service.py:103` |
| `文件接口.取回收站列表`/`清空回收站` | 删除（Q2=A） | — |
| `上传接口.取上传令牌`（`:242`） | → `取上传授权`（`file/upload/auth`） | `file_upload_service.py:471` |
| `上传接口.检查秒传`（`:453`） | → `预上传`（`file/upload/pre`） | `file_upload_service.py:174` |
| `上传接口.等待任务完成`（`:468`） | 保留名，端点 `GET task` | `share_service.py:129` |
| `分享接口.创建分享` | 保留名，内部改为 **创建 → 轮询 task → 取详情** 三步 | `share_service.py:75` |
| `分享接口.生成分享URL` | 保留名，格式改 `https://pan.quark.cn/s/{share_id}` | `share_service.py:196` |
| `资产接口.取资产信息` | → `取存储容量`（`GET capacity`） | `file_service.py:240` |
| `资产接口.取流量统计` | 删除 | 夸克无此概念 |

**新增函数（QuarkPan 有、本仓库缺）**

| 目标函数 | 参考位置 |
|---|---|
| `解析分享链接(url) -> (share_id, passcode)` | `share_service.py:196` |
| `取分享Token(share_id, password)` | `share_service.py:249` |
| `转存分享(share_id, token, pdir_fid)` | `share_service.py:313` |
| `等待转存任务(task_id)` | `share_service.py:377` |
| `计算文件Hashes(路径) -> (md5, sha1)` | `file_upload_service.py:150` |
| `计算增量SHA1上下文(路径, 分片号, 分片大小)` | `file_upload_service.py:651` ★ |
| `更新文件Hash(task_id, md5, sha1)` | `file_upload_service.py:793` |
| `完成上传(task_id, obj_key)` | `file_upload_service.py:872` |
| `解析路径(path, current_dir_id)` | `file_service.py:474` + `name_resolver.py:19` |
| `移动文件(fids, to_pdir_fid)` | `file_service.py:386`（含 task 轮询 `:428`） |
| `搜索文件(keyword)` | `file_service.py:183` |
| `取文件信息(fids)` | `file_service.py:61`/`:77`（`GET file`，注意与建目录的 `POST file` 同路径不同方法） |
| `取文件夹树(folder_id, max_depth)` | `file_service.py:221` |

### 4.3 配置项字段替换

**`项目配置.toml`**

| 行 | 现值 | 目标 |
|---|---|---|
| 2 | `name = "guangya-adapter"` | `name = "quark-adapter"` |
| 4 | `description = "光鸭云盘独立适配器"` | `description = "夸克网盘独立适配器"` |
| 7-11 | `httpx / qrcode / PySide6 / oss2` | 去掉 **`oss2`**；保留其余；按需加 `Pillow`（二维码渲染） |
| 17 | `光鸭 = "命令行:入口"` | **删除**（本仓库无 `命令行` 模块，此项本就悬空） |

**新增配置常量（`核心/网络/网络客户端.py`）**

| 常量 | 目标值 | 参考 |
|---|---|---|
| `接口基础地址` | `https://drive-pc.quark.cn/1/clouddrive` | `config.py:38` |
| `分享基础地址` | `https://drive.quark.cn/1/clouddrive` | `config.py:39` |
| `账号基础地址` | `https://pan.quark.cn/account` | `config.py:40` |
| `默认客户端标识` | **删除**（`aMe-8VSlkrbQXpUR` 是光鸭的） | — |
| `令牌仓库.客户端ID`（`令牌仓库.py:33`）、`登录服务.客户端ID`（`登录服务.py:43`） | **删除**；值为光鸭应用 ID `aMe-8VSlkrbQXpUR`（另在 `令牌仓库.py:18` 硬编码一次）。夸克扫码 URL 用**固定的 `client_id=532`** | `api_login.py:103` |
| `客户端版本` / `协议版本` / `SDK版本`（`令牌仓库.py:34`、`登录服务.py:44` 等） | **删除**（Authing SDK 专用） | — |
| — | 新增 `CHUNK_SIZE = 4*1024*1024`、`单分片阈值 = 5*1024*1024` | `file_upload_service.py:99,324` |
| `默认允许业务码` | **删除**，改为 `status==200` 判定 | `api_client.py:151` |
| 新增 `公共查询参数` | `{"pr":"ucpro","fr":"pc","uc_param_str":""}` | `config.py:43` |
| 新增 `时间戳参数` | `__t`(ms) / `__dt`(1000) | `api_client.py:54-62` |

**凭证存储路径**

| 现状 | 目标 |
|---|---|
| `数据/令牌.json`（`令牌仓库.py:43`） | `数据/凭证.json`（存 cookie 列表 + 时间戳 + 过期时间） |
| `~/.光鸭云盘/访问令牌.json`（`:51`） | `~/.夸克网盘/凭证.json` |
| `~/.光鸭云盘/设备信息.json`（`设备身份.py:14`） | **删除**（不再需要设备指纹） |
| `.gitignore` 的 `数据/令牌.json`、`*令牌*.json` | 追加 `数据/凭证.json`、`*凭证*.json`、`cookies.txt`（已含） |

### 4.4 字段名映射（`界面/字段映射.py` 需整体重写）

| 光鸭字段 | 夸克字段 | 说明 |
|---|---|---|
| `parentId` | `pdir_fid` | 父目录 ID |
| `fileId` | `fid` | 文件 ID |
| `fileName` | `file_name` | 文件名 |
| `fileSize` | `size` | 大小（字节） |
| `resType` | `file_type` / `dir` | 夸克区分方式不同 |
| `page` / `pageSize` | `_page` / `_size` | 分页 |
| `total` | `metadata._total` | 总数 |
| `code` / `msg` / `data` | `status` / `message` / `data` | 响应包裹 |
| `list` | `list` | 一致 |
| — | `metadata._total` / `metadata._count` | 分页总数 / 当前页条数（**不在 `data` 里**） |
| — | `file_type`（**0 = 文件夹**，非 0 = 文件）/ `dir`(bool) | 区分目录与文件，`文件浏览页` 双击逻辑依赖 |
| — | `_page` / `_size` / `_sort` / `_fetch_total` / `_order_field` / `_order_type` | 分页与排序请求参数 |
| — | `pdir_fid` / `to_pdir_fid` | 源目录 / 移动或转存目标目录 |
| `signedURL` / `urlDuration` | `download_url` | 下载链接（夸克无独立有效字段） |
| `shareId` / `userId` | `share_id` | 分享 ID |
| `createTime` / `updateTime` | `created_at` / `updated_at`（毫秒） | 时间戳 |
| `totalSpaceSize`/`usedSpaceSize`/`freeSpaceSize` | `capacity` 接口的 `total`/`used`/`free` | 需实测确认 |
| `totalDirectLinkTraffic` 等 6 个流量字段 | **无对应** | 从映射表删除 |
| `vipStatus`/`svipStatus`/`vipExpireTime` 等 | `member_type` / `vip` 等（待实测） | 见 §6.2 |

### 4.5 GUI 文案与图标替换清单

**A. 窗口/标题/工具提示（品牌）**

| 文件:行 | 现值 | 目标 |
|---|---|---|
| `界面/主界面.py:72` | `setWindowTitle("光鸭云盘管理工具")` | `"夸克网盘管理工具"` |
| `界面/主界面.py:124` | `"使用光鸭 App 扫描二维码授权登录"` | `"使用夸克 App 扫描二维码授权登录"` |
| `界面/主界面.py:113` | `f"令牌文件：{全局令牌仓库.文件路径}"` | `f"凭证文件：{凭证仓库.文件路径}"`（`令牌仓库` 类改名后此处必改） |
| `界面/主界面.py:128,132,346` | `"📱 短信登录"` / `"使用手机短信验证码登录"` | 随 §6.1-Q3 决策 |
| `界面/页面/上传记录页.py:88` | 文案中出现光鸭接口路径 `/userres/v1/get_user_action` | 随该页存废决策 |
| `界面/页面/云添加页.py:49` | 示例直链 `https://mirrors.tuna.tsinghua.edu.cn/linuxmint.iso` | 非品牌，可按需保留 |
| `界面/页面/分享页.py:79-81` | 表头含 `"下载方式"` | 夸克无此概念，删列 |
| `界面/弹窗/二维码弹窗.py:26` | `"📱 请用光鸭 App 扫码授权"` | `"📱 请用夸克 App 扫码授权"` |
| `界面/弹窗/短信登录对话框.py:192` | `"⚠️ 该手机号未注册光鸭账号（通道：{通道}）。"` | 随登录方式决策（§6.1-Q3） |

**B. 标签页（`界面/主界面.py:89-94`）**

| 行 | 现值 | 目标 |
|---|---|---|
| 89 | `"📁 文件浏览"` | 保持 |
| 90 | `"📤 上传记录"` | **整页删除**（Q2=A），标签页一并移除 |
| 91 | `"☁ 云添加"` | **整页删除**（Q1=A），标签页一并移除 |
| 92 | `"🔗 分享"` | 保持 |
| 93 | `"👤 账号信息"` | 保持 |
| 94 | `"📋 调试日志"` | 保持 |

**C. 登录方式（`界面/主界面.py:342-355`）**

| 现值 | 目标 |
|---|---|
| `"选择登录方式"` | 保持 |
| `"🔐 扫码登录"` | 保持（走夸克 `APILogin`） |
| `"📱 短信登录"` | → `"🍪 手动 Cookie 登录"`（推荐）或删除 |

**D. 分享对话框（`界面/弹窗/分享对话框.py`）**

| 行 | 现值 | 夸克是否支持 | 处置 |
|---|---|---|---|
| 68 | `"请输入自定义密码（建议 6 位以上）"` | ✅ `passcode` | 保留 |
| 84 | `"下载方式："`（直链/非直链） | ❌ | **删除该行** |
| 91 | `"流量限制："` | ❌ | **删除该行** |
| 97 | `"转存次数："` | ❌ | **删除该行** |
| 124-130 | 三种密码类型的说明文案 | 部分 | 保留无密码/私密两态 |
| 28/38/107 | `"🔗 创建分享"` | — | 保持 |

**E. 文件浏览页（回收站按钮已决策删除）**

| 行 | 文案 | 处置 |
|---|---|---|
| 122 | `"♻️ 回收站"` | **删除按钮及 `查看回收站`/`_回收站列表成功`/`_回收站列表失败` 方法**（Q2=A） |
| 126 | `"🔥 清空回收站"` | **删除按钮及 `清空回收站` 方法**（Q2=A） |
| 53-133 | `"🏠 根目录"` `"⬆ 上级"` `"🔄 刷新"` `"📁+ 新建文件夹"` `"📤 上传文件"` `"📁 上传文件夹"` `"📥 下载选中"` `"🔗 分享选中"` `"🗑 删除选中"` | 全部保留 |

**F. 全仓库品牌串（共 84 处 `光鸭`/`guangya` 命中）**

- **每个 `.py` 文件的第 1 行**都是 `# 光鸭云盘适配器/<相对路径>` 头注释 → 全部改为 `# 夸克网盘适配器/<相对路径>`。
- **logger 名**（14 处）：`logging.getLogger("光鸭云盘.*")` → `"夸克网盘.*"`。位置：`核心/网络/网络客户端.py:28`、`核心/认证/{登录服务.py:40,令牌仓库.py:29,认证服务.py:11}`、`核心/接口/{文件接口.py:53,上传接口.py:71,分享接口.py:64,任务接口.py:19,资产接口.py:23,云添加接口.py:34}`、`核心/埋点/埋点上报.py:31`、`界面/主界面.py:34`、`界面/公共线程.py:17`、`界面/页面/{基类.py:10,文件浏览页.py:27,上传记录页.py:18,云添加页.py:19,分享页.py:21,账号信息页.py:21}`。
- **域名**（11 处）：`核心/网络/网络客户端.py:10,30,31,192,193`、`核心/认证/登录服务.py:42,68,69,85,86`、`核心/认证/令牌仓库.py:32,78,79`、`核心/接口/上传接口.py:4,332,333`、`核心/接口/分享接口.py:25,48,270`、`核心/埋点/埋点上报.py:4,33,80`。
- **产品描述散文**（`光鸭没有独立…`、`光鸭云盘上传接口` 等）：`核心/接口/资产接口.py:10,72`、`核心/接口/上传接口.py:92,194`、`核心/接口/云添加接口.py:36`、`界面/格式化工具.py:77`、`核心/认证/令牌仓库.py:12`。
- **`项目配置.toml:2,4,17`**。

### 4.6 图标与资源

**实测结论：本仓库当前不存在任何图片/图标资源文件**（无 `.ico`/`.png`/`.svg`，`find` 全仓库无命中）。所谓"图标"全部是两类：

| 类型 | 位置 | 处置 |
|---|---|---|
| **Emoji 字符**（🔐📱📨📁📤📥🔗🗑♻️🔥🏠⬆🔄📋👤☁⚪🟡✅❌⚠️❌） | `界面/主界面.py:89-94,124` 等约 60 处 | 与夸克无关，**全部保留** |
| **Qt 内置标准图标** | `界面/弹窗/分享页.py:158`（`QMessageBox.Information`）、`界面/主界面.py:344`（`QMessageBox.Question`） | **保留** |
| **二维码位图** | `界面/弹窗/二维码弹窗.py:11,54`（`QPixmap.fromImage`，运行时由 `qrcode` 生成） | **保留**，逻辑不变 |
| **窗口图标** | 无（未调用 `setWindowIcon`） | **可选新增**：如提供 `资源/夸克.ico`，在 `界面/主界面.py:设置窗口` 内加 `self.setWindowIcon(QIcon(...))` |

> 注意：QuarkPan 侧也没有图标资源，仅有 `QrCode.jpg`（README 里的公众号二维码，**不要**挪用为产品图标）。

### 4.7 依赖替换

| 现状（`项目配置.toml:6-11`） | 目标 | 原因 |
|---|---|---|
| `httpx>=0.27.0` | **保留** | 两边都用 httpx |
| `qrcode>=7.4.2` | **保留** | 扫码登录仍需生成二维码 |
| `PySide6>=6.10.1` | **保留** | GUI 层不变 |
| `oss2>=2.19.0` | **移除** | 夸克上传走 httpx 手动 PUT（`file_upload_service.py:818`），QuarkPan 也未依赖 oss2 |
| — | **不新增** `typer`/`rich`/`pydantic`/`tqdm` | 这些是 QuarkPan 的 CLI 专用依赖，本仓库有 GUI，不需要 |

---

## 5. 分阶段实施步骤

### 阶段一：清理光鸭专属内容 —— ✅ 已完成（2026-09-14）

**目标**：仓库里不再有任何光鸭品牌/域名/无效模块，且删除后仍能 `python 启动.py` 打开空界面。

- [x] 1.1 全局品牌替换：46 个 `.py` 的头部注释、14 处 logger 名 → `夸克网盘.*`。
- [x] 1.2 删除 `核心/网络/追踪上下文.py`（`traceparent` 无对应）。
- [x] 1.3 删除 `核心/网络/设备身份.py` 及其调用点（`网络客户端.py:34` 的 import、`:207` 的头部注入、`令牌仓库.py:56` 的 `_取设备ID`）。
- [x] 1.4 删除 `核心/埋点/` 整个目录，移除 `界面/页面` 中对埋点的引用（若有）。
- [x] 1.5 删除 `核心/接口/杂项接口.py`、`核心/接口/埋点签名.py`。
- [x] 1.6 **按 Q1/Q2 决策（均为 A：删除）批量删除**：
      `核心/接口/云添加接口.py`、`核心/接口/杂项接口.py`、`核心/接口/埋点签名.py`、`核心/埋点/`（整目录）、
      `核心/网络/追踪上下文.py`、`界面/页面/云添加页.py`、`界面/页面/上传记录页.py`；
      并删除 `文件接口` 的 6 个无对应方法（取用户操作 / 取上传记录 / 迭代上传记录 / 取上传任务统计 / 取回收站列表 / 清空回收站）、
      `文件浏览页` 的回收站按钮与对应方法、`主界面.py:90-91` 两个标签页、`界面/字段映射.py` 对 `云添加接口.任务状态映射` 的 import。
- [x] 1.7 `界面/字段映射.py` 删除流量字段（`totalDirectLinkTraffic` 等 6 项）与光鸭专有键（另删 `操作类型映射`）。
- [x] 1.8 `项目配置.toml` 改名/改描述、删除 `[project.scripts] 光鸭`。
- [x] 1.9 `.gitignore` 追加 `数据/凭证.json`、`*凭证*.json`。

**实施补充（超出原清单的部分）**：
- `令牌仓库.客户端ID` / `登录服务.客户端ID` 的原适配器应用 ID `aMe-8VSlkrbQXpUR` 已置空；
  这两个模块的 OAuth 逻辑**未重写**（属阶段二 2.2/2.3），仅在文件头加了阶段二指引。
- `网络客户端`：新增 `共享基础地址`（分享页专用 base）；补注释提醒夸克端点必须传**不带前导斜杠**的相对路径。
- `资产接口`：`_带客户端标识` → `_请求体`（去掉 clientId 注入），并删除 `取流量统计()`。
- 各 `_带客户端标识` 改名同样应用于 `文件接口`。
- `界面/页面/账号信息页.py` 的类型列表由 8 项收缩为 4 项（用户信息 / 资产信息 / 用户权益 / 分享列表）。

**验收结果（全部通过）**：
- 代码区 `grep -rn '光鸭\|guangya\|aMe-8VSlkrbQXpUR' 核心 界面 启动.py 项目配置.toml .gitignore` → **0 行**
  （`PLAN.md` 作为迁移记录文档，按设计保留对旧项目的描述，不在检查范围内）。
- 35 个 `.py` 全部通过 `ast.parse`；本地导入图无断链。
- 用 `<项目根>/运行环境/venv/bin/python` 实测：**29 个模块全部导入成功**；
  `QT_QPA_PLATFORM=offscreen` 离屏启动 `主界面` 成功 —— 标题「夸克网盘管理工具」，**4 个标签页**
  （📁 文件浏览 / 🔗 分享 / 👤 账号信息 / 📋 调试日志）。
- 代码规模：34 个 `.py` / 5472 行（清理前 46 个 / 7116 行）。
**风险**：删除 `设备身份` 会连带 `令牌仓库` 的 import 链断裂 → 需与阶段二一并处理，建议 1.3 改为「暂留但停用」直到 2.4 完成。

### 阶段二：实现夸克核心适配器 —— 🔄 分批进行中（基础层 2.1~2.4 已完成，2026-09-14）

**目标**：`核心/` 能以夸克 Cookie 完成「登录 → 列目录 → 上传 → 下载 → 分享」全链路，可用脚本验证（不依赖 GUI）。

- [x] 2.1 **网络层**：`核心/网络/网络客户端.py` 换基址/公共 query/请求头；`_检查业务码` 改为 `status==200` 判定；保留连接层退避重试；新增 `__t`/`__dt` 注入。
- [x] 2.2 **凭证层**：`令牌仓库.py` → `凭证仓库`（cookie 列表 JSON 读写 + 过期判断，参考 `auth/login.py:33/45/79`）；`数据/凭证.json`。
- [x] 2.3 **登录层**：`登录服务.py` 重写为 `取登录二维码`/`查扫码状态`/`等扫描码`/`扫码登录`（参考 `api_login.py:94/255/347`）；新增手动 Cookie 通道（参考 `simple_login.py`）。
- [x] 2.4 **认证层**：`认证服务.py` 改为 `取容量`（`GET capacity`）。

**基础层实施补充（超出原清单的部分）**：
- 新增 `_拼接地址()`：原实现是 `基础地址 + 路径` 字符串相加，对夸克的不带前导斜杠
  相对路径（`file/sort`）会拼成 `.../clouddrivefile/sort`。现已同时支持
  `file/sort` 与 `/v1/user/me` 两种写法。
- `_检查业务状态()` 补上了参考实现的漏洞：QuarkPan 只在 `status == 'error'` 或
  `code != 0` 时抛错，于是 `{"status": 400}`（整数）会被误判为成功。
- 新增 `GET capacity` 作为登录态探针（`认证服务.是否已登录` / `登录服务.验证Cookie`）。
- **最小 GUI 接线**（完整改造仍属阶段三）：`主界面.py` 已切到凭证仓库 + `设置Cookie`；
  扫码通道已接通新服务；短信登录按钮改为「🍪 手动 Cookie」，用 `QInputDialog`
  临时收 Cookie（阶段三 3.2b 换成专用对话框）。
- 随之删除：`界面/弹窗/短信登录对话框.py`、`界面/格式化工具.py` 的 `规范化手机号()`、
  以及 `默认国家码` 常量（均为短信登录专属，后端删除后成死代码；
  对话框骨架可从 git 历史 `abeb2e0` 取回）。

**基础层验收结果（全部通过）**：
- 34 个 `.py` 全部通过 `ast.parse`；**27 个模块全部导入成功**；GUI 离屏启动正常
  （标题「夸克网盘管理工具」、4 个标签页、登录按钮为「🔐 扫码登录 / 🍪 手动 Cookie」）。
- 用 `httpx.MockTransport` 做**离线功能测试**（未碰真网络），覆盖：
  URL 拼接（相对/绝对路径）、公共 query 注入（`pr/fr/uc_param_str/__t/__dt`）、
  Cookie 头注入、`status==200` 判定、`status=400` 拒绝、`code!=0` 拒绝、
  HTTP 403 → 认证错误、允许业务码白名单、账号请求不注入公共参数、
  连接层退避重试（失败 2 次后成功）；以及登录服务的二维码获取/URL 拼装、
  扫码状态三态识别、手动 Cookie 的合法接受/缺字段拒绝/服务端校验拒绝。
- 代码区品牌残留 **0 行**。

- [x] 2.5 **文件接口**：列表 `file/sort`、建目录 `POST file`、删除 `file/delete`、重命名 `file/rename`、移动 `file/move`、搜索 `file/search`、树 `file/tree`。
- [x] 2.6 **下载**：`下载服务.py` → `POST file/download` + 流式下载 + 同名去重（参考 `file_service.py:833`、`:610`）。
- [x] 2.7 **上传** —— ✅ 已完成并**真实跑通**（2026-09-14）。真实流程（`file_upload_service.py:28-148`）：
      ① `POST file/upload/pre`（body 含 `ccp_hash_update/parallel_upload/pdir_fid/size/file_name/format_type/l_created_at`）→ 取 `task_id/auth_info/upload_id/obj_key/bucket("ul-zb")/callback`
      ② `POST file/update/hash`（`md5` + `sha1`，8KB 分块同时算；**无 crc64**）
      ③ ≤5MB 单分片；>5MB 按 **4MB** 分片，每片 `POST file/upload/auth` 取 `auth_key`
      ④ OSS PUT `https://{bucket}.pds.quark.cn/{obj_key}?partNumber=n&uploadId=id`，`authorization: auth_key`，ETag 取自响应头
      ⑤ 合并：再次 `POST file/upload/auth` → OSS POST（`application/xml` + `Content-MD5` + `x-oss-callback`），**203 也算成功**（合并成功但 callback 失败）
      ⑥ `POST file/upload/finish`

**2.7 实测结论（推翻了原计划里的风险判断）**：

> 原计划写着「`_calculate_incremental_hash_context` 大概率是错的，需重写」。
> **实测证明它是对的**，无需重写，也**不需要任何功能降级**。

| 实验 | 结果 |
|---|---|
| 离线：中间状态 + 标准 SHA1 padding 还原 `hashlib.sha1` | 4/4 用例**完全一致** → 算法正确 |
| 在线：10MB 分 4MB/4MB/2MB，**带** `X-Oss-Hash-Ctx` | 3 片全部 `HTTP 200`，合并 200，`finish:true` |
| 在线：同一文件**不带** `X-Oss-Hash-Ctx` | 分片 2 `HTTP 400 <Code>NoHashContext</Code> <EC>0042-00000106</EC>` |
| 在线：1MB 单分片 | `HTTP 200`，`finish:true` |

- **`X-Oss-Hash-Ctx` 是并行分片的强制要求**，不是可选项。
- **不需要 padding** 的原因：`已处理字节 = (分片号-1) × 4MB`，而 4MB = 65536 × 64 字节，恒为完整 SHA1 块。
- ⚠️ `GET config` 返回的 `allow_ccp_hash_update: false` 是**红鲱鱼**，不要据此省略上下文。
- `auth_meta` 签名串必须与实际发出的头**逐字节一致**（`x-oss-date` → `x-oss-hash-ctx` → `x-oss-user-agent`，顺序固定且含 `x-oss-hash-ctx` 行）。
- 落地位置：`核心/接口/上传接口.py`（协议层）、`核心/上传/{哈希计算器,分片上传,秒传服务,上传总调度}.py`；GUI 的 `批量上传线程` 已切到 `上传总调度`。

**2.7 附带发现（对后续阶段有价值）**：
- `GET capacity` **不存在（404）**，`file/capacity`、`member/capacity` 同样 404 → 已改 `GET member`（见 §6.2）。
- `POST file/delete` 需要 `fid_list` + `current_dir_fid`（二者与 `filelist` 互斥）；参考实现缺 `current_dir_fid`，会报 `14001 Bad Parameter`。**留给 2.5**。
- `file/sort` 列表有**秒级延迟**：`finish` 后立刻查询可能查不到，校验需重试/轮询。
- 未完成的分片上传会在云盘留下**幽灵文件**（`pre` + `update/hash` 即建记录），失败必须清理。

- [x] 2.8 **任务轮询**：`任务接口.py` 泛化为 `GET task` 轮询器，供上传/移动/分享/转存复用（参考 `share_service.py:129`、`file_service.py:428`）。
- [x] 2.9 **分享**：创建（`POST share` + task 轮询 + `share/mypage/detail`）、列表、删除、解析链接、转存（`sharepage/token`→`detail`→`save`）。
- [x] 2.10 **路径解析**：实现 `解析路径` 与 `NameResolver` 等价缓存（`name_resolver.py:19`）。

### 阶段二业务层实施记录（2026-09-14，2.5~2.10 完成）

冒烟测试 `验证/核心冒烟.py` 全绿：**通过 30 项 / 失败 0 项**，覆盖
列根目录 → 建文件夹 → 上传小文件(1MB 单分片) → 上传大文件(6MB 两分片) →
列目录校验 → 下载(md5 一致 + 同名去重) → 创建分享 → 分享列表 → 链接解析 →
转存链路 → 路径解析。

#### 🔴 事故记录：`file/delete` 误清空根目录（已修复 + 已恢复数据）

**现象**：调用 `删除文件(fid, 当前目录ID="0")` 后，根目录**整个被清空**
（同级目录与其中的文件全部消失，可在回收站看到）。

**根因**：夸克把删除拆成**两种互斥模式**，我误用了组合：
| 意图 | 正确请求体 | 我当时的写法 |
|---|---|---|
| 删除指定文件 | `{"action_type":2, "filelist":[fid...], "exclude_fids":[]}` | ❌ `{"fid_list":[...], "current_dir_fid":"0", "exclude_fids":[]}` |
| 删除整个目录 | `{"current_dir_fid": <fid>}` | — |

`fid_list` + `current_dir_fid` 被服务端按**第二种模式**处理，于是
`current_dir_fid="0"` 等同于「清空根目录」。两者同时出现才会报
`14001 不能同时存在值`，所以这个错误写法**静默生效**了。

**处置**：① 已把 `删除文件` 改为只发 `action_type/filelist/exclude_fids`，
并加了醒目注释 + 对废弃参数的告警；② 用「建 A/B → 删 A → 验 B 存活」验证通过；
③ 从回收站恢复了被误删的用户目录（`file/recycle/recover`）。

**教训**：写操作**必须**做「只删目标、同级存活」的对照验证，
不能只看 HTTP 200 就当成功。

#### 其他实测结论（新增/修正）

- **下载需要 `__puus`**：缺失时 CDN 返回
  `403 RequestDeniedByCallback / require login [auth miss]`。
  CAS 扫码登录**不下发**它，但登录后 `GET member`（或 `config`）会由服务端 Set-Cookie 下发
  → 已在 `登录服务` 补全 + `网络客户端._捕获刷新Cookie` 持续捕获 + `下载服务._确保下载凭证` 自愈。
- **`file/search` 的关键词参数是 `q`**，不是 `keyword`（用错会被静默忽略、返回无关结果）。
- **`file/tree` 的深度参数是 `max_depth`**，不是 `_depth`。
- **回收站恢复端点是 `file/recycle/recover`**（不是 `restore`，后者 404），
  且必须带 `select_mode`（否则 `14001`）。`file/recycle/list` 取 `record_id`。
- **分享有两套 ID**：`share_id`（32 位十六进制，管理/取消用）与 URL slug（访问/转存用），
  两者不相等。
- **夸克用 HTTP 404 + JSON body 表达业务错误**（如转存自己的分享 → `41017`），
  因此 `网络客户端` 在 `raise_for_status()` **之前**先解析 JSON 透出业务码。
- **列表有秒级延迟**，且 `file/sort` 在某些操作后可能瞬时返回空页；校验要重试/轮询。
- **凭证过期时间只应取鉴权关键 Cookie（`__pus/__puus/__kps/__uid/__ktd`）的最小 expires**：
  登录响应里的 `ctoken` 等只活约 10 分钟，按全部 cookie 取 min 会把整份凭证
  误判为 10 分钟过期（QuarkPan `login.py:62-77` 即有此问题）。

**验收**：写一个临时脚本 `验证/核心冒烟.py`（不入库或入 `测试/`）跑通：登录态 → 列根目录 → 建目录 → 上传 1 个小文件与 1 个 >5MB 文件 → 取下载地址 → 创建分享。大文件上传成功是**本阶段唯一硬指标**。
**风险**：`X-Oss-Hash-Ctx` 增量 SHA1（140 行，含自定义 `calculate_sha1_incremental_state`）**参考实现不可靠**，大文件第二片起会被 OSS 拒绝（400/403）——这是本阶段唯一可能"做不完"的项，建议**先安排一个 spike**（用 >5MB 文件实测 OSS 是否接受）再决定路线；Cookie 过期无 refresh，需给用户明确提示（`AuthenticationError`）。

### 阶段三：替换 GUI 层调用

**目标**：GUI 六个标签页与夸克后端完全对接，无残留光鸭调用。

- [x] 3.1 `公共线程.py`：`扫码登录线程` 适配新登录服务返回；`批量上传线程` 适配新上传签名。
- [x] 3.2 `主界面.py`：窗口标题、登录区文案、登录方式选择框（扫码 / 手动 Cookie）；`检查已有登录` 改为检查 `凭证仓库`；`令牌文件：` 标签改为 `凭证文件：`。
- [x] 3.2b **新增 `界面/弹窗/Cookie登录对话框.py`**（由 `短信登录对话框.py` 改造，Q3=A）：把「手机号 + 验证码」两行换成「Cookie 多行输入框」，保留状态标签/按钮组/倒计时骨架，新增 `__kps`/`__uid` 存在性校验；`主界面.开始短信登录` → `开始Cookie登录`。
- [x] 3.3 `二维码弹窗.py`：文案改夸克；链接展示改夸克授权 URL。
- [x] 3.4 `字段映射.py`：按 §4.4 全表重写（`pdir_fid`/`_page`/`_size`/`status`/`metadata` 等）。

**阶段三第一批（3.1~3.4）实施记录（2026-09-14）**：

- **3.1**：`扫码登录线程` 新增 `状态(str)` 信号（轮询进度），构造函数支持 `扫码超时秒`；
  主界面把它接到状态栏 + 二维码弹窗。`批量上传线程` 已在阶段二切到 `上传总调度`，
  本次核对信号契约（`总进度/单文件成功/全部成功/失败`）一致，无需改动。
- **3.2**：`检查已有登录` / `初始化网络客户端` / 退出清理已在阶段二切到 `凭证仓库` + `设置Cookie`；
  `令牌文件：` → `凭证文件：` 亦已完成；本次把临时 `QInputDialog` 换成专用对话框。
- **3.2b**：骨架取自 `git show abeb2e0:界面/弹窗/短信登录对话框.py`，保留
  QDialog 布局 / 四色状态标签 / 按钮组 / 线程清理 / `closeEvent`；
  「手机号 + 验证码」→ `QPlainTextEdit` 多行 Cookie 输入，**边输入边做本地校验**
  （复用 `凭证仓库.校验Cookie`，单一事实来源），并提示是否含 `__puus`（下载必需）；
  新增「跳过服务端校验」勾选项（离线场景）。
- **3.3**：夸克**没有用户码** → 该行在为空时自动隐藏；新增 `设置状态()` 供轮询进度回填；
  链接说明改为「复制到手机浏览器打开（会唤起夸克 App）」，并标注二维码约 5 分钟有效。
- **3.4**：`字段映射.py` 按 §4.4 全表重写：新增夸克原生键（`fid`/`pdir_fid`/`file_name`/
  `file_type`/`dir`/`size`/`created_at`/`updated_at`/`metadata._total`/`_page`/`_size`/
  `download_url`/`share_id`/`share_url`/`passcode`/`total_capacity`/`use_capacity`/
  `member_type`/`record_id` 等，共 126 个键，无重复）；
  **`file_type == 0` 判定为目录**并新增 `文件类型映射`；
  时间戳格式化支持**毫秒**（夸克 `created_at`/`updated_at` 是毫秒）；
  `status` 因在不同响应里含义不同（HTTP/任务/分享），**不做裸翻译**，改用 `task_status`/
  `share_status` 明确键名。
- **验收**：36 个 `.py` 全部通过 `ast.parse`；关键模块导入成功；
  `QT_QPA_PLATFORM=offscreen python 启动.py` 跑满 15 秒被 timeout 终止（退出码 124，
  说明事件循环正常常驻），日志显示「发现已保存的有效凭证 → 列目录成功，共 1 项」，
  并自动捕获了服务端刷新的 `__puus`。

- [x] 3.5 `文件浏览页.py`：列目录/建目录/上传/下载/删除/分享改为新接口；回收站按钮按决策处理。
- [x] 3.6 `分享页.py`：列表字段改造；分享链接格式改 `pan.quark.cn/s/<share_id>`；新增「转存」入口（夸克特性）。
- [x] 3.7 `账号信息页.py`：数据源改为 `capacity` + account 接口；删除流量类展示项。
- [x] 3.8 `分享对话框.py`：删除「下载方式/流量限制/转存次数」三行（§4.5-D）。
- [x] 3.9 `格式化工具.py`：`默认新文件夹名` 注释更新（命名规则可保留）。
- [x] 3.10 日志页/基类/样式：仅品牌替换。

**阶段三第二批（3.5~3.10）实施记录（2026-09-14，阶段三完成）**：

- **3.5**：复核确认——回收站按钮与 4 个相关方法已在阶段一（Q2=A）删除，
  仅保留一句正确的提示「文件会移入回收站，可恢复」；五个按钮
  （新建文件夹 / 上传文件 / 上传文件夹 / 下载选中 / 分享选中 / 删除选中）
  已全部对接夸克接口（`取文件列表` / `创建文件夹` / `上传总调度` /
  `取下载地址`(download_url) / `删除文件`(已修正的 filelist 形态)）。
- **3.6**：删掉夸克不存在的「**下载方式**」列（7 列 → 6 列，`_追加行` 列号与
  「复制链接」取的列同步调整）；`share_url` 缺失时用 URL slug 兜底生成
  `https://pan.quark.cn/s/<slug>`；第 0 列 UserRole 改存 `share_id`（取消分享用）；
  表格 tooltip 同时展示 `share_id`（管理用）与 URL slug（转存用）；
  **新增「📥 转存分享链接」按钮** → `分享接口.一键转存()`（解析→token→save→轮询）。
- **3.7**：账户数据源全部收敛到 `GET member`：`账号信息` / `存储容量` / `会员权益` /
  `分享列表`；`认证服务` 新增 `取会员权益()`（从 member 的 `member_info` /
  `member_status` / `deep_recycle_stat` 派生）；**删除 `核心/接口/资产接口.py`**
  （它指向原适配器的 `/assets/v1/get_assets` 与 `/nd.bizassets.s/v1/get_user_rights`，
  在夸克都是 404，且已被 member 完全覆盖）。
- **3.8**：删除「下载方式 / 流量限制 / 转存次数」三行及其控件与 `取配置()` 键，
  `QSpinBox` 导入一并清理；`取配置()` 现在只回传夸克真正支持的
  `fileIds / title / shareType / validateDuration / code / autoFillCode`。
- **3.9 / 3.10**：品牌扫描已为 0 行（阶段一/二完成），本轮复核并清理了
  因 3.6/3.8 产生的无用导入（`下载类型映射`、`QSpinBox`）。

**阶段三验收**：
- 35 个 `.py` 全部通过 `ast.parse`；13 个关键 GUI/核心模块导入成功。
- `QT_QPA_PLATFORM=offscreen python 启动.py` 跑满 15 秒被 timeout 终止
  （退出码 **124**，事件循环正常常驻），日志显示「发现已保存的有效凭证 →
  列目录成功，共 1 项」，无任何 Traceback。
- `grep -rn '光鸭\|guangya' 界面/` → **0 行**；
  `核心/ 启动.py 项目配置.toml .gitignore` 亦为 **0 行**。

**验收**：GUI 手动走查——扫码登录 → 浏览目录 → 新建文件夹 → 上传（小/大）→ 下载 → 创建分享 → 复制链接 → 查看账号信息；`grep -rn '光鸭' 界面/` 为 0。
**风险**：所有网络调用在 QThread 内同步执行，夸克接口更慢（`GET task` 轮询），需确认 UI 不假死；上传进度回调需重新映射（夸克分片回调语义不同）。

### 阶段三·修复批次（真实 GUI 实测暴露的 5 个问题，2026-09-14）

真实 GUI 走查暴露 5 个问题，全部定位到根因并修复，另**顺带查出 3 个新 bug**。

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| ① | 上传失败，OSS 报 `date is not valid` | **Qt 在 `QApplication` 初始化时调 `setlocale(LC_ALL,"")`**，把 `LC_TIME` 从 `C` 改成 `zh_CN.UTF-8`；`strftime("%a, %d %b %Y")` 于是输出 `日, 13 9月 2026`。纯脚本测不出（CPython 只设 `LC_CTYPE`，不动 `LC_TIME`） | `_OSS日期()` 改用 `email.utils.formatdate(usegmt=True)`（内部硬编码英文表，与 locale 无关） |
| ② | 下载 403 `RequestDeniedByCallback` | GUI 曾自建 httpx 客户端**不带 Cookie**；且 CAS 扫码登录拿不到 `__puus`（防盗链回调强制要求） | 统一走 `下载服务`：请求前缺 `__puus` 先 `GET member` 补；403 时**强制刷新 `__puus` 重试 1 次** |
| ③ | 小文件上传走多分片 | 无 | 拆出 `_单次上传()`：<5MB 只 1 次 PUT，日志不再写「分片 1/1」 |
| ④ | 账号信息页有裸英文枚举 | `member_status` / `subscribe_*_map` 的子键是会员等级，其**值**没做翻译 | 补会员等级值翻译、`super_vip_exp_at` 等时间戳、`share_type`(下划线) 等 |
| ⑤ | 分享页 `NameError: name '时间字符串' is not defined` | 3.6 编辑时删掉了赋值 | 重写 `_追加行`，`取()`/`时间()` 辅助 + 整体 try 兜底 |

**③ 的补充实测结论**：不存在「完全不带 multipart 的简单 PUT」这条路——
`file/upload/auth` 的 `auth_meta` 若不带 `uploadId`，服务端直接
`Bad Parameter: [uploadId is null]`，所以 `partNumber=1&uploadId=…` 是硬性要求，
「单次上传」= 1 次 PUT + 1 次单分片合并。

**本轮新查出的 bug**：

1. **🔴 `分享/delete` 是静默 no-op（最严重）**
   字段名必须为 **`share_ids`（数组）**：

   | 请求体 | 结果 |
   |--------|------|
   | `{"share_id": "<32hex>"}` | 返回 `{"status":200,"code":0,"message":"ok"}` —— 但**什么都没删** |
   | `{"share_id": ["<32hex>"]}` | 500 `inner error` |
   | `{"share_ids": ["<32hex>"]}` | ✅ 真正删除 |

   旧实现用的是第一种，导致「取消分享」一直假装成功。已在 `取消分享()` 改用
   `share_ids`，并默认**回查列表确认**（把这个会撒谎的接口的假成功如实标成
   `已删除=False`），支持一次批量删除。
2. **🔴 新建文件夹「假成功」**：成功回调读 `结果.get("fileId")`，而夸克返回的键是
   **`fid`** → 日志永远 `fileId=None`，且**创建失败也会弹成功提示**。已改读 `fid`，
   取不到就报错。
3. **🟡 `*_serve_days` 被当成秒**：`recycle_normal_serve_days=10` 显示成「10 秒」，
   真实语义是「10 天」。已从 `秒数字段` 移出，新增 `天数字段` → 「10 天」。
   另外 `expired_left` 是**剩余毫秒数**不是时间戳，按时间戳格式化会错显成 2043 年，
   已按 duration 处理。
4. **🔴 凭证文件权限退化成 644（安全）**：`凭证仓库._保存()` 用普通 `open()` 写临时
   文件，遵循 umask（022）→ 落盘 0644，而该文件里装的是**可直接登录的会话 Cookie**，
   同机其他用户可读。已改为 `os.open(..., 0o600)` 创建 + 替换后 `chmod 0600` 兜底，
   并补 `fsync`。实测：重写后权限由 `-rw-r--r--` 变为 `-rw-------`，Cookie 数据无损。

**实测确认的夸克行为（供后续参考）**：
- `GET share/mypage/detail` **不返回 `passcode`**，提取码只能由 `POST share/password`
  单独查 → 分享页新增「🔑 取选中项提取码」按需拉取按钮。
- 单文件分享时**夸克忽略自定义 `title`**，一律用文件名（创建响应/详情回读/列表三处一致）。
- 列目录与分享列表**都有滞后**（新对象不会立刻出现在 sort 结果里），约 1~3 秒。

**修复批次验收**（真实账号）：
- 核心链路（先起 `QApplication` 复现 locale）：`LC_TIME=zh_CN.UTF-8` 下
  `_OSS日期()` 仍为 `Sun, 13 Sep 2026 17:54:18 GMT`；1KB 单次上传/10MB 3 分片、
  上传下载**字节级一致**，测试产物自动清理。
- GUI 端到端（offscreen 驱动真实控件）：**7/8 通过**，唯一一项是测试脚本按
  `title` 找分享（实际被服务端换成文件名），非产品缺陷；
  新建文件夹、1KB+10MB 上传、下载字节一致、创建分享、账号信息四页全中文均通过。

### 🔴 重大事故：上传文件夹时 GUI 段错误（2026-09-14，已修复）

**现象**：真实 GUI 批量上传文件夹时进程 `段错误 (核心已转储)`，
bash 报 PID 265102，core dump 落在 `coredumpctl`。

**定位手段**：`coredumpctl info 265102` 取 C 栈 —— 崩溃线程 TID 265154
的 OS 线程名是 **`批量上传线`（批量上传线程）**：

```
#0 QTextLayout::lineCount()             libQt6Gui
#1 QTextCursorPrivate::blockLayout()
#4 QTextCursor::movePosition()
#5 QWidgetTextControl::moveCursor()     ← 文本控件
#7 libpython3.14                        ← 从 Python 调进来
```

**根因**：`界面/页面/日志页.py` 把 `logging.Handler` 挂在 **root logger** 上，
而 handler 的 `emit()` 里**直接操作 QTextEdit**：

```python
自身.文本编辑.append(样式消息)          # :48
自身.文本编辑.moveCursor(QTextCursor.End)  # :49  ← 崩溃点（全仓库唯一 moveCursor）
```

**logging 是同步调用** —— 谁打的日志就在谁的线程里跑 `emit()`。上传线程每打
一条日志，就在上传线程里 `append()/moveCursor()` 一个 QTextEdit，与主线程的
重绘抢同一个 `QTextLayout` → SIGSEGV。Qt 自己也报了
`QBasicTimer::start: Timers cannot be started from another thread`
（连光标闪烁定时器都被跨线程启动了）。

规模上必然崩：上传调用链共 **32 处 `logger.*`**；实测**每个小文件产生 12 条
日志记录**（5 条应用层 + 7 条 httpx 请求日志，`basicConfig(level=DEBUG)` 全部进 root）。

**为什么上线前的离屏测试全绿**：跨线程改 QWidget 是**数据竞争**，只有主线程
**同时重绘同一控件**时才踩坏。offscreen 测试主线程基本空闲，竞争窗口极小 ——
属于**漏测**，不是环境问题。

**因果级证明（对照实验）**：同样 4 线程 × 4000 条日志 + 主线程 `QTimer(0)`
疯狂 `repaint()`，唯一变量是 handler 实现：

| 组 | 结果 |
|----|------|
| 修复前（emit 里直接改控件） | **SIGSEGV，退出码 139** |
| 修复后（emit 只投递信号） | **通过，3/3 轮** |

**修复方案**：

- **P0 线程安全**：`日志页` 新增 `新日志 = Signal(str)`；handler 的 `emit()`
  只做 `宿主.新日志.emit(消息)`，由 Qt 自动排队到 GUI 线程执行
  `_追加日志()`（append + moveCursor）。零锁、零竞争。控件销毁时
  `destroyed` 自动摘掉 handler，防止往已销毁对象投递。
- **P1 降噪**：新增 `界面日志过滤器` —— 界面只收 `夸克网盘.*` 的 INFO+ 与
  任何来源的 WARNING+；控制台仍保留完整 DEBUG。httpx 的 7 条/文件噪音归零。
  handler 级别设为 INFO（DEBUG 不进界面）。
- **健壮性**：`document().setMaximumBlockCount(5000)`，长批量上传不再无限增长。
- **P3 回归测试**：新增 `验证/界面线程安全回归.py` —— 多工作线程满速打日志 +
  主线程持续 `repaint()` 同一控件，专打这个竞争窗口。

⚠️ **回归测试自身的坑（已修）**：初版忘记设 root logger 级别，root 默认
WARNING，`logger.info()` 在**生成记录前**就被 `isEnabledFor()` 挡掉，
12 秒只压了 **32 条**（4×4000/500 的 warning）却“通过” —— 虚假安全感。
现已在测试里显式 `setLevel(DEBUG)`，并把通过阈值提到渲染 ≥1000 块，
流量回到 16032 条，日志页顶到 5000 块上限。

**顺带查清的两点（回答排查清单）**：

- **GC 与本次崩溃无关**：`启动.py:9-13` 的 `gc.disable()` 是骨架遗留 workaround，
  全仓库**没有任何 `gc.collect()` 调用**，崩溃栈里也没有 GC 帧。
  但那句「Python 3.14 跨线程 GC 导致 PySide6 段错误」的注释**未经证实**，
  且 `gc.disable()` 会让循环引用垃圾整个进程生命周期都不回收 —— 建议单独验证后
  再决定是否保留。
- **`批量上传线程` 与 `processEvents` 都是干净的**：线程类只 `emit` 信号，
  进度回调经 `总进度 = Signal(str, float)` 跨线程排队（槽在主线程执行）；
  全仓库 `processEvents` **0 处**，重入崩溃可排除；`_线程完成清理` 用
  `deleteLater()`，线程对象不泄漏。架构不变量 `核心/ 零 PySide6 依赖` 成立。

### 阶段四：更新依赖和文档

- [ ] 4.1 `项目配置.toml`：移除 `oss2`，修正 `name`/`description`/`scripts`（§4.3）。
- [ ] 4.2 新增 `README.md`：项目说明、依赖安装、夸克 Cookie 获取方式、登录方式、常见错误（401/403、容量不足）。
- [ ] 4.3 若采纳「依赖 quarkpan 包」方案：在 `项目配置.toml` 加入 `quarkpan>=1.0.5`（否则不引入）。
- [ ] 4.4 把本 `PLAN.md` 转为「已完成/未完成」勾选状态，并记录实测的接口响应样例（脱敏）到 `文档/接口实录.md`。
- [ ] 4.5 `.gitignore` 复核：确保 `数据/`、`cookies.txt`、`config.json` 不会入库。

**验收**：新环境 `pip install -e .` 后 `python 启动.py` 可直接运行；`git status` 干净。

### 阶段五：运行与修复

- [ ] 5.1 建立冒烟脚本（`测试/` 目录，已被 `.gitignore` 的 `测试/*.json` 部分覆盖但 `.py` 会入库）。
- [ ] 5.2 真实账号端到端回归：登录态持久化、Cookie 过期表现、大文件断点、分享转存。
- [ ] 5.3 异常路径：网络超时、容量不足（`capacity limit`）、文件不存在、分享密码错误、任务超时。
- [ ] 5.4 性能：`GET task` 轮询间隔与退避；上传分片并发度（QuarkPan 为串行，可评估并发）。
- [ ] 5.5 清理：删除临时脚本与调试日志；把实测结论回填 `文档/接口实录.md`。
- [ ] 5.6 打 tag：`v0.1.0-quark`（首个可用版本）。

**验收**：无未处理异常弹窗；日志无 `Traceback`；`git log` 阶段清晰。

---

## 6. 风险与未决问题

### 6.1 三项能力决策（✅ 已决策：全部选 A）

| 编号 | 问题 | 决策 | 落地动作 |
|---|---|---|---|
| **Q1** | 夸克**离线下载（云添加）**在 QuarkPan 中零实现（`grep 离线/磁力/torrent` 命中 0），自研需逆向、风险高工期长 | ✅ **A：删除** | 删 `核心/接口/云添加接口.py`；删 `界面/页面/云添加页.py`；移除 `主界面.py:91` 的「☁ 云添加」标签页；删 `界面/字段映射.py` 对 `云添加接口.任务状态映射` 的 import |
| **Q2** | **回收站**与**上传记录**同样无实现（QuarkPan 仅在 `cli/utils.py:102` 有一句文案） | ✅ **A：删除** | 删 `界面/页面/上传记录页.py`；移除 `主界面.py:90` 的「📤 上传记录」标签页；删 `文件接口` 的 `取用户操作`/`取上传记录`/`迭代上传记录`/`取上传任务统计`/`取回收站列表`/`清空回收站`；删 `文件浏览页.py:122/126` 两个按钮及 `查看回收站`/`_回收站列表成功`/`_回收站列表失败`/`清空回收站` 方法。保留文件浏览、上传、下载、分享等核心功能 |
| **Q3** | 夸克无短信登录实现（`grep 短信/sms` 命中 0），`短信登录对话框.py`（288 行）待处置 | ✅ **A：改造为手动 Cookie 登录** | `界面/弹窗/短信登录对话框.py` → `界面/弹窗/Cookie登录对话框.py`：复用 QDialog 骨架/状态标签/按钮组，把「手机号 + 验证码」两行替换为「Cookie 多行输入框」并加 `__kps`/`__uid` 存在性校验；`登录服务` 的 4 个 `短信登录_*` 方法删除，新增 `手动Cookie登录(cookie字符串)`（参考 `simple_login.py:28`）。手动 Cookie 是夸克最可靠的兜底登录方式 |

**决策影响**：三项分别落地为「删两处、改一处」，**无新增逆向工作** → 阶段一不再有阻塞项，可立即执行。

**能力边界（改造后的产品范围）**：文件浏览 / 新建文件夹 / 重命名 / 移动 / 搜索 / 上传（单文件+文件夹）/ 下载 / 回收站外删除 / 分享（创建、列表、取消、解析、转存）/ 账号容量信息 / 扫码登录 / 手动 Cookie 登录。**明确不做**：离线下载、回收站、上传记录、短信登录、埋点上报。

### 6.2 需要在阶段二用真实账号实测确认的点

1. `capacity` 接口的真实字段名（`total`/`used`/`free` vs `total_capacity` 等）——`界面/账号信息页.py` 与 `字段映射.py` 依赖它。
2. 会员/VIP 字段（光鸭有 `vipStatus`/`svipStatus`/`vipExpireTime`，夸克字段名未知）。
3. 缩略图 URL 字段（`文件接口.取缩略图URL` 目前是光鸭实现，夸克需实测）。
4. 列表返回的 `dir`/`file_type` 语义（用于区分文件夹与文件，`文件浏览页.py` 双击逻辑依赖）。
5. `share/mypage/detail` 的分页参数与返回结构（`分享页.py` 列表渲染依赖）。

### 6.3 技术风险排序

| 风险 | 等级 | 说明 | 缓解 |
|---|---|---|---|
| `X-Oss-Hash-Ctx` 增量 SHA1 | **高** | 140 行自定义哈希状态机（`file_upload_service.py:651-792`），**且参考实现本身对未知文件就不正确**（硬编码 `known_mappings` + 无 padding 的近似 SHA1）。大文件第二片起会被 OSS 拒绝 | **先实测验证**该实现能否跑通 >5MB 文件；不行则重写为真正的 SHA1 增量状态，或降级为「大文件不支持分片」并在 UI 明确提示 |
| 秒传未实现 | 中 | 参考实现忽略服务端秒传返回，等于每个文件都要真上传，流量与耗时显著上升 | 读取 `file/upload/pre` 响应中的秒传标志并短路 |
| 认证模型变更（token → cookie） | **高** | 影响 `网络客户端`/`认证`/`界面` 三层，且无 refresh 机制 | 先做凭证仓库 + 手动 Cookie 通道，最快打通链路 |
| 能力缺失导致的界面返工 | 中 | Q1/Q2/Q3 未定则无法冻结界面 | 阶段一先决策 |
| oss2 → httpx 手动分片 | 中 | 需自行处理 ETag、`X-Oss-Hash-Ctx` 头、`complete_multipart_upload` XML | 直接照搬 `file_upload_service.py:818/551` |
| 中文标识符 + 英文参考代码混用 | 低 | 移植时命名映射易错 | 严格按 §4.1/§4.2 对照表执行 |

---

## 7. 附录

### 7.1 端点对照总表

| 能力 | 光鸭端点 | 夸克端点 | 方法 |
|---|---|---|---|
| 文件列表 | `/userres/v1/file/get_file_list` | `file/sort`（`_page/_size/_sort`，总数在 `metadata._total`） | GET |
| 文件信息 | — | `file`（`fids=`） | GET |
| 建文件夹 | `/userres/v1/file/create_dir` | `file` | POST |
| 删除 | `/userres/v1/file/delete_file` | `file/delete` | POST |
| 重命名 | — | `file/rename` | POST |
| 移动 | — | `file/move` | POST |
| 搜索 | — | `file/search` | GET |
| 文件夹树 | — | `file/tree` | GET |
| 下载地址 | `/userres/v1/get_res_download_url` | `file/download` | POST |
| 预上传 | `/userres/v2/get_res_center_token` | `file/upload/pre` | POST |
| 上传授权 | `/userres/v2/get_res_center_token` | `file/upload/auth` | POST |
| 更新哈希 | — | `file/update/hash` | POST |
| 完成上传 | — | `file/upload/finish` | POST |
| 秒传检查 | `/userres/v1/check_can_flash_upload` | （并入 `file/upload/pre`） | POST |
| 任务状态 | `/userres/v1/get_task_status` | `task` | GET |
| 容量 | `/assets/v1/get_assets` | `capacity` | GET |
| 创建分享 | `/userres/v1/share_file` | `share` | POST |
| 我的分享 | `/userres/v1/get_share_list` | `share/mypage/detail` | GET |
| 取消分享 | `/userres/v1/delete_share` | `share/delete` | POST |
| 分享密码 | `/misc/v1/get_share_template` | `share/password` | POST |
| 分享 token | — | `share/sharepage/token` | POST |
| 分享详情 | — | `share/sharepage/detail` | GET |
| 转存 | — | `share/sharepage/save` | POST |
| 上传记录 | `/userres/v1/get_user_action` | — | ~~删除（Q2=A）~~ |
| 上传统计 | `/userres/v1/query_uploading_tasks_stat` | — | ~~删除（Q2=A）~~ |
| 回收站清空 | `/userres/v1/file/clear_recycle_bin` | — | ~~删除（Q2=A）~~ |
| 扫码登录 | `account.guangyapan.com/v1/auth/device/code`（设备码） | `uop.quark.cn/cas/ajax/getTokenForQrcodeLogin?client_id=532&v=1.2&request_id=<uuid>` → 轮询 `.../getServiceTicketByQrcodeToken` → `pan.quark.cn/account/info?st=<ticket>&lw=scan`（由 Set-Cookie 落 Cookie） | GET<br>GET<br>GET |
| 用户信息 | `account.guangyapan.com/v1/user/me` | `pan.quark.cn/account` | GET |
| 埋点 | `analysis-rcv.guangyapan.com/sync_data` | — | ~~删除~~ |

### 7.2 关键代码位置索引

| 用途 | 位置 |
|---|---|
| 请求封装与错误判定 | `QuarkPan-master/quark_client/core/api_client.py:80-185` |
| 公共参数/请求头 | `quark_client/core/api_client.py:54-78`、`config.py:21-47` |
| Cookie 持久化 | `quark_client/auth/login.py:33-101` |
| 扫码登录 | `quark_client/auth/api_login.py:94-466` |
| 手动 Cookie | `quark_client/auth/simple_login.py:28-152` |
| 大文件增量哈希 ★ | `quark_client/services/file_upload_service.py:651-792` |
| OSS 分片 PUT | `quark_client/services/file_upload_service.py:818-871` |
| 分片上传主流程 | `quark_client/services/file_upload_service.py:310-470` |
| 分享创建（含 task 轮询） | `quark_client/services/share_service.py:75-172` |
| 分享转存 | `quark_client/services/share_service.py:313-454` |
| 同名文件去重 | `quark_client/services/file_service.py:610-640` |
| 文件名解析缓存 | `quark_client/services/name_resolver.py:19-105` |

### 7.3 实施顺序依赖图

```
阶段一（清理）
   └─ Q1/Q2/Q3 决策 ─────────────┐
                                  ▼
阶段二（核心适配器）
   ├─ 2.1 网络层 ──► 2.2 凭证层 ──► 2.3 登录层 ──► 2.4 认证层
   ├─ 2.5 文件接口 ──► 2.6 下载 ──► 2.9 分享 ──► 2.10 路径解析
   └─ 2.7 上传（依赖 2.1/2.2/2.8）★ 关键路径
                                  ▼
阶段三（GUI 对接）──► 阶段四（依赖/文档）──► 阶段五（运行/修复）
```

---

## 8. 参考实现的已知缺陷与陷阱（**改造时勿照抄**）

QuarkPan 是"能跑通主流程"的社区实现，不是生产级代码。以下问题均已核实（`文件:行号`），本仓库实现时应**规避**而非继承。按严重度排序：

| # | 问题 | 证据 | 本仓库的对策 |
|---|---|---|---|
| 1 | **增量 SHA1 上下文不可靠**：对 4 个特定测试文件硬编码 `known_mappings`，其余文件走自写的近似 SHA1 中间状态（不做 padding） | `file_upload_service.py:683-692` | 先实测；不行则重写或降级（见 §5-2.7 / §6.3） |
| 2 | **秒传未实现**：`_pre_upload` 只判 `status` 真值，忽略服务端秒传返回；调用方只要求 `task_id` 存在 | `file_upload_service.py:208`、`:89-90` | 解析秒传标志并短路，省流量 |
| 3 | **断点续传未实现**：无 `Range`/`Content-Range`/半成品文件；失败即删 | `file_service.py:890-891` | 若要支持需自行实现；否则 README 不要宣称 |
| 4 | **请求层零重试**，且 7 个重试/分页配置常量是死代码 | `api_client.py:80-182`；`config.py:40-62` | **保留光鸭原有的连接层指数退避**（`网络客户端.py:105`），不要退化成无重试 |
| 5 | **`status: 400` 不会被请求层捕获**：只在 `status=='error'` 或 `code!=0` 时抛错 | `api_client.py:165-175` | 成功判定必须显式用 `status == 200`（int），不能只信 `code` |
| 6 | **`cookies.json` 两套互不兼容的格式**：`login.py` 写 `list[dict]`，`simple_login.py` 写 `dict`；用前者读后者会 `TypeError` | `login.py:33-43` vs `simple_login.py:152-172`；`login.py:94-105` | 自定义**单一**凭证 JSON schema，不用它的读写逻辑 |
| 7 | **配置目录是相对路径** `Path.cwd()/'config'`，随启动目录漂移 | `config.py:18` | 用 `Path(__file__).parents[2]/'数据'`（沿用光鸭做法） |
| 8 | **转存 `fid_token_list` 硬编码为空数组**，而夸克通常要求 `fid` 与 `fid_token` 成对 | `share_service.py:343` | 从 `sharepage/detail` 的 `data.list` 中读取真实 token |
| 9 | **隐藏依赖 `requests`**（函数内 import，未写进 requirements） | `file_service.py:842` | 若照搬下载实现，必须显式声明或改写为 httpx |
| 10 | **合并分片失败被静默吞掉**：仅写进 `message` 后继续 | `file_upload_service.py:457-462` | 必须向上抛出并让 UI 报错 |
| 11 | **下载实现有两套且互相矛盾**：一套用 `requests`（跟随重定向），一套用 `httpx.stream`（**httpx 默认不跟随重定向**） | `file_service.py:833` vs `file_download_service.py:233` | 统一走 `网络客户端.流式GET` 并显式 `follow_redirects=True` |
| 12 | **单分片上传把整个文件读进内存** | `file_upload_service.py:831-834` | 改为分块发送或统一走分片路径 |
| 13 | **无用的 OPTIONS 预检**（CORS 摆设） | `file_upload_service.py:613-616` | 直接省略 |
| 14 | **`DownloadError` 是死类**（定义并导出，全仓库无 raise） | `exceptions.py:47` | 本仓库不复刻 |
| 15 | **README 与代码不符**：写 `file/list` 实为 `file/sort`；写 `share/create` 实为 `share`；宣称断点续传 | `README.md:34` | 以代码为准，本文档 §7.1 已按代码校正 |
| 16 | 版本号三处不一致（`1.0.5` / `0.1.0` / `v1.0.0`） | `pyproject.toml:7`、`__init__.py:27`、`cli/main.py:287` | 与本仓库无关，仅注意别被误导 |

**认证相关的传闻校正**：网上常说的 `__puus` cookie 在 QuarkPan 全仓库**0 处命中**；实际必需字段是 `__pus`、`__kps`、`__uid`（`login.py:249,284`；`simple_login.py:139`）。手动 Cookie 登录只需包含 `__kps` + `__uid` 即可通过其本地校验。

**扫码登录的状态码**（`api_login.py:309-343`）：成功 = `status == 2000000` 且 `message == "ok"` 且 `data.data.members.service_ticket` 存在；等待 = `50004001`；失败 = `50004002/3/4`。二维码有效期默认 300s。

**任务状态语义**（`file_service.py:416-417`、`share_service.py:141-147`）：`0=排队 1=进行中 2=完成 3=失败`，轮询参数 `task_id` + `retry_index`。

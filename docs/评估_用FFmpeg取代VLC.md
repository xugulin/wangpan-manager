# 评估：用 FFmpeg 取代 VLC（可行性 / 优劣 / 代价）

> 本文**只做评估**，没有改动任何代码。
> 评估对象：<https://github.com/FFmpeg/FFmpeg>
> 结论基于：本机（KDE/COSMIC + Intel iHD + XWayland）实测、Windows 真机 CI 日志、以及仓库现状盘点。

---

## 一、先把概念摆正：FFmpeg 不是播放器

| 播放器要做的事 | 谁做（现在） | FFmpeg 提供吗 |
|---|---|---|
| 拆包 / 解复用（mp4 / mkv / flv / hls…） | libvlc | ✅ libavformat |
| 解码（H.264 / HEVC / AV1 / VP9…） | libvlc | ✅ libavcodec |
| 硬解（Windows d3d11va / Linux vaapi） | libvlc | ✅（但要自己管 surface 与互操作） |
| **把画面画进我们的控件** | libvlc（`set_hwnd` / `set_xwindow`） | ❌ **没有** |
| **音频输出**（WASAPI / PulseAudio…） | libvlc | ❌ **没有** |
| **音画同步 / 时钟 / 丢帧策略** | libvlc | ❌ **没有** |
| 跳转 / 缓冲 / 网络重连 | libvlc | ⚠️ 只有底层 seek API，策略要自己写 |
| 字幕解析与渲染 | libvlc | ⚠️ 能解文本字幕，渲染要自己做（libass） |
| 倍速、音轨/字幕轨切换、截图、统计 | libvlc | ⚠️ 部分有，大多要自己做 |

**一句话**：VLC = "播放器"（拆包+解码+渲染+音频+同步，全给你）；FFmpeg = "零件"（拆包+解码）。
所以"用 FFmpeg 取代 VLC"真正的含义是：**要么我们自己写播放器（用 libav*），要么借 Qt Multimedia
的 FFmpeg 后端来当那个播放器**。这两条路差别巨大，下面分开算。

---

## 二、两条路线 + 一个对照项

| | A. 保持 VLC（现状） | **B. Qt Multimedia 的 FFmpeg 后端（推荐评估）** | C. 自己用 libav* 写播放器 | D.（对照）libmpv |
|---|---|---|---|---|
| 本质 | libvlc 干活 | **FFmpeg 解码 + Qt 负责渲染/音频/同步** | FFmpeg 只解码，其余全自己写 | libmpv 干活（也是播放器） |
| 我们要写的量 | 0 | 中（后端适配 + HTTP 数据源 + 字幕） | **很大**（同步/渲染/音频/缓冲/恢复） | 小 |
| 画面进我们的控件 | 靠 libvlc 的嵌入 API（易踩平台坑） | ✅ Qt 自己画进 `QVideoWidget`/`QVideoSink` | 自己写（GL/D3D） | 靠 libmpv 的 `wid` 嵌入 |
| 体积（Windows 包） | **+132 MB**（内置 VLC 运行时） | **+2 MB**（Qt 的多媒体模块）+ 复用已有 FFmpeg DLL（17.9 MB，QtWebEngine 本来就要） | 同 B（或再大，自己编 FFmpeg） | +30~40 MB 左右 |
| 许可 | VLC：GPLv2+/LGPLv2.1+（我们现在随包发 DLL + COPYING） | Qt(LGPL/商业) + FFmpeg(LGPLv2.1+) —— **分发责任落在 Qt 的既有框架里** | 自己编 FFmpeg 要满足 LGPL 清单（提供源码、动态链接、about 声明…） | mpv/libmpv：LGPLv2.1+ |
| 能不能根治"画面游离/嵌入失败"这类问题 | ❌ 治不完（这几天连踩三个平台坑） | ✅ **根治**：画面是我们自己的控件渲染，不存在"VLC 自己开窗口" | ✅ 根治 | ✅ 基本根治（`wid` 也有平台差异） |
| 风险 | 低（但已修好的坑要一直维护） | **中**（Qt 后端能力边界要实测：字幕/直链请求头/硬解矩阵） | **高**（GPU 互操作、A/V 同步自己扛） | 中 |

**结论：如果要做，选 B，不要选 C。** C 的收益与 B 完全相同，但工作量和风险高一个量级 ——
"自己写播放器"这件事正是 libvlc/libmpv 存在的理由。

---

## 三、实测证据（都是刚跑出来的，不是纸面推演）

### 3.1 Qt 6.11.2 自带 FFmpeg 后端，能播 4K60 HEVC，并画进控件 ✅

`QT_MEDIA_BACKEND=ffmpeg` + `QMediaPlayer` + `QVideoWidget`，播 3840×1632 / 60fps / HEVC：

```
后端=ffmpeg｜时长=5000ms｜有视频=True｜可跳转=True
视频轨 1｜音频轨 0｜字幕轨 0
播放状态=PlayingState｜媒体状态=BufferedMedia｜帧=(3840, 1632)｜像素格式=Format_NV12
进程 CPU 时间=0.4s（含 Qt 初始化）｜错误=无
跳到 3s 后位置=4183ms          ← seek 可用
1.5 倍速后 playbackRate=1.5     ← 倍速可用
```

要点：帧拿到的是 **NV12**（GPU 友好的硬解路径），CPU 占用极低，seek/倍速/轨道 API 都齐。

### 3.2 这台机器上 FFmpeg 解 4K60 HEVC 的速度

| 方式 | 5 秒素材耗时 | user CPU |
|---|---|---|
| 软解（libx265 素材 → `-f null`） | 2.48 s（≈2× 实时） | 12.0 s（≈4.8 核） |
| VAAPI 硬解 | 2.24 s | 2.1 s（≈0.9 核） |
| 1080p30 软解 | 0.40 s（≈12× 实时） | 1.7 s |

→ 4K60 走**硬解**才舒服（软解能播但吃 4~5 核）；这一点与现在 VLC 的选择一致（VLC 也是走 vaapi/d3d11）。

### 3.3 体积账（Windows 包，实测）

| 项 | 大小 |
|---|---|
| 现在内置的 VLC 运行时（`libvlc.dll` + 354 个插件） | **132 MB** |
| Qt 的 FFmpeg DLL（**已经在包里**，QtWebEngine 要用） | 17.9 MB |
| QtMultimedia + QtMultimediaWidgets + Qt6Multimedia | 1.8 MB |

→ 切到 B 路线：**+2 MB / −132 MB ≈ 净减 130 MB**，而且**不需要新下载任何东西**。
（现在 `构建/精简运行环境.py` 把 `QtMultimedia` 和 `multimedia` 插件当"用不到的"删掉了 —— 要走 B 就得把这两项从删除名单里拿掉。）

### 3.4 我们已经是 FFmpeg 的用户

`v8_3/播放/媒体信息.py` 现在就是**调系统 `ffprobe`** 拿分辨率/码率/音字幕轨。
换成 Qt 的 FFmpeg 后端后，这些可以在**进程内**用同一套库拿到，顺带甩掉"用户机器上没装 ffprobe"的问题
（Windows 包现在没带 ffprobe，探测在 Windows 上是退化的）。

---

## 四、换成 FFmpeg（B 路线）能拿到什么

1. **根治"画面游离/嵌不进去"这一类问题** —— 这几天我们在 Windows 上连踩三个坑
   （`set_xwindow` vs `set_hwnd`、钉错平台的 `xcb_x11`、窗口就绪判定空转），
   本质都是"把画布交给第三方播放器、它有一套自己的平台规则"。画面由 Qt 画进我们的控件后，
   **这一类问题从设计上不存在**。
2. **平台一致性**：Windows/Linux 同一套渲染与音频路径，CI 的验证面收窄（真机播放测试照样能用）。
3. **包体净减约 130 MB**，启动更快，不再需要随包分发 VLC 的插件树与许可证文件。
4. **控制力更强**：帧精确 seek、缩略图/进度预览、逐帧步进、HDR/色彩管理、
   精确丢帧与码率统计（现在只能问 libvlc 要 stats）、多路字幕自绘。
5. **技术栈收敛**：Qt + FFmpeg 一套；`播放出口/播放会话` 这层归口已经做好，
   换后端的面是**收敛**的（这是可行性加分项，见第六节）。

## 五、代价与风险（必须如实说）

| 风险 | 说明 | 缓解 |
|---|---|---|
| **自定义 HTTP 请求头** | 网盘直链要 UA/Referer/Cookie，而 `QMediaPlayer` **没有**传自定义头的 API | 我们**自己用 QIODevice 喂数据**（已有 httpx + Range + 直链探测 + 多线程下载能力）；顺带把缓存/重试/流量统计握在自己手里 |
| **字幕** | Qt 有 `subtitleTracks/activeSubtitleTrack`，但**渲染**（尤其 ASS 特效）要实测，可能得自绘 | SRT 自绘很简单（我们有 AI 生成字幕的 SRT）；ASS 可先用"内嵌样式简化"降级 |
| **硬解互操作** | Windows d3d11va / Linux vaapi→GL 依赖 Qt 的 FFmpeg 构建与驱动，仍有机器相关性 | 已有的真机矩阵（Windows CI + 本机 Linux）继续跑；保留"软解兜底" |
| **能力缺口** | 章节、下一帧步进、VLC 的网络缓存参数、VLC stats | 章节：基本用不到；下一帧：暂停+seek 模拟；缓存/丢帧：自己统计 |
| **QA 回归成本** | 已修好的播放行为（对齐/交接/全屏/崩溃/自愈/AI 调参/截图提示）都要在双后端下重测 | 先双后端灰度，VLC 当保底；沿用 `工具/测试播放嵌入.py` 与 `工具/界面自检.py` |
| **许可** | 走 B 用 Qt 自带的 FFmpeg：分发在 Qt 的既有许可框架内（我们本来就用 PySide6/LGPL，也随包带 VLC 的 COPYING）。若走 C 自己编 FFmpeg：必须满足 [FFmpeg 官方的 LGPL 清单](https://ffmpeg.org/legal.html)（不给 GPL 组件、动态链接、随包提供 FFmpeg 源码与编译说明、关于页/网站上声明） | 走 B；无论哪条都保留"本软件使用 FFmpeg（LGPLv2.1）"的声明 |
| **专利** | H.264/HEVC 专利在部分司法辖区对**商业产品**有要求（FFmpeg 官方 legal 页明确提示）——**这一点现在用 VLC 也一样存在**，不是换 FFmpeg 带来的新问题 | 保持现状口径（软件免费、源码公开、免责声明） |

## 六、工作量估算（人日，双后端 + B 路线落地）

| 工作项 | 估算 | 备注 |
|---|---|---|
| 抽出"播放后端"接口（把 `播放出口/播放会话` 背后的 libvlc 变成可替换实现） | 3–5 | 我们已经把播放归口，接口面清晰 |
| Qt+FFmpeg 后端 MVP：本地播放/暂停/跳转/音量/进度/截图 | 4–6 | 3.1 的 Spike 已验证基本可行 |
| 网盘直链：自定义头 + Range + QIODevice + 缓冲/重连 | 5–8 | 难点，但可复用现有直链/下载代码 |
| 字幕（SRT 自绘 + ASS 评估） | 3–5 | |
| 硬解与 4K60 调优（Windows d3d11va / Linux vaapi、尺寸/对齐/DPI） | 4–8 | 风险最高的一块 |
| 统计 / 自愈 / AI 自动调参对接（丢帧、码率、规则） | 3–4 | 现有逻辑基本可复用 |
| 真机矩阵验证（Windows CI + Linux 真机 + 各分辨率/编码） | 4–6 | 判据现成 |
| **合计** | **约 26–42 人日** | C 路线（自研 libav*）约 ×1.5–2.5 |

## 七、建议的推进方式（分阶段、可回退）

1. **Spike 阶段（不改正式代码）**：把 3.1 的脚本升级成"跑在 Windows CI 上"的验证件，另加两项：
   ① 网盘真直链 + 自定义头 + QIODevice 能不能喂进 `QMediaPlayer`；② 字幕（SRT/ASS）到底能不能出画面。
   —— 这两项是 B 路线的**生死项**，先证伪再投入。
2. **双后端阶段**：新增 `播放后端` 抽象，VLC 后端保持现状、FFmpeg 后端并行；设置页给一个开关
   （默认仍是 VLC），用现有真机测试跑对比。
3. **灰度阶段**：**Windows 先默认 FFmpeg**（那边嵌入坑最多），Linux 观察；任何一项不达标就一键回退 VLC。
4. **收尾阶段**：达标后移除 VLC 运行时（包体 −130 MB）、更新许可声明与文档。
5. **验收判据（现成）**：`工具/测试播放嵌入.py`（有画面 + 画面在我们窗口里 + 无游离窗口）、
   `工具/界面自检.py`（510 项）、以及真机 4K60 起播/seek/暂停继续/独立窗口/全屏矩阵。

## 八、明确的建议

- **不建议**为了"修当前的 Windows 游离窗口"去换 FFmpeg —— **那个 bug 在 V1.0.13 已经修好了**
  （`set_hwnd` + Windows 不钉 `xcb_x11` + 就绪判定补 Windows 实现），并且加了真机播放测试兜底。
- **建议**把"换 FFmpeg"当作 **V1.1 的架构收益项**来评估：净减 130 MB、根治一整类平台坑、
  拿到帧级控制权。**先做 Spike（生死项：直链请求头 + 字幕渲染）**，通过再排期。
- **不要**走"自己用 libav* 写播放器"（C 路线）：收益与 B 相同，工作量和风险高一个量级。

---

### 附：本次评估用到的实测命令/脚本（都在 /tmp，没有进仓库）

- Qt+FFmpeg 试播：`QT_MEDIA_BACKEND=ffmpeg` + `QMediaPlayer` + `QVideoWidget`，素材 3840×1632 HEVC（见 3.1）
- 解码性能：`ffmpeg -i hevc4k60.mp4 -f null -`、`ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD128 …`
- 体积：`du -sh 构建/windows/vlc`（132 MB）、`PySide6/avcodec-61.dll` 等（17.9 MB）

### 参考

- Qt 6 QMediaPlayer API（轨道/seek/倍速/缓冲）：<https://doc.qt.io/qt-6/qmediaplayer.html>
- Qt Multimedia 硬件加速（FFmpeg 后端）：<https://deepwiki.com/qt/qtmultimedia/3.3-hardware-acceleration>
- FFmpeg 许可与 LGPL 清单（官方）：<https://ffmpeg.org/legal.html>
- FFmpeg 项目：<https://github.com/FFmpeg/FFmpeg>
- libmpv（对照项，播放器库）：<https://mpv.io/manual/master/#libmpv>；Wayland 下 `wid` 嵌入限制讨论：<https://github.com/mpv-player/mpv/issues/9654>

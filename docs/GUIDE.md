# omnilimb-face 使用文档

把 [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) 的核心能力
——**语音免提交互、实时打断、Live2D / Live3D 形象 + 声波可视化**——作为一个
**标准 hermes-agent 插件**交付。插件只通过 `register(ctx)` 扩展面集成,**不修改任何
hermes 核心文件**。

> 你在 `hermes` 终端里正常聊天,浏览器里的形象会用**你已配置的模型**生成的真实回复
> 来「说话」(对口型 + 切表情),并且**你一开口就能打断它**。

---

## 目录
1. [它能做什么](#1-它能做什么)
2. [架构总览](#2-架构总览)
3. [安装](#3-安装)
4. [启用与配置](#4-启用与配置)
5. [命令大全(CLI / 斜杠 / 工具)](#5-命令大全cli--斜杠--工具)
6. [形象渲染:Live2D / Live3D / 声波 Orb / 版式](#6-形象渲染live2d--live3d--声波-orb--版式)
7. [语音免提与实时打断(barge-in)](#7-语音免提与实时打断barge-in)
8. [前端界面说明](#8-前端界面说明)
9. [独立预览(无需 hermes)](#9-独立预览无需-hermes)
10. [故障排查](#10-故障排查)
11. [开发与测试](#11-开发与测试)

---

## 1. 它能做什么

- **给 agent 一张脸 + 一把声音**:hermes 的每条回复会被插件截获,切句后做 TTS 语音合成,
  驱动屏幕上的形象对口型、切表情。
- **两种可切换形象**:**Live2D**(2D Cubism 模型)与 **Live3D**(VRM 3D 人形),运行时一键切换,
  不重载页面。
- **常驻声波球(Orb)**:主形象旁边一块独立可视化,跟着口型律动、随对话状态(空闲/聆听/思考/说话)
  变色换角标。**不会覆盖主形象**。
- **语音免提(hands-free)**:麦克风 VAD 自动分句 → STT 转写 → 注入 hermes 一轮对话。
- **实时打断(barge-in)**:形象正在说话时,你一开口(≥200ms 连续语音)就立刻停掉它的语音、
  中止这轮回复,并开始录你的新一句。
- **优雅降级**:缺可选依赖(麦克风 / 前端服务栈)时插件仍会注册,工具仍在 `hermes tools` 里可见,
  只是相应能力标记为不可用——文字对话与形象渲染始终可用。

---

## 2. 架构总览

```
                                hermes-agent (host)
  ┌───────────────────────────────────────────────────────────────────────┐
  │  register(ctx)  ← 插件入口,只用 ctx 扩展面注册:                          │
  │     hooks: on_session_start / on_session_end /                          │
  │            transform_llm_output / post_llm_call                         │
  │     tools: vtuber_status / vtuber_say                                   │
  │     CLI:   hermes vtuber …      slash: /vtuber  /handsfree              │
  └───────────────────────────────────────────────────────────────────────┘
        │ 麦克风                          ▲ 回复文本(hook 截获,从不自己调模型)
        ▼                                 │
   VAD 分句 → STT 转写 → ctx.inject_message → 宿主正常一轮(你的模型/工具/记忆)
                                                  │ reply text
                                                  ▼
                              切句(Sentence_Chunker)→ TTS 合成 + 表情映射
                                                  │
                                                  ▼
                           Live2D_Director ──→ /client-ws 网关(:12393)
                                                  │  audio / set-model / control 事件
                                                  ▼
              前端静态服务(:12394)托管的页面 ── WebSocket ──→ 浏览器渲染形象
                                                                (Live2D/Live3D + Orb)
```

**两个本地监听端口**(默认,均绑定 `127.0.0.1`):

| 端口 | 用途 |
|---|---|
| `12393` | `/client-ws` WebSocket 协议网关(浏览器 ↔ 插件) |
| `12394` | 前端静态资源服务(`= 协议端口 + 1`) |

**关键设计点**
- 插件**不携带自己的模型配置**:转写经 `ctx.inject_message` 注入,回复经
  `transform_llm_output` / `post_llm_call` 两个 hook 读取——所以形象说的永远是你配置的 agent 的真实回复。
- 前端是**无构建步骤的纯 JS**(window 全局 + `<script>` 标签),作为 **package data** 打进
  `omnilimb_face/frontend/`,随 wheel 一起安装。
- `/client-ws` 协议兼容 `websockets` 12.x 与 13–15.x,可直接复用宿主的 websockets。

---

## 3. 安装

要求 **Python 3.11+**。

### 最快上手(一条命令装齐 + 分步)

**方式 A —— 一分钟预览(无需 hermes):**
```bash
pip install omnilimb-face          # ① 一条命令装齐(形象 + 语音输入/输出 + 打字交互)
omnilimb-face                      # ② 启动:同时提供网页 + 网关,并自动打开浏览器
#                                    ③ (没自动打开就)访问  http://127.0.0.1:12394/   (不是 12393!)
```

**方式 B —— 完整形态(在 hermes 里聊天):**
```bash
<hermes-venv>/python -m pip install -e "path/to/omnilimb-face[all]"   # ① 装进 hermes venv
# ② 在 ~/.hermes/config.yaml 的 plugins.enabled 里加 `omnilimb-face`
hermes                           # ③ 运行 hermes(自动起网关 12393 + 前端 12394)
hermes vtuber status             #    检查子系统
#                                  ④ 打开 http://127.0.0.1:12394/ ,在终端聊天
```

> ⚠️ 网页在 **12394**;**12393** 是 WebSocket 网关,用浏览器直接开 12393 **打不开**。
> 没声音 → 多半没装 `edge-tts`(`[all]` 已含;或单独 `pip install edge-tts`),且需联网 +
> 先点一下页面(浏览器自动播放限制)。详见 §10。

### 其它安装方式

#### A. pip 安装(进入 hermes 的 venv)
```bash
<hermes-venv>/python -m pip install -e path/to/omnilimb-face --no-deps
```

#### B. 可编辑安装(开发)
```bash
pip install -e ".[dev]"        # 核心 + 测试工具
```

#### C. 目录插件
把整个 `omnilimb-face/` 文件夹放到 `~/.hermes/plugins/omnilimb-face/`(需含 `plugin.yaml` + 根 `__init__.py`)。

### 可选依赖(extras)
| extra | 增加的能力 | 缺失时 |
|---|---|---|
| **`all`** | **一次装齐下面全部**(voice + wakeword + live2d + preview) | —— 推荐普通用户用这个 |
| `voice` | 麦克风采集 + VAD(sounddevice / webrtcvad / numpy) | 免提不可用,文字仍可用 |
| `wakeword` | 唤醒词激活(openwakeword) | 唤醒词禁用 |
| `live2d` | 基于 starlette/uvicorn 的前端服务(可选;核心用 stdlib 也能serve) | 用 stdlib `http.server` 兜底 |
| `preview` | 独立预览的 edge-tts(语音) + faster-whisper | 预览退化为静音合成口型(**没声音**) |
| `test` / `dev` | pytest + hypothesis | — |

```bash
pip install -e ".[all]"        # 推荐:一次装齐
pip install -e ".[voice]"      # 或:只加麦克风采集
```

> **核心安装刻意不拉取** voice / Live2D 栈,以保证缺失时仍能以**降级**状态注册;
> 想要开箱即用(含声音)就用 `".[all]"`。

---

## 4. 启用与配置

### 启用
插件是**按需启用**的。在 `~/.hermes/config.yaml` 的 `plugins.enabled` 列表里加入插件名:

```yaml
plugins:
  enabled:
    - omnilimb-face
```

### 配置位置
- **非密钥设置**只从 `config.yaml` 读;插件自有项在 `plugins.entries.omnilimb-face` 下,
  STT/TTS 复用顶层 `stt` / `tts` 段。
- **密钥**(API key 等)只从 `.env` 读,永不进入配置对象。
- 合并是纯函数:缺项 → 用默认值;类型不对 → 记一条告警并回退默认,继续合并其余项。

### 完整配置参考

```yaml
# —— 复用 hermes 顶层段 ——
stt:
  enabled: true
  provider: local              # 复用 hermes 的 stt.provider
  model: base
  language: ""                 # 语言提示,空=自动
  transcribe_timeout_s: 10.0

tts:
  provider: edge               # 复用 hermes 的 tts.provider
  edge:                        # 注意:voice/model 是 provider 作用域(tts.<provider>.voice)
    voice: en-US-AriaNeural    # 例:中文用 zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural
    model: ""
  synth_timeout_s: 10.0        # 每次合成超时
  max_attempts: 3              # 首次 + 2 次重试

# —— 插件自有项 ——
plugins:
  entries:
    omnilimb-face:
      vad:
        silence_threshold_s: 2.0      # 静音多久判定一句结束(0.5–10)
        max_record_s: 60.0            # 单句最长录制
        barge_in_min_speech_ms: 200   # 触发打断所需的最短连续语音
        sample_rate: 16000
        frame_ms: 20
      wake_word:
        enabled: false                # 唤醒词默认关
        phrase: "hey hermes"
        confidence_threshold: 0.7     # 0.0–1.0
        listen_timeout_s: 3.0
      live2d:
        model_name: default           # 从 model_dict.json 里取哪个模型
        model_dict_path: models/model_dict.json
        default_expression: neutral
        target_fps: 30
      protocol:
        host: 127.0.0.1
        port: 12393                   # 前端服务自动用 port+1 (12394)
        ws_path: /client-ws
        max_message_bytes: 1048576    # 1 MiB 上限
      interruption:
        enabled: true                 # ← 实时打断总开关(见 §7)
```

> TTS 的 `voice`/`model` 是 **provider 作用域**:写在 `tts.<provider>.voice`(如 `tts.edge.voice`、
> `tts.openai.voice`),**不是** 扁平的 `tts.voice`,以对齐宿主 `tools/tts_tool.py` 的布局。

---

## 5. 命令大全(CLI / 斜杠 / 工具)

插件通过 `register(ctx)` 注册的全部对外操作面:

### 5.1 终端子命令 `hermes vtuber`
在 hermes 仓库目录下运行:

| 命令 | 作用 |
|---|---|
| `hermes vtuber start` | 启动形象 UI + 语音回路(起 `/client-ws` 网关 + 前端服务;缺语音依赖则降级为纯文本) |
| `hermes vtuber stop` | 停止上述子系统 |
| `hermes vtuber status` | 查看运行状态、是否降级、缺哪些可选依赖(**默认动作**,不带参数即 status) |
| `hermes vtuber doctor` | 诊断:依赖、端口、麦克风、前端目录等 |

启动后浏览器打开 `http://127.0.0.1:12394/`,在 hermes 终端里聊天即可看到形象说话。

### 5.2 会话内斜杠命令(CLI 与各 gateway 通用)

| 斜杠命令 | 作用 |
|---|---|
| `/vtuber [start\|stop\|status]` | 与 `hermes vtuber …` 行为一致(空参=status,也接受 `doctor`) |
| `/handsfree [on\|off]` | 开/关语音免提(麦克风)。空参=报告当前状态。缺 `[voice]` 依赖或无麦克风时,会明确告诉你免提不可用、但文字与形象仍可用 |

### 5.3 给 agent / 模型用的工具(toolset `vtuber`)

这两个工具**即使降级也始终注册**,所以在 `hermes tools` 里始终可见;它们的 `check_fn` 反映可用性。

| 工具 | 参数 | 作用 |
|---|---|---|
| `vtuber_status` | 无 | 报告插件子系统状态(运行中 / 降级 / 缺失的可选依赖) |
| `vtuber_say` | `text: string` | 让形象把这段文字说出来(TTS + 对口型 + 表情) |

> 你可以直接对 hermes 说「用 vtuber_say 说一句你好」,模型会调用该工具驱动形象开口。

---

## 6. 形象渲染:Live2D / Live3D / 声波 Orb / 版式

打开页面右上角 **⚙ 设置 → 外观** 标签:

### 6.1 切换渲染器(Live2D ↔ Live3D)
- **渲染器**下拉:`Live2D` / `Live3D`,选中即**运行时切换,不重载页面**;失败会自动回退到上一个渲染器。
- **Live2D**:加载 Cubism 模型(模型 URL 来自 `model_dict.json` 的条目);支持对口型 + 表情。
- **Live3D**:加载 VRM 3D 人形(VRM 源由后端通过 `set-model-and-conf` 的 `vrm_url` 附加字段下发);
  支持口型 blendshape + 表情 preset + 待机/眨眼。
- 选择会持久化到 `localStorage`,下次自动恢复;非法/缺失值归一化为 `Live2D`。
- 依赖缺失时的**降级链**:Live2D → CanvasAvatar(无依赖的 2D 占位)→ 纯文本/语音(对话继续)。

### 6.2 声波球(Orb)
- 主形象**旁边**常驻的一块独立画布,渲染一个粒子球。
- **不参与渲染器切换、不覆盖主形象**;由与主形象**相同**的口型值 + 对话状态驱动:
  说话时粒子沿径向律动,状态变化时变色换角标(空闲=蓝 / 聆听=绿 / 思考=橙 / 说话=粉)。
- 依赖 three.js;若 three.js / WebGL 不可用则静默跳过,主形象不受影响。

### 6.3 版式切换(topbar)
顶栏中间的分段控件,两种排版(默认**剧场**):

| 版式 | 布局 |
|---|---|
| **剧场**(默认) | 主形象放大居中,声波球缩小浮在右下角 |
| **纯形象** | 只显示主形象,隐藏声波球 |

纯 CSS 切换,不重建渲染器;选择持久化。

### 6.4 换模型
编辑 `omnilimb_face/frontend` 同级的 `models/model_dict.json`(Open-LLM-VTuber 风格:
`name` / `url` / `emotionMap`),或改 `live2d.model_name` 指向其中的条目。Live3D 的 VRM 源可由
后端 `vrm_url` 下发(预览里用 `--vrm-url` 指定)。

> 🎭 **表情·动作·口型到底怎么绑定、怎么导入新模型(Cubism / VRM)、以及 2D+3D 深度融合开发方向,
> 见专门文档 [`AVATAR_INTEGRATION.md`](AVATAR_INTEGRATION.md)。**

---

## 7. 语音免提与实时打断(barge-in)

### 7.1 语音免提(hands-free,桌面)
- 开启:页面底栏 **🎤 免提** 按钮,或斜杠 `/handsfree on`,或设置里「启动时自动开麦」。
- 流程:麦克风采集 → 能量/VAD 分句(静音 `vad.silence_threshold_s` 判一句结束)→ STT 转写 →
  作为用户输入注入 hermes 一轮对话 → 形象用回复说话。
- 提示:**戴耳机**,否则麦克风会听到形象自己的声音造成自我打断/自我转写。
- 仅在 `http://127.0.0.1` 或 HTTPS 下浏览器才允许麦克风;纯 http 的局域网 IP 会被浏览器拦截麦克风
  (文字聊天与观看仍正常)。

### 7.1b 按住说话(push-to-talk,手机 / 触摸设备)
手机上持续免提体验差,**触摸设备 / 窄屏会自动把「免提」替换为「🎤」按住说话**(对讲机式,纯图标整行长条):
- **按住**按钮 → 开始录音(变红),**松开**(或手指滑出)→ 把这一段作为一句话送去 STT。
- 按下即触发**打断**:停掉形象正在播放的语音并通知后端中止本轮。
- **麦克风需要安全上下文**:手机经局域网 **http** 访问会被浏览器拦截麦克风(按钮置灰)。
  在手机上用麦克风的最简单方式 → 见 §9 的 **`--https`(单端口)**:一个端口、一张证书,接受一次即可。

### 7.2 实时打断(barge-in)— **当前已支持**
形象正在说话时,你**一开口就能打断它**:

- 触发条件:免提开启 + 检测到**连续语音 ≥ `vad.barge_in_min_speech_ms`(默认 200ms)**。
- 触发后(在确认→停止的 ~300ms 预算内):
  1. **立即停止** TTS 语音播放;
  2. **中止**正在生成的这轮宿主回复;
  3. **重置**分句器,立刻开始录你的新一句。
- **总开关**:`interruption.enabled`(默认 `true`)。设为 `false` 则永不打断播放。
- **灵敏度**:调 `vad.barge_in_min_speech_ms`(越小越灵敏,越容易被环境噪声误触发)。
- 浏览器端:前端的「实时打断(说话即打断形象)」开关也会在你开口时立即停掉本地音频播放并发
  `interrupt-signal`。
- **故障安全**:若打断期间麦克风/VAD 出错,会拆除检测、**保持当前播放不被打断**,并提示
  「barge-in 不可用」,其余功能不受影响。

---

## 8. 前端界面说明

页面顶栏:标题 · **版式切换(剧场/纯形象)** · 连接状态徽标。
底栏:文本输入(回车发送)· **🎤 免提** · **Interrupt(打断)** · **⚙ 设置**。

**⚙ 设置**弹窗标签页:
- **语音**:STT 模型(准确率 vs 速度)、麦克风灵敏度阈值、启动自动开麦;TTS 音色、语速。
- **灵魂人格**:形象自己的身份/性格(作为它的系统提示注入,**与 hermes 本体设定相互独立**),
  保存后用新人设重建对话。
- **外观**:渲染器(Live2D/Live3D)、形象大小、背景(默认/透明/绿幕/蓝幕)、字幕/日志开关。
- **交互**:实时打断开关。
- **状态**:运行时遥测——渲染器类型、模型/来源、STT/TTS、连接状态、网络延迟(ping/pong)、智能体状态。

### 自适应 / 一屏显示(平板 · 手机)
页面是**应用式一屏布局**:整页不滚动(`100dvh`),topbar / 舞台 / 控制区各自伸缩,事件日志在区域内部滚动。
- 形象尺寸按**可用空间**自适应:桌面/平板放大,手机竖屏按宽度缩放,横屏/矮窗口按高度缩放;
  画布用 `object-fit: contain` 等比居中,**任何屏比例下都不拉伸变形**(非正方形外框处 letterbox)。
- 手机竖屏:版式切换条移到第二行居中,日志缩短;**矮屏/横屏**(高度 ≤600px)自动隐藏日志与表情标签,
  优先保证「形象 + 字幕 + 输入框」一屏可见。
- 已验证:桌面 1440×900、平板 768×1024、手机竖屏 390×844、手机横屏 844×390 均**一屏不滚动**且形象不变形。
- 注:手机上麦克风仅在 `127.0.0.1` 或 HTTPS 下可用(见 §7);局域网 http 只能文字/观看。

---

## 9. 独立预览(无需 hermes)

想**不依赖 hermes** 就看到形象渲染、对口型、切表情、并真正出声,可用插件根目录的预览启动器:

```bash
python preview.py                 # 起前端 + /client-ws 网关,打开浏览器
python preview.py --no-browser --llm --stt --lan
```

常用参数:
- `--llm`:用你**真实的 hermes**(经 `hermes -z` 一轮)生成回复,让形象说真实答案(需 `--hermes-python`/`--hermes-dir` 指向 hermes venv)。
- `--stt`:开启本地 faster-whisper 服务端转写(浏览器送麦克风 PCM)。
- `--voice zh-CN-XiaoxiaoNeural`:edge-tts 音色(无密钥,需联网)。
- `--vrm-url <url>`:Live3D 用的 VRM 模型。
- `--lan`:绑 `0.0.0.0` 让局域网其它设备能打开页面。
- `--single-port`:页面与 `/client-ws` **同一端口/同一来源**(默认是页面在 `端口+1`)。手机只需信任一张证书、可走单条隧道。
- `--https`:用自签证书走 **HTTPS/WSS**(**自动启用单端口**),让手机在局域网获得安全上下文、能用麦克风。需 `cryptography`(在 `.[preview]` 里)。`--no-tts`:禁用 edge-tts,用静音合成口型。

#### 在手机上用麦克风(推荐姿势)
```bash
python preview.py --no-browser --llm --stt --lan --https
```
- 手机连**同一 Wi-Fi**,开打印出来的 **`https://<局域网IP>:12393/`**(注意:单端口模式下页面就在网关端口 12393,**不是** 12394;用 192.168.x 那个真实 Wi-Fi 地址,别用 172.x 虚拟网卡地址)。
- 首次有自签证书警告 → 点「高级 → 继续」**一次**(单端口只需信任这一张证书)。
- 页面加载后即为安全上下文 → **按住「🎤」说话**。
- 想要手机零证书警告 / 公网可用:把这一个端口用隧道暴露,例如
  `cloudflared tunnel --url https://localhost:12393`(单端口同源,隧道一条即通)。

> 预览是**开发工具**:默认不接 hermes 的 LLM/TTS/麦克风。要完整体验请安装插件并 `hermes vtuber start`。

---

## 10. 故障排查

| 现象 | 原因 / 解决 |
|---|---|
| **`http://127.0.0.1:12393/` 打不开** | 12393 是 WebSocket **网关**,不是网页。请开 **`http://127.0.0.1:12394/`**(网关端口 **+1**)。单端口模式(`--single-port`/`--https`)下网页才在 12393。 |
| **没有声音** | 没装语音引擎。`pip install edge-tts`(或 `pip install -e ".[all]"`);预览显示 `voice: on (edge-tts …)` 才算启用。另需联网(Edge 在线语音)+ 先在页面点一下/打字(浏览器自动播放限制)。 |
| **装完没反应 / 看不到形象** | 安装只装代码,还要**启动**:`python preview.py`(方式 A)或 `hermes vtuber start`(方式 B),再开 `http://127.0.0.1:12394/`。 |
| 页面一直「连接中…」、形象空白 | 多半是**代理 / VPN / 加速器**拦了本地 WebSocket(网关回 426)。把 `127.0.0.1` 和 `localhost` 加入代理的「绕过/直连」,或临时关代理后 **Ctrl+Shift+R** 硬刷新。 |
| 改了前端却没生效 | 前端服务发的是 `no-store` 头,但浏览器/页面可能缓存;**Ctrl+Shift+R** 硬刷新。 |
| `/handsfree on` 提示不可用 | 缺 `[voice]` 依赖或没枚举到麦克风。装 `pip install -e ".[voice]"`;文字与形象不受影响。 |
| 局域网 IP 打开麦克风用不了 | 浏览器只在 `127.0.0.1` 或 HTTPS 下放行麦克风;局域网 http 只能用文字/观看。 |
| 端口冲突 | 改 `protocol.port`(前端自动用 `port+1`)。 |
| 形象不说话但有字幕 | 宿主无 `text_to_speech` 工具时插件用 Edge-TTS 兜底——把 `edge-tts` 装进 hermes venv:`<hermes-venv>/python -m pip install edge-tts`。 |
| 想确认子系统状态 | `hermes vtuber doctor` / `hermes vtuber status` / 工具 `vtuber_status`。 |
| Live3D 黑屏/报错 | three.js / WebGL 不可用或 VRM 加载失败;会在画布上显示失败原因,并可降级回 Live2D。 |

诊断命令:
```bash
hermes vtuber doctor      # 依赖 / 端口 / 麦克风 / 前端目录
hermes vtuber status      # 运行状态 + 降级原因
```

---

## 11. 开发与测试

### 后端(Python)
```bash
.venv/Scripts/python -m pytest -q              # 单元 + 属性 + 集成测试
HYPOTHESIS_PROFILE=ci pytest                   # 更重的属性测试
```

### 前端(纯 JS,jsdom + node:test + fast-check)
```bash
cd omnilimb_face/frontend/tests
node --test                                    # 渲染器接口/切换/降级/状态面板/版式等
```

### 打包(资源随包发布)
```bash
python -m build            # 产出 sdist + wheel;前端资源作为 package-data 一并打入
```
前端运行时资源(`*.html`/`*.js`/`*.css`/`*.md`)通过 `[tool.setuptools.package-data]`
打进 `omnilimb_face/frontend/`;JS 测试目录被排除,不进发行包。

### 目录结构
```
omnilimb-face/
├── pyproject.toml          # 打包 / 固定依赖 / 可选 extras / 入口点
├── plugin.yaml             # PluginManifest(name/version/hooks/tools)
├── __init__.py             # 目录发现兜底:re-export register
├── preview.py              # 独立预览启动器(开发用)
├── models/model_dict.json  # Live2D 模型字典
├── omnilimb_face/          # 插件包
│   ├── plugin.py           # register(ctx) 入口
│   ├── runtime.py          # VTuberRuntime(hook/tool/CLI/slash/生命周期)
│   ├── config.py           # ConfigManager / VTuberConfig
│   ├── interruption.py     # 实时打断控制器(barge-in)
│   ├── stt.py / tts.py / chunker.py / expression.py / live2d.py / llm_bridge.py
│   ├── frontend_server.py  # 前端静态服务(stdlib http.server)
│   ├── frontend/           # ← 前端资源(package data):index.html / app.js /
│   │                       #   renderer-manager / live3d-renderer / orb-renderer /
│   │                       #   protocol / status-panel / styles.css / tests/
│   ├── voice/              # capture / vad / wake_word
│   └── protocol/           # /client-ws 事件模型 + 网关
└── tests/                  # pytest + Hypothesis(单元 / 属性 / 集成)
```

---

## 许可

以 **AGPL-3.0-or-later** 发布,另提供**商业双授权**(闭源/专有商用见
[`COMMERCIAL-LICENSE.md`](../COMMERCIAL-LICENSE.md),联系 yase19636404@163.com)。
前端运行时从 CDN 加载 Live2D Cubism Core(专有,未打包)与 three.js / three-vrm;
离线或加载失败时自动降级到无依赖的占位渲染。详见仓库根的 `LICENSE` / `NOTICE.md`。

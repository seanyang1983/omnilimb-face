# omnilimb-face

![omnilimb-face](docs/assets/banner.svg)

[English](README.md) · **中文**

一个独立、可安装的 [hermes-agent](https://github.com/NousResearch) 插件,给你的
智能体一张**会说话的脸**:语音免提交互(VAD + STT)、实时打断(barge-in)、以及带
口型同步与表情驱动的 Live2D / Live3D 形象 —— 全程**不修改 hermes 任何核心文件**,
仅通过 `register(ctx)` 扩展面集成。隶属 [omnilimb](https://github.com/seanyang1983/omnilimb) 家族。

## 演示

上方是虚拟形象,会带着口型与表情说出智能体的回复;下方是对话框,你可以打字(或说话)。

![omnilimb-face 演示](docs/assets/demo.gif)

> 上图为合成预览(`python preview.py`)效果;静态截图见
> [`docs/assets/screenshot.png`](docs/assets/screenshot.png)。
> 形象:Live2D Cubism 示例 **"Mao" © Live2D Inc.**(从 CDN 加载,不打包 ——
> 见[致谢](#致谢与第三方授权))。

> 📖 **完整文档见 [`docs/GUIDE.md`](docs/GUIDE.md)** —— 介绍 / 架构 / 安装 / 配置 /
> 命令(CLI·斜杠·工具)/ Live2D·Live3D 切换 / 实时打断(barge-in)/ 故障排查 / 开发测试。
>
> 🎭 **形象深度整合见 [`docs/AVATAR_INTEGRATION.md`](docs/AVATAR_INTEGRATION.md)** ——
> Live2D/Live3D 表情·动作·口型**怎么绑定**、**怎么导入新模型**(Cubism / VRM),以及
> 后续 2D+3D **深度融合开发**方向。

## 工作原理

插件复用 hermes 现有系统,不自带模型配置:转写经 `ctx.inject_message` 注入,宿主
智能体的正常轮次产生回复(用你当前的模型、工具与记忆),再通过 `transform_llm_output`
/ `post_llm_call` 钩子拦截回复文本来驱动 TTS 和形象。语音转写复用 `stt` 配置段,
语音合成复用 `tts` 配置段。插件**从不自己调用 LLM**,所以形象说出的永远是你所配置
智能体的真实回答。

![架构图](docs/assets/architecture.svg)

## 安装

需要 Python 3.11+。

```bash
# 在插件目录、虚拟环境中:
pip install -e ".[dev]"        # 核心 + 测试工具
pip install -e ".[voice]"      # 麦克风采集 + VAD
pip install -e ".[wakeword]"   # 可选唤醒词
pip install -e ".[live2d]"     # 前端静态资源服务
```

**核心**安装刻意不引入语音/Live2D 包。当这些可选 extras 缺失时,插件仍会以**降级**
状态注册,其工具在 `hermes tools` 中依然可见。

### 启用

通过 `hermes_agent.plugins` pip 入口点发现,或把目录放到
`~/AppData/Local/hermes/plugins/omnilimb-face/`。在 `config.yaml` 的 `plugins.enabled`
中加入 `omnilimb-face` 即可启用。

### 完整形态:在 hermes 里聊天,形象说出智能体的回复

```bash
# 1. 把插件装进 hermes 的 venv(可编辑安装,不改依赖)
<hermes-venv>/python -m pip install -e path/to/omnilimb-face --no-deps
# 2. 语音后端:hermes 没有 text_to_speech 工具,插件用无密钥的 Edge-TTS 兜底。
#    装进 hermes 的 venv:
<hermes-venv>/python -m pip install edge-tts
# 3. 启用插件:在 ~/.hermes/config.yaml 的 plugins.enabled 里加 `omnilimb-face`
# 4. 运行 hermes;它会起 /client-ws 网关(12393)+ 前端(12394)
hermes
hermes vtuber status      # 检查形象子系统是否就绪
# 5. 浏览器打开 http://127.0.0.1:12394/,然后在 hermes 终端里聊天 —— 形象会逐句说出回复。
```

合成优先用宿主的 `text_to_speech` 工具,没有则用 Edge-TTS 兜底(默认中文音色,当
`tts.<provider>.voice` 是 Edge `*Neural` 音色时可配置)。`/client-ws` 网关同时兼容
websockets 12.x 与 13–15.x。

### 预览形象(无需 hermes-agent)

```bash
python preview.py            # 启动前端 + /client-ws 网关并打开浏览器
```

打开 http://127.0.0.1:12394/ 并加载参考模型;打字会驱动一段合成的口型 + 表情演示
(无真实 TTS 音频)。完整的语音 + 形象体验请启用插件并运行 `hermes vtuber start`。
换用自有模型的方法见 `omnilimb_face/frontend/README.md`。

## 目录结构

```
omnilimb-face/
├── pyproject.toml          # 打包、钉版依赖、可选 extras、入口点
├── plugin.yaml             # PluginManifest(name/version/hooks/tools)
├── __init__.py             # 目录发现 shim —— 重导出 register
├── omnilimb_face/          # 插件包
│   ├── plugin.py           # register(ctx) 入口
│   ├── voice/              # 采集、VAD、唤醒词
│   └── protocol/           # /client-ws 事件模型 + 网关
└── tests/                  # pytest + Hypothesis(单元 / 属性 / 集成)
```

## 开发

```bash
pytest                                   # 运行测试套件
HYPOTHESIS_PROFILE=ci pytest             # 更重的属性测试
```

## 许可

以 **MIT 协议**发布 —— 见 [`LICENSE`](LICENSE)。

### 致谢与第三方授权

本插件**不打包、不再分发**任何形象模型、Live2D Cubism Core 或第三方前端运行时,
全部仅在运行时经 CDN 加载(离线时回退到无依赖的 canvas 占位形象)。详见
[`NOTICE.md`](NOTICE.md) 与 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

- **Open-LLM-VTuber** —— 这里的 `/client-ws` 协议是与
  [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) **兼容**的
  独立重写(截至 2026-06,其 v1.2.0 及之前为 MIT;license 变更说明见 `NOTICE.md`)。
  未拷贝任何上游源码。感谢该项目的协议设计。
- **Live2D Cubism 示例模型** —— 默认形象(`models/model_dict.json`)仅通过钉死 commit
  的 CDN URL 引用 Live2D 官方 Cubism 示例 **"Mao"**。Cubism Core 运行时为 Live2D Inc.
  专有,从 Live2D 的 CDN 加载,不打包。

  > This content uses sample data owned and copyrighted by Live2D Inc.(《Terms of
  > Use for Live2D Cubism Sample Data》/ Live2D Cubism SDK 许可 —— https://www.live2d.com/en/)。

  Live2D 官方示例对一般用户与小规模事业者(最近年销售额低于 1000 万日元)可免费商用/
  非商用;达到或超过该门槛的主体须遵守 Live2D 附加条款。**正式或规模化商用请在
  `models/model_dict.json` 中替换为你自有或已获授权的模型。**
- **pixi.js / pixi-live2d-display / three.js / @pixiv/three-vrm** —— 均为 MIT,CDN 加载。
- **可选 Python 依赖** —— `edge-tts`(`[preview]`,**GPL-3.0**,不打包)与
  `openWakeWord`(`[wakeword]`,库为 Apache-2.0,但其预训练模型为
  **CC-BY-NC-SA-4.0 / 非商用**)在商用时需注意。

# 形象深度整合指南：Live2D + Live3D（表情 / 动作 / 口型 / 导入模型 / 深度开发）

本文档专门讲清 omnilimb-face 里 **Live2D** 与 **Live3D** 两套形象渲染器是怎么工作的：
表情和动作有哪些、**它们到底是怎么绑定的**（从后端文本一路到画面）、**怎么导入一个新模型**
（Cubism 的 `*.model3.json` 和 VRM 的 `*.vrm`），以及后续要做 **Live2D + 3D 深度融合 / 深度开发**
可以往哪些方向走。

> 这是 [`docs/GUIDE.md`](GUIDE.md) 的配套深入文档。GUIDE 讲安装/配置/命令/排查；本文只聚焦“形象”。
> 代码、文件路径、标识符一律用英文；说明用中文。

---

## 1. 总览：一条信号从文字到表情/口型走完整条链路

形象的全部“动”都由一个统一的数据流驱动，**两套渲染器共用同一套协议信号**，互不知道对方存在：

```
LLM 回复文本（含 [emotion] 标记）
        │
        ▼
ExpressionMapper.map_reply()        ← 后端 omnilimb_face/expression.py（纯函数）
   ├─ 抽出 [key] 标记，按 emotionMap 映射成 expression INDEX 序列
   └─ 把标记从文本里删掉，得到 display_text
        │
        ▼
Live2DDirector.push_audio_segment(seg)   ← 后端 omnilimb_face/live2d.py
   构造 AudioEvent：
   { audio, volumes, slice_length, display_text:{text}, actions:{expressions:[INDEX...]} }
        │
        ▼ /client-ws  WebSocket
        ▼
protocol.js  → onAudio(msg) / onSetModel(modelInfo, msg, extras)
        │
        ▼
app.js
   ├─ AudioPlayer 播放 audio，按 volumes/slice_length 采样 → driveMouth(v)（口型）
   ├─ applyExpressions(actions) 取 actions.expressions[0] → avatar.setExpression(idx, name)
   └─ avatar.playMotion("Tap")（让形象做个反应动作）
        │
        ▼ RendererManager 转发给“当前激活的渲染器”
        ├─ Live2DAvatar  (avatar.js)        ← Cubism 模型
        └─ Live3DRenderer (live3d-renderer.js) ← VRM 模型
```

关键点：

- **表情用“索引（index）”在线上传输**，不是名字。名字（happy/anger/...）只在两端用于人类可读和
  Live3D 的 preset 映射。索引↔名字的字典是 **`emotionMap`**，随模型一起下发。
- **口型（lip-sync）和表情是两条独立的信号**。口型来自 `audio` 事件里的 `volumes`（每个元素覆盖
  `slice_length` 毫秒的归一化音量），表情来自 `actions.expressions`。
- **动作（motion）** 目前由前端在每段语音开始时统一触发一个 `"Tap"` 反应动作（Live2D 有效；
  Live3D 当前是安全空实现）。

---

## 2. 协议契约（两套渲染器都依赖它）

定义在 `omnilimb_face/protocol/events.py`（后端）与 `omnilimb_face/frontend/protocol.js`（前端）。
和形象相关的三个事件：

### 2.1 `set-model-and-conf` —— 告诉前端加载哪个模型

```jsonc
{
  "type": "set-model-and-conf",
  "model_info": {
    "name": "default",
    "url": "https://.../Mao.model3.json",   // Live2D Cubism 模型地址
    "emotionMap": { "neutral": 0, "joy": 1, "anger": 4, ... },
    "is_placeholder": false,
    "vrm_url": "https://.../sample.vrm"      // 附加字段：Live3D 的 VRM 地址
  },
  "conf_name": "default"
}
```

- `url` 给 **Live2D** 用；`vrm_url` 给 **Live3D** 用。两者同时下发，**由“当前激活的渲染器”决定加载哪一个**
  （见 `app.js` 的 `loadModelForActiveRenderer`）。
- `vrm_url` 是 **附加字段**：`protocol.js._parseModelExtras()` 把它解析成第三个参数 `extras.vrmUrl`，
  不存在/非法时静默回退，不影响既有字段。这保证了一个“标准 Open-LLM-VTuber 前端”也能照常吃这条事件。

### 2.2 `audio` —— 每段合成语音，驱动口型 + 表情

```jsonc
{
  "type": "audio",
  "audio": "<base64 WAV>" | null,   // null 时只驱动口型/表情，不出声
  "volumes": [0.0, 0.3, 0.8, ...],  // 归一化音量序列（lip-sync 包络）
  "slice_length": 60,               // 每个 volumes 元素覆盖的毫秒数
  "display_text": { "text": "你好呀" },
  "actions": { "expressions": [1] } // expression INDEX 序列（取第 0 个为主表情）
}
```

### 2.3 `control` —— 复位/打断

- `control: "mouth-reset"`：把口型收回静止态（两套渲染器的 `resetMouth()`）。
- `control: "interrupt"`：barge-in 打断，前端立即停止播放并清空队列。

---

## 3. 表情绑定逻辑（最关键的部分）

### 3.1 后端：文本里的 `[key]` 标记 → expression INDEX

文件：`omnilimb_face/expression.py` 的 `ExpressionMapper`（纯函数，对齐 Open-LLM-VTuber 的
`Live2dModel`）。

- 构造时传入 `emotion_map`（关键词→索引）和 `default_expression`（默认/中性表情名，通常 `"neutral"`）。
- `emo_prompt()` 生成 `"[neutral], [joy], [anger]"` 这样的可用关键词串，注入到 LLM 提示里，
  引导模型在回复中输出 `[key]` 标记。
- `map_reply(reply_text)` 做四件事（顺序扫描，保序）：
  1. 用正则 `TAG_PATTERN = \[([^\[\]]+)\]` 抽出所有 `[key]` 标记；
  2. `key` 在 `emotion_map` 里 → 取其索引追加进 `expressions`；不在 → 记进 `unmatched`，不产生索引；
  3. `primary` = **第一个被识别的**标记的索引（冲突时最早者胜）；一个都没识别到就回退到
     `default_expression` 的索引（默认表情自己也没映射时为 `None`）；
  4. `display_text` = 去掉所有标记并规整空白后的可显示文本。

> 识别是**大小写敏感的精确匹配**。`[]`（空括号）和嵌套括号不算标记。

### 3.2 前端：INDEX → 名字 → 具体渲染器

文件：`omnilimb_face/frontend/app.js`

- 收到 `set-model-and-conf` 时，用 `emotionMap` 建**反查表** `indexToEmotion`（索引→名字），
  用于界面标签和 Live3D 的 preset 映射。
- 收到 `audio` 时 `applyExpressions(actions)`：

  ```js
  const idx = actions.expressions[0];               // 取主表情索引
  avatar.setExpression(idx, indexToEmotion[idx]);   // 同时把索引和可读名传下去
  els.expressionLabel.textContent = "expression: " + (indexToEmotion[idx] || "#" + idx);
  ```

- `avatar` 其实是 `RendererManager`，它把 `setExpression(index, name)` 原样转发给“当前激活的渲染器”。
  **同一个调用，Live2D 用 `index`，Live3D 用 `name`** —— 这是两套渲染器表情绑定的分叉点。

### 3.3 Live2D 的表情绑定（用 index）

文件：`omnilimb_face/frontend/avatar.js` 的 `Live2DAvatar`（基于 pixi-live2d-display + Cubism Core）。

- `setExpression(index, name)` → `model.expression(index)`。**`index` 就是模型自带表情列表里的下标**，
  也就是 `model3.json` 的 `FileReferences.Expressions` 数组顺序。
- 所以 **`emotionMap` 的“值”必须等于该模型表情列表里你想要的那个表情的下标**。这就是绑定关系的本质：
  `emotionMap["joy"] = 1` 意思是“joy 这个情绪 = 这个模型的第 1 个表情”。
- 依赖缺失（CDN 没加载到 PIXI / Cubism Core）时退回 `CanvasAvatar` 占位（6 色情绪调色板），
  协议链路照跑不报错。

### 3.4 Live3D 的表情绑定（用 name → VRM preset）

文件：`omnilimb_face/frontend/live3d-renderer.js` 的 `Live3DRenderer`（基于 three.js + three-vrm）。

- VRM 没有“第 N 个表情”的概念，它有**标准情绪 preset**：`happy / angry / sad / relaxed / surprised / neutral`。
- `setExpression(index, name)` 走 `EMOTION_TO_VRM_EXPRESSION[name.toLowerCase()]` 把可读名归一到 preset，
  比如 `joy/happiness/smile → happy`，`anger/mad/rage → angry`，`fear → surprised`。`index` 此处基本不用。
- **单活表情不变量**：应用新 preset 前先把上一个 preset 权重清零，保证任意时刻最多一个情绪 preset 权重 > 0。
- 找不到对应 preset、或模型没有该 preset → **保持当前表情不变**，不报错（`_hasExpression` 用
  `expressionManager.getExpression` 探测）。
- 模型还没加载完就来了表情请求 → 记进 `_pendingExpression`，加载完成后补用（启动时的 neutral 就靠这个）。

---

## 4. 口型（lip-sync）绑定逻辑

口型由 `app.js` 的 `AudioPlayer` 统一产生一个 `[0,1]` 的开合度 `v`，再通过 `driveMouth(v)` /
`driveResetMouth()` **同时扇出**给主渲染器和伴随的粒子球 Orb：

```js
function driveMouth(v)      { avatar.setMouthOpen(v); if (voiceOrb) voiceOrb.setMouthOpen(v); }
function driveResetMouth()  { avatar.resetMouth();    if (voiceOrb) voiceOrb.resetMouth(); }
```

`v` 的来源：优先用 `audio` 事件里的 `volumes`（按 `slice_length` 毫秒采样）；如果只给了真实音频没给
`volumes`，则用 `volumesFromBuffer()` 从解码后的波形按 RMS 现算包络。

### 4.1 Live2D 口型

- 不同 Cubism 版本写法不同：Cubism 4/5 用 `core.setParameterValueById`，Cubism 2.1 用
  `setParamFloat("PARAM_MOUTH_OPEN_Y")`。
- **口型参数 ID 是按模型动态发现的**：读 `model3.json` 的 `Groups → LipSync → Ids`，没有就用默认
  `["ParamMouthOpenY", "ParamA"]`。
- **关键 gotcha**：口型参数必须在 `model.update()`（推进 motion 动画）**之后**写入，否则会被动作动画覆盖。
  `Live2DAvatar` 用 `autoUpdate:false` + 自己在 ticker 里先 `model.update()` 再写口型来规避。

### 4.2 Live3D 口型

- `setMouthOpen(v)` 先 clamp 到 `[0,1]` 存为 `_mouthTarget`（非数值/NaN/Infinity 保持原值不报错），
  实际写入发生在每帧 `vrm.update(dt)` **之后** 的 `_applyMouth()`——和 Live2D 同样的“动画后写口型”策略。
- 口型 blendshape 名按可用项探测：候选 `["aa","a","ou","oh","ih","ee"]`，命中第一个存在的，否则退回 `"aa"`。
- 无语音时还跑待机摆动（idle sway）和 2–6 秒随机间隔的眨眼（blink），说话期间（300ms 余辉窗口内）抑制眨眼。

---

## 5. 动作（motion）绑定逻辑

- 前端在每段语音开始时调用 `avatar.playMotion("Tap")`，让形象对每次回复有个可见反应。
- **Live2D**：`avatar.js` 的 `_resolveMotionGroup` 把 `"Tap"` 解析成一串候选动作组名
  `["TapBody","Tap@Body","Tap","Flick",...]`，命中模型 `model3.json` 的 `FileReferences.Motions` 里
  实际存在的那个组。还会关掉模型自带动作的内置音效（`PIXI.live2d.config.sound = false`）。
- **Live3D**：`playMotion()` 目前是安全空实现（VRM 动作/动画播放留作后续扩展，见第 8 节）。

---

## 6. 如何导入一个新的 Live2D 模型（Cubism）

Live2D 模型就是一套 Cubism 资源，入口是 `*.model3.json`。两种用法：

### 6.1 改 `models/model_dict.json`（推荐）

文件：`models/model_dict.json`，格式是 Open-LLM-VTuber 风格的数组：

```jsonc
[
  {
    "name": "my-avatar",
    "description": "随便写，给人看的",
    "url": "https://your-cdn/yourmodel/Yourmodel.model3.json",
    "emotionMap": {
      "neutral": 0,
      "joy": 1,
      "anger": 2,
      "sadness": 3
    }
  }
]
```

步骤：

1. 把你的 Cubism 模型放到能被浏览器访问的地址（CDN 或本地静态服务），拿到 `*.model3.json` 的 URL，填 `url`。
2. **对齐 `emotionMap`**：打开你模型的 `model3.json`，看 `FileReferences.Expressions` 数组——
   **数组下标就是你要填进 `emotionMap` 的值**。例如该数组第 0 个是 `exp_neutral.exp3.json`、第 1 个是
   `exp_smile.exp3.json`，那就写 `"neutral": 0, "joy": 1`。
3. 后端 `Live2DModelInfo.load_model_info(model_name, model_dict_path)` 会按 `name` 找到这条目；
   找不到/文件缺失/解析失败时**不报错**，降级成占位形象（`is_placeholder=true`）。
4. 预览时选用它：`python preview.py --model my-avatar`（`--model` 默认是 `default`）。
5. 口型和动作**不用手配**：`avatar.js` 会自动读模型声明的 LipSync 参数、自动解析 tap 类动作组。

> 当前内置 `default` 是 Live2D 官方 Mao（Cubism 4）样例，从 CDN 加载，自带 8 个表情 + 8 个动作，开箱即用。

### 6.2 只换默认模型

如果只想换掉默认的，直接编辑 `model_dict.json` 里 `name: "default"` 那条的 `url` 和 `emotionMap` 即可，
不用加 `--model`。

---

## 7. 如何导入一个新的 Live3D 模型（VRM）

Live3D 用 VRM（`*.vrm`，three-vrm 加载）。它**不读 `model_dict.json` 的 `url`**，而是读 `vrm_url`。

1. 准备一个可访问的 `*.vrm` 地址（CDN / 本地静态服务）。VRM 0.x 和 1.0 都支持。
2. 预览时指定：

   ```bash
   python preview.py --vrm-url "https://your-cdn/yourmodel.vrm"
   ```

   后端把它放进 `model_info.vrm_url` 一起下发（见 `preview.py` 的 `vrm_url_holder` 和
   `DEFAULT_VRM_URL`）。
3. 在页面右上角渲染器选择器切到 **Live3D**，`loadModelForActiveRenderer` 就会用
   `extras.vrmUrl`（而不是 Cubism `url`）调 `Live3DRenderer.setModel(vrmUrl)` 加载。
4. **表情不用配映射**：VRM 用标准 preset，`EMOTION_TO_VRM_EXPRESSION` 已经把常见情绪名归一到
   `happy/angry/sad/relaxed/surprised/neutral`。只要你的 emotionMap 名字是这些常见词，就能直接生效；
   模型缺某个 preset 时该表情自动跳过、不报错。
5. 加载失败（URL 非法 / 10 秒超时 / 网络失败 / 不是合法 VRM）会**在画布上显示失败原因文本**，
   保持原渲染状态不变，绝不渲染半个坏模型。

> 默认 VRM 是 pixiv three-vrm 的公开样例（`VRM1_Constraint_Twist_Sample.vrm`）。

---

## 8. 降级链（为什么它“总能显示点东西”）

| 场景 | 行为 |
| --- | --- |
| Live2D 运行时（PIXI/Cubism Core）没加载到 | `createAvatar` 退回 `CanvasAvatar` 占位（6 色情绪球），协议照跑 |
| `model_dict.json` 缺失/解析失败/找不到 name | 后端降级 `is_placeholder=true`，前端显示占位 |
| 切到 Live3D 但 three.js 没就绪 | `Live3DRenderer` 构造抛错 → `RendererManager` 回滚到切换前的渲染器 |
| VRM 加载失败 | 画布上显示失败文本，保持原状，不渲染坏模型 |
| 模型缺某表情/口型 blendshape | 该项静默跳过，其余照常 |

设计原则：**任何一环失败都不连累其它环**，保证页面始终可交互。

---

## 9. 后续深度开发 / Live2D + 3D 深度融合方向

目前 Live2D 和 Live3D 是**互斥切换**（同一时刻只激活一个），它们已经共享统一的
`Renderer_Interface`（`setModel / setMouthOpen / resetMouth / setExpression / playMotion /
setAgentState / destroy`）。基于这套抽象，可以往下面几个方向深入：

1. **统一表情语义层（最值得先做）**
   现在 Live2D 用 index、Live3D 用 name，是两套语义。建议在协议里把表情升级成**语义化情绪 + 强度**
   （如 `{emotion:"joy", intensity:0.8}`），后端只发语义；Live2D 侧再把语义映射到 index、Live3D 侧映射到
   preset 权重。这样换模型就只对一张映射表，不用改协议，也能驱动**表情强度/混合**而不只是“开/关”。

2. **表情混合与过渡（blending / easing）**
   Live3D 的 `expressionManager.setValue` 天然支持 `[0,1]` 权重，可做多 preset 加权混合和缓动过渡；
   Live2D 也可用参数插值做表情淡入淡出。把“单活表情”升级成“带衰减的多表情叠加”，情绪表现会自然很多。

3. **动作（motion）体系补齐 Live3D**
   现在 `Live3DRenderer.playMotion()` 是空实现。可接入 VRM 动画（VRMA / mixamo + retarget），
   把后端的 `actions` 从“只有 expressions”扩展到“也有 motions”，让 2D/3D 都能播放具名动作组，
   并复用 Live2D 已有的 `_resolveMotionGroup` 候选解析思路做跨模型兜底。

4. **2D + 3D 同屏融合**
   既然 Orb 已经证明“主形象旁边可以挂一个独立的常驻可视化”，可以让 Live2D 与 Live3D **同时在场**：
   比如 3D 半身做身体/镜头运动、2D 贴脸做高精度口型表情；或一个做主形象、另一个做画中画。
   实现上让 `RendererManager` 支持“多激活渲染器 + 各自独立 canvas”，口型/表情信号继续走现有扇出
   （`driveMouth` 已经是扇出模式，扩展成 N 路即可）。

5. **口型升级到音素级（viseme）**
   现在口型只用单一“张开度”。可把 STT/TTS 的音素时间轴接进来，驱动 Live3D 的 `aa/ih/ou/ee/oh`
   多元音 blendshape 和 Live2D 的多口型参数，做更精准的对嘴型。

6. **物理与注视（look-at / spring bones）**
   Live3D 已加载 three-vrm（含弹簧骨骼）。可加入头部/眼睛 look-at 跟随光标或说话节奏，
   让形象有“在看你”的临场感；Live2D 也有对应的物理/参数可驱动。

每一步都建议保持现在的“**协议只发语义、渲染器各自解释、失败各自降级**”原则，这样 2D 和 3D 才能在
同一套数据流下持续融合，而不会互相耦合。

---

## 10. 相关源码索引

| 关注点 | 文件 |
| --- | --- |
| 后端：情绪标记 → 索引（纯函数） | `omnilimb_face/expression.py` |
| 后端：模型信息 / emotionMap / 降级 / 事件构造 | `omnilimb_face/live2d.py` |
| 后端：协议事件定义 | `omnilimb_face/protocol/events.py` |
| 模型字典（Live2D 入口） | `models/model_dict.json` |
| 预览/驱动（--model / --vrm-url / 关键词表情） | `preview.py` |
| 前端：协议解析（含 vrm_url → extras.vrmUrl） | `omnilimb_face/frontend/protocol.js` |
| 前端：总装线（applyExpressions / 口型扇出 / 加载模型） | `omnilimb_face/frontend/app.js` |
| 前端：Live2D 渲染器（index 表情 / 动作 / 口型参数发现） | `omnilimb_face/frontend/avatar.js` |
| 前端：Live3D 渲染器（VRM preset / 口型 / 眨眼 / 待机） | `omnilimb_face/frontend/live3d-renderer.js` |
| 前端：渲染器切换/降级 | `omnilimb_face/frontend/renderer-manager.js` |
| 前端：伴随粒子球 Orb | `omnilimb_face/frontend/orb-renderer.js` |

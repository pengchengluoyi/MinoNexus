# Web / VLM 截图：动态缩放与关键区域方案

## 背景

视觉模型读屏时需要在 **清晰度** 与 **成本/延迟** 之间折中：图越大，base64 越长、上传越慢、image token 越多、推理越慢；图过小则小字、图标、密集控件糊掉，定位与断言失败。

[MinoScout](https://github.com/pengchengluoyi/MinoScout) 在 **采集端** 按 `compress_ratio` 缩小 Web 截图并转 JPEG，但 **`width`/`height` 仍报原视口**（协议约定，千分比坐标不受影响）。  
[MinoNexus](.) 在 **消费端** 用 `make_llm_jpeg` 把长边压到约 720–1280 再喂 planner / VLM job。

[Midscene](https://midscenejs.com/reference/) 的 `screenshotShrinkFactor` 是另一类机制：**整图等比缩小**，并用 `shrunkShotToLogicalRatio` 把模型返回坐标还原到逻辑坐标系。官方文档 **没有** 内置「只对某一矩形区域高清截图」的 API；区域策略需要自己在截图管线里做。

---

## 本仓现状（对照 Midscene）

| 环节 | 行为 | 类似 Midscene |
|------|------|----------------|
| Scout `compress_ratio` | 整图缩小 → JPEG；尺寸元数据=原图 | `screenshotShrinkFactor`（在 Scout 侧、仅 Web） |
| Nexus `make_llm_jpeg` | 再压长边 + JPEG quality | 二次缩小（planner/VLM 前） |
| Nexus `make_thumb` | Studio 流式事件缩略图 | 与模型无关 |
| 坐标 | 0–1000 千分比 + hierarchy bounds | Midscene 用 ratio 映射像素 |

调参入口：Console **AI Provider** 的 `web_compress_ratio`、`plan_compress_ratio`；单次 `EXECUTE screenshot` 可覆盖 `params.compress_ratio`。

---

## 关键区域截图：是否可行？

**可行，且与 Midscene 全局 shrink 互补**——不是替代 hierarchy/DOM，而是在「整图已糊」或「屏上只有一小块需要看清」时，给模型 **第二张（或多张）局部高清图**。

适用场景：

- 验证码、小号字体协议、密集表格单元格、小图标按钮。
- 整图 `compress_ratio`/`make_llm_jpeg` 已调到极限仍定位失败。
- Web 登录框在固定区域（可用 `bounds` 扩边裁剪）。

不适用 / 需谨慎：

- **跨域 iframe**：DOM 不可读时只能靠视口裁剪，需用 hierarchy 里 iframe 的 `bounds` 或上次 tap 坐标。
- **区域选错**：裁到空白或邻控件 → 模型幻觉；应带 **原图缩略 + 裁剪框元数据**。
- **多图 token**：一张全图 + 一张局部 ≈ 增加一轮 image token；应 **按需触发**（失败重试、或 planner 声明 `needs_region_shot`）。

Midscene 本身靠 **全局 shrink + 多帧 UI observer** 控 token；「关键区域」是产品层扩展，思路一致：**原图/报告用全分辨率，模型用受控副本**。

---

## 推荐实现（分阶段）

### 阶段 A — Scout 已具备的能力（v0.1.44+）

`EXECUTE screenshot` 的 `params`：

- `full_page` / `fullPage`：整页长图。
- `clip`：`{x,y,width,height}` 或 `region: [x1,y1,x2,y2]`（CSS 像素，与视口一致）。
- 仍配合 `compress_ratio`；**`width`/`height` 继续报全视口逻辑尺寸**，裁剪只影响图像内容，不改变坐标系约定（与 Midscene「缩小但 logical size 不变」同族）。

`hierarchy` 的 `extra`：

- `aria_snapshot`：Playwright `aria_snapshot()` 文本（可 `aria_max_chars` 截断）。
- 节点含同源 iframe / open shadow 内控件；跨域 iframe 占位节点 `cross_origin: true`。

执行：

- `frame_selector` / `frame_path` / `shadow_host_css`：tap/input/upload 作用域。
- `switch_tab` / `open_tab` / `upload_file`。

### 阶段 B — Nexus 按需区域图（建议）

1. **触发条件**（任一即可）  
   - `assert_visual` / locate 连续失败且 `web_channel`；  
   - hierarchy 给出目标 `bounds` 且面积 &lt; 视口 40%；  
   - 设置项 `vlm_region_fallback_enabled`（默认关）。

2. **区域怎么算**  
   - 优先：目标节点 `bounds` ± padding（如 12px 或 5% 屏宽）；  
   - 次选：`web_focus` 焦点元素 bounds；  
   - 再次：上次 tap 千分比 → 像素 → 固定 320×240 窗口 clamp 到视口。

3. **第二次截图**  
   - `router_proxy.observe_async("screenshot", params={ "clip": {...}, "compress_ratio": 1.0 })`；  
   - 或 `make_llm_jpeg` 前对 **原图 base64** 用 Pillow `crop`（不增加 Scout 往返）。

4. **喂模型**  
   - 方案 1（简单）：单消息两张图——全图（缩）+ 局部（高清），system 说明「图2为图1中红框区域」。  
   - 方案 2（省 token）：仅局部图 + hierarchy 文本描述全局。  
   - 坐标：局部图内仍用 **千分比相对全视口** 回答（与现网一致），不要在局部图里再用一套坐标。

5. **与 `plan_compress_ratio` 关系**  
   - 全图：`plan_compress_ratio` / `make_llm_jpeg` 照常；  
   - 局部：`compress_ratio=1` 或 `make_llm_jpeg(long_edge=1280, quality=88)`，避免二次糊化。

### 阶段 C — 可选增强

- Planner 输出 `focus_bounds_1000` 显式要区域截图。  
- 批跑报告存 **full + crop** 双路径（仅报告，不全量进 LLM）。  
- Android：adb 截图 + Pillow crop（无 Playwright `clip`），逻辑与 Web 相同。

---

## 调参建议

| 现象 | 动作 |
|------|------|
| Token/延迟高 | 提高 `web_compress_ratio` / `plan_compress_ratio`（Web 先试 2–3） |
| 小字/图标定位失败 | 略降压缩比 **或** 启用阶段 B 区域 fallback |
| Azure GPT-5 点击偏移 | 参考 Midscene FAQ：提供商侧缩图 → 预先 `make_llm_jpeg` 把短边压到 &lt;768 |

---

## 小结

- **动态缩放**：本仓已在 Scout（Web JPEG）+ Nexus（`make_llm_jpeg`）两层实现，语义对齐 Midscene 的 `screenshotShrinkFactor`，但坐标以 **千分比 + 原图尺寸元数据** 为主。  
- **关键区域截图**：Midscene 无开箱方案，**技术上可行**；推荐 **bounds/焦点驱动裁剪 + 按需第二张图**，全图缩略保上下文，局部高清保细节，并严格统一坐标系。

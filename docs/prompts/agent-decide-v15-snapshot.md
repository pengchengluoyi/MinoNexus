# agent-decide prompt v15（归档）

v16 升级时，**完整 v15 正文**会写入 `llm_jobs` 表 `agent-decide` 行的 `overrides_json.revisions`（`note`: `v15 before swipe_direction from/to milli coords`）。Console Jobs 页可用 `activate_version: 15` 回退。

v15 相对 v14 **程序侧追加**的段落（见 `mino_nexus/ai/job_upgrades.py`）：

## do 步子阶段（operation / achievement）

步骤块会标明当前子阶段：

- **operation**：只执行 instruction 与【本步导航】；「达成信号」仅为摘要，**禁止**当作点击/输入目标。
- **achievement**：只判断本步达成信号可否 `signal_done`；**禁止**再 mutate 改界面。
- `thought` 结论须与 `action.capability_id` 一致；应收工时必须 `signal_done`，勿 thought 写收工却发 tap/press_key/BACK。

标记：`<!-- prompt_version >= 15 (do_subphase + thought/action 对齐) -->`

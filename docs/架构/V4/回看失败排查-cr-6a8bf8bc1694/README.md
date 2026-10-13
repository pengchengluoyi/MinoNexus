# 回看（visual review）误判失败：cr-6a8bf8bc1694

> 日期 2026-10-13。会话 `cr-6a8bf8bc1694::case-b1d321c8::5fda2f6d`，`status=fail`：
> 「do_s2_0_hook 回看未通过（画面上上一步还没完成）。重做之后画面仍未达成，已停止。」

## 结论（一句话）
**点赞 → 拉起登录页，这正是用例要的结果，却被回看判成"没完成"。**
回看拿到的 `expected` 是通用兜底文案，没带用例本步声明的达成信号，于是把预期中的跳转当成"走偏"。设备、点击、模型都没问题。

## 证据链（只看 session_events）
| seq | 事件 | 事实 |
|---|---|---|
| 404 `context/slots` | 步骤 2 | instruction=「在详情页点击点赞图标」；**达成信号=「拉起登录页」**；导航分叉写明"点赞后应进入拉起登录页，到位后 signal_done" |
| 436/437 | tap (200,953) | adb pass，点中详情页左下心形 |
| 440 `review/dispatched` | **expected=「在详情页点击「点赞」图标的结果已经出现在画面上」** | ← 问题所在：没有"登录页" |
| 474 `exec/vision` | 回看 prior_status=**fail** | 模型看到登录页，理由："当前处于造物相机登录页，并非详情页，无法找到点赞图标" |
| 476/477 `review/fail` | redo_already_sent=false | 里程碑回 in_progress，模型点返回 (94,76) 退出登录页 |
| 480-512 | 回到详情页，再点赞 | |
| 514/515 `review/fail` | redo_already_sent=true | 第二次又判 fail → 连续回看未通过 → 停止 |

画面事实：第一次点击后就已是登录页，用例预期达成；之后模型反而把它退出去了。

## 根因
`mino_nexus/loop/visual_review.py::_visible_expected` 对通用 hook 里程碑（`do_s{n}_0_hook`，无专属 id/标题）
只会落到 `f"{标题}的结果已经出现在画面上"`。
而 `remember_visual_prior → _check_sheet` 从不读 `StepCursor.current().expected`（用例步骤的达成信号，
`step_pointer` 已用它生成「达成信号·摘要」给执行模型看）。
**执行提示与回看提示用了两套预期**：执行模型被告知"拉起登录页就收工"，回看却被问"点赞的结果出现了吗"。
凡是"点击后预期跳到另一页"（点赞/收藏/关注 → 登录、支付 → 收银台）的 do 步骤，回看都会误判。

同源的次要问题：第一次 fail 后的 redo 动作是"返回"，把本已达成的状态破坏掉；这是误判的放大器，不是起点。

## 修复
`visual_review.py`：
- `remember_visual_prior` 在 `phase=="do"` 时取 `cursor.current().expected` 传入 `_check_sheet(step_expected=…)`。
- `_visible_expected` 对 `tap_element` 的通用兜底改用 step_expected：
  `「{达成信号}」（以本步声明的结果为准，画面已跳转到该结果即视为通过）`。
- 有专属文案的里程碑（协议弹窗、登录入口、发码、提交登录、输入框聚焦）保持原样，不受影响。

## 验证方式（不写 pytest）
重跑 `case-b1d321c8`，看 `review/dispatched.expected` 是否含「拉起登录页」，以及随后 `review/pass`、步骤 2 进入 check。
若回看仍 fail，再看 `exec/vision.thought` 是否仍在拿"详情页"当判据（prompt 层问题）。

## 遗留 / 建议
1. 回看 fail 后的 redo 若画面已是该步声明的结果，不应再下发"返回"类动作——可在 `apply_same_turn_prior` 加一道"画面命中 step_expected 则 settled"的短路（需要视觉判断，未做）。
2. 步骤 3「在登录页输入手机号与验证码」说明该用例的流程就是登录页，本次失败不会影响用例设计。
3. 排查此类问题的入口：`review/dispatched.expected` ↔ `context/slots` 里的「达成信号」对照，二者不一致即本类问题。

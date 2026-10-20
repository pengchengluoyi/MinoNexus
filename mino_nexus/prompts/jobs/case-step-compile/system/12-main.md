你把测试用例的一段编号步拆成事件。输入 JSON 里有 layer、source、platform、catalog。catalog 是这一层允许的全部事件。

只输出 JSON：{"spans":[...]}
每条 span 除 quote、key_ref、slots/params 外，必须带 intent_role（下列枚举之一，由你根据作者语义判断，不得靠固定句式表）：
- read_only：只观察/阅读/对照界面，不改变 UI（常写在操作栏）
- navigate_to_screen：要把界面带到某个业务屏（操作栏）
- system_app_lifecycle：系统级关闭/强停/启动应用，不是点控件
- mutate_ui：点击、输入、滑动等改变界面
- assert_absent：某对象不应出现
- assert_screen_endstate：只校验稳定终点屏/页面状态，不校验过渡动画过程
- assert_human_only：时序、流畅度、多帧过程等单帧无法可靠自动判定
- assert_copy：指定逐字文案
- assert_other：其他可自动预期检查

intent_role 与 key_ref 必须一致：
- read_only → operation.author_remark
- navigate_to_screen → operation.nav_target（params.nav_target 填口语目标屏名）
- system_app_lifecycle → operation.system_app（params.hook_cap 为 close_app|kill_app|launch_app|open_app）
- assert_absent → expected.ui_absent（params.target.name 为作者指名对象）
- assert_screen_endstate → expected.screen_reached
- assert_human_only → expected.human_review（params.meaning 保留完整含义含时间/过程描述）
- 应用生命周期不得使用 operation.tap

操作栏里语义为 read_only 时，必须 operation.author_remark，不得因「像预期」就报错或改用 expected 事件。
操作栏里语义为 navigate_to_screen 时，必须 operation.nav_target，不得用 expected.screen_reached。
否定可见性（不再出现、无某按钮、不展示某模块）用 assert_absent + expected.ui_absent，不用 language_intent 代替。
过渡/跳转过程不要单独生成 checkpoint；只校验收得住的终点屏（assert_screen_endstate）。
动画时长、流畅、多帧过程用 assert_human_only + expected.human_review。

quote 必须是 source 里逐字连续的一段。key_ref 只能从 catalog 里选。
slots[].quote 必须落在父 quote 里面。params 的键只能是该条目的 param_slots。
若输入里有 errors，按原因改摘录和事件，仍然只能从 catalog 里选。不要输出说明文字。

其余规则与上一版相同：登录流、多 span 顺序、括号预期整句保留、params 对象形状、不得输出 param_slots 数组等，全部继续遵守。

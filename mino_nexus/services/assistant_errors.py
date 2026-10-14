"""将执行错误解释为用户能处理的问题，诊断原文仍留在执行日志。"""
import re
from typing import Any


def explain(summary: str, data: Any = None) -> tuple[str, list[str]]:
    text = str(summary or '').strip()
    if isinstance(data, dict):
        preflight = data.get('preflight') or {}
        detail = data.get('detail') if isinstance(data.get('detail'), dict) else {}
        blocked = preflight.get('blocked') or data.get('blocked') or detail.get('blocked')
        if blocked and not preflight.get('runnable'):
            return ('所选用例均未通过执行预检，本次没有创建任务。',
                    ['打开下方 Workbench 用例链接，检查原步骤和编译预览；修正后重新执行。'])
        message = data.get('user_message')
        if message:
            text = str(message)
    text = re.sub(r'步骤\s*(\d+)\s*操作', r'操作步骤 \1', text)
    if 'credential' in text and ('缺少' in text or 'missing' in text):
        if any(word in text for word in ('一键', '点击', '按钮')):
            prefix = text.split('缺少')[0].rstrip('，,。 ')
            return (prefix + '：系统把这个点击步骤识别成了完整登录流程，因此暂时无法开始执行。',
                    ['在用例编辑页检查该步骤的操作类型，应为点击按钮；修正后再执行。',
                     '如果原意是完整登录流程，请在项目当前环境中配置可用测试账号。'])
        return ('登录流程缺少可用的测试账号，当前无法开始执行。', ['在项目当前环境的账号资源中配置测试账号，再重新执行；不要在聊天里发送密码。'])
    if '渠道' in text and '配置' in text:
        return text, ['打开 Workbench 的项目应用渠道配置，补齐本次环境的应用标识或入口地址。']
    text = re.sub(r'缺少槽\s*[「“]?([\w.-]+)[」”]?', '缺少该操作所需的信息', text)
    if any(word in text for word in ('busy', '占用', '繁忙')):
        return ('执行资源正在被其他任务使用，本次任务尚未提交。', ['等待资源空闲后继续本次请求，或明确选择另一台兼容设备。'])
    if any(word in text for word in ('结果未知', 'uncertain', '调用失败')):
        return ('操作结果暂时无法确认。', ['先查询任务状态，确认是否已经受理；系统不会自动重复创建任务。'])
    if not text:
        return ('服务没有返回可确认的执行结果。', ['在任务页查看状态或联系管理员检查服务。'])
    return text, ['检查上面的具体问题，修正后重新发起请求。']

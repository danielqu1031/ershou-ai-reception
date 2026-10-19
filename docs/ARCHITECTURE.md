# 架构

`浏览器本机页面 → app.py → RecoveryControl / Reception → Xianyu → 闲鱼`

`Reception → Ollama 分类 → policy.py 规则与商品事实 → 候选回复`

- `settings.py`：集中读取和验证本地配置；没有私有配置时使用不能连接真实账号的示例。
- `app.py` / `index.html`：本机页面、来源校验和短期页面令牌。商品字段转义后渲染。
- `policy.py`：本机模型只分类；最终答复受价格与明确事实限制。模型不可用时用保守的未知信息回复。
- `engine.py`：SQLite 去重、发送确认、会话暂停、短期未发送任务及交易保护。
- `xianyu.py` / `login_session.py`：非官方网页协议，处理身份、商品状态、消息、心跳与续期。
- `saved_session.py` / `recovery_control.py`：显式保存同意、恢复代际、暂停/撤销及失败关闭。
- `keychain_vault.py` / `keychain_helper.swift`：仅本机登录钥匙串；凭据经子进程标准输入输出传递，不放参数或环境变量。工具编译时绑定配置账号与商品，不信任所有应用。
- `supervisor.py` / `parent_watch.py`：子进程保活与父进程失联处理。
- `manage.py` / `startup_control.py`：仅管理当前用户这一个接待任务；主动停止持久禁用恢复。

一个运行实例对应一个账号和一个商品。修改配置需停止后重启，不支持热切换；已有保存登录应先撤销。公共测试仅使用假身份，不能访问真实钥匙串或外部消息系统。

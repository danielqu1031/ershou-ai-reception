# Windows 10 / 11

使用 Python 3.12 和 Ollama，免费本机运行。后台接待需要电脑保持唤醒、联网并登录 Windows；锁屏可以，睡眠、关机和注销期间不能接待。登录过期或平台要求验证时仍需重新登录，不承诺永久有效。

## 安装

1. 安装 [Python 3.12](https://www.python.org/downloads/windows/)，保留 Python Launcher；安装 [Ollama Windows 版](https://ollama.com/download/windows)。
2. 将项目完整解压到自己的本机目录，例如 `%USERPROFILE%\Projects\ershou-ai-reception`。不要直接在 ZIP 内启动，也不要使用共享或云同步目录。
3. 双击 `install-windows.bat`。它建立独立 Python 环境、安装依赖并首次复制示例配置；不会覆盖已有配置。
4. 在终端执行 `ollama pull qwen3:4b-instruct-2507-q4_K_M`。模型下载需网络和足够磁盘空间；模型配置必须与实际下载的名称一致。
5. 编辑 `config.local.json`。按 README 填写自己的卖家 ID、商品 ID、标价、底价和真实商品信息。不要填 Cookie。示例 ID 无法连接真实账号。
6. 双击 `start-windows.bat`，打开 http://127.0.0.1:8942/ （如改过端口使用新端口）。按 [连接与首次验收](CONNECT.md) 在本机页面绑定账号，核对真实回复后开启自动接待。

## 保存登录、后台和自启

勾选保存登录后，Cookie 由 Windows DPAPI 按当前用户加密，密文保存于 `%LOCALAPPDATA%\ErshouAIReception\credentials`。不写入项目、启动参数、日志或注册表；无需苹果钥匙串编译工具。不使用机器级解密权限，同一 Windows 用户运行的软件仍有可能解密，因此不要共享这个用户账户。

- `start-windows.bat`：后台启动；关闭启动窗口不会关闭后台程序。程序崩溃会重启，主管进程消失时子进程会退出。
- `autostart-windows.bat`：可选，登记当前用户登录自启，无需管理员。它不自动同意保存登录、不自动切换接待模式。Windows 启动后需要登录该用户。
- `stop-windows.bat`：停止本项目后台程序，禁止自动恢复并移除本项目的登录自启；不关闭共享 Ollama。
- 页面“暂停自动接待”会持久暂停；再次打开不会自行接待。重新启动后，需要在页面明确开启。
- 页面“移除保存登录并禁用自启”会删除本商品保存的密文并停止后台；若浏览器提示连接中断，这是后台退出导致的。

自启仅写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `com.ershoureception.opensource`，不带登录信息。搬动目录前先运行停止脚本，再从新目录安装；暂不支持同时运行多份安装。

## 排查和验证边界

- 找不到 `py`：重新安装 Python 3.12 并启用 Python Launcher。
- 找不到 Ollama：安装后重新打开终端；确认 `ollama --version` 可运行。
- 启动失败：查看 `data/reception.log`。分享日志前检查个人信息，绝不分享 Cookie、数据库或本机配置。
- 密文无法读取：重新在本机页面连接并保存。不要把密文复制到另一台电脑当作登录迁移方案。
- 手动前台启动 `.venv\Scripts\python.exe app.py` 时，用该窗口的 Ctrl+C 退出；停止脚本会提示仍存在的前台实例。

Windows CI 运行通用接待测试、真实 DPAPI 加解密、临时注册表自启配置及进程退出检查；不使用真实账号，不验证真实闲鱼聊天、实际重启登录或所有 Windows 设备兼容性。

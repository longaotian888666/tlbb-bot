# 封号后台 OCR 自动登录模块

本模块从 `tlbb-admin` 的自动封禁链路中剥离出来，只保留自动登录能力：

1. 请求封号后台 `/api/base/captcha` 获取验证码。
2. 将验证码图片提交给 OCR `/ocr6`。
3. 提交 `/api/base/login`。
4. 返回并可落盘保存 session，包括 token、token header、cookie 和用户信息。
5. 可用同一个客户端调用后台业务 API，自动复用登录 cookie 和 token。
6. 只读请求遇到明确的认证失效时会重登并重试一次；写请求默认不重放。

它不包含广告识别、监听任务或 Web 后台代码。经过目标校验的封禁、解封、禁言、游戏用户密码修改等领域方法由同一个客户端提供。
相关后台业务 API 调用示例见：[封号后台 API 使用说明](BAN_ADMIN_API_USAGE.md)。


## 文件

```text
src/tlbb_bot/ban_admin_login.py   自动登录核心和命令行入口
tests/test_ban_admin_login.py     离线登录与重试策略测试
tests/test_ban_admin_http.py      本地 HTTP、CookieJar 和错误处理测试
tests/test_ban_admin_domain.py    领域方法 payload 与安全约束测试
examples/ban_admin_login_usage.py 调用示例
example.ban-admin.env             配置示例，不保存真实账号密码
```

## 运行环境

项目当前约定两个相互独立的 Python 运行时：

| 用途 | 解释器 | 版本 |
| --- | --- | --- |
| 查询后台脚本和本项目测试 | `C:\Users\Administrator\miniconda3\python.exe` | Python 3.13.13 |
| OCR FastAPI 服务 | `D:\code\16capchar\16capchar\ddddocr-fastapi-main\.venv-py11\Scripts\python.exe` | Python 3.11.15 |

OCR 虚拟环境基于 `C:\Users\Administrator\miniconda3\envs\py11\python.exe`，基础解释器版本也是 Python 3.11.15。不要使用 OCR 虚拟环境运行查询后台脚本，也不要用默认 Python 3.13 启动 OCR 服务。

OCR 服务地址：

```text
健康检查：http://127.0.0.1:8000/health
识别接口：http://127.0.0.1:8000/ocr6
```

进程 PID 会随重启变化，不能把曾经的 PID `41500` 当作固定配置。应以健康检查和端口监听状态判断服务是否可用。

## 配置

把 `example.ban-admin.env` 中的字段加入实际 `.env` 后填写真实值：

```env
BAN_ADMIN_LOGIN_URL=https://admin.example.test/#/login
BAN_ADMIN_USERNAME=change-me
BAN_ADMIN_PASSWORD=change-me
BAN_ADMIN_OCR_URL=http://127.0.0.1:8000/ocr6
```

当前真实封号后台登录地址使用明文 HTTP。账号、密码、验证码、cookie 和 token 在网络上传输时不受 TLS 保护，只应在可信网络或 VPN 内使用；服务端条件允许时应迁移到 HTTPS。

也兼容旧中文 key：

```env
封禁账号后台登陆地址：https://admin.example.test/#/login
账号=admin
密码=secret
验证码识别接口=http://127.0.0.1:8000/ocr6
```

## 命令行登录

先确认 OCR 服务健康：

```powershell
$health = Invoke-RestMethod http://127.0.0.1:8000/health
$health.status
```

再使用默认 Python 3.13 执行登录：

```powershell
$env:PYTHONPATH = ".\src"
& "C:\Users\Administrator\miniconda3\python.exe" -m tlbb_bot.ban_admin_login `
  --env .\.env `
  --session-out .\ban_admin_session.json `
  --timeout 25
```

命令行输出会脱敏，不会打印密码、token 或验证码原文。
`ban_admin_session.json` 本身包含可用的 token 和 cookie，属于敏感文件。它已被 `.gitignore` 排除；不要发送、提交或写入日志，并限制只有当前用户可以读取。

## Python 调用

```python
from pathlib import Path

from tlbb_bot.ban_admin_login import BanAdminClient, BanAdminConfig

config = BanAdminConfig(
    login_url="https://admin.example.test/#/login",
    username="admin",
    password="secret",
    ocr_url="http://127.0.0.1:8000/ocr6",
)

client = BanAdminClient(config, session_path=Path("ban_admin_session.json"), timeout=25)
session = client.login()

print({
    "backend": session.backend,
    "token_present": bool(session.token),
    "user": session.user,
})

current_user = client.get_current_user()
```

后台 session 有时效。业务接口调用应优先使用领域方法；底层 `client.request_json()` 会复用登录时的 `CookieJar` 和 `x-token`。默认策略如下：

- `GET`、`HEAD`、`OPTIONS`：遇到 HTTP `401`、`code=7 / 授权已过期`，或响应明确表示 token/登录已失效时，重新登录并重试一次。
- `POST`、`PUT`、`PATCH`、`DELETE`：默认不重试，避免重复封禁、解封、改密或重置密码。
- 无认证失效信息的 HTTP `403` 按权限不足处理，不会触发重登。
- 登录响应缺少 `data.token` 会直接报错，不生成空 token session。

只有确认写接口具备幂等性时，调用方才应显式设置 `retry_on_auth_expired=True`。本模块提供的领域写方法全部固定禁用自动重试。

## 接口约定

验证码接口：

```text
POST /api/base/captcha
```

期望返回：

```json
{
  "code": 0,
  "data": {
    "captchaId": "captcha-id",
    "picPath": "data:image/png;base64,..."
  }
}
```

OCR 接口：

```text
POST http://127.0.0.1:8000/ocr6
Content-Type: application/x-www-form-urlencoded
```

提交字段：

```text
image=<base64>
probability=false
png_fix=false
charsets=0123456789
```

期望返回：

```json
{
  "code": 200,
  "data": ["2468"]
}
```

登录接口：

```text
POST /api/base/login
```

提交字段：

```json
{
  "username": "admin",
  "password": "secret",
  "captcha": "2468",
  "captchaId": "captcha-id"
}
```

## 测试

```powershell
$env:PYTHONPATH = ".\src"
& "C:\Users\Administrator\miniconda3\python.exe" -B -m unittest discover `
  -s tests `
  -p "test_*.py"
```

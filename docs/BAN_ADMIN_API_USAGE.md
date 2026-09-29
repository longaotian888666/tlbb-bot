# 封号后台 API 使用说明

本文说明拿到 `tlbb_bot.ban_admin_login` 的登录 session 后，如何调用封号后台 API 执行：

- 封禁账号 / 解除封禁
- 禁言账号 / 解除禁言
- 查找游戏用户密码 / 修改游戏用户密码

这里的“账号封禁/禁言”实际按后台表单字段是对游戏角色名执行操作；“查密码/改密码”指 `#/layout/GameUsers/Users` 游戏用户管理页面里的用户账号 `passwd` 字段，不是代理账号密码，也不是后台管理员自己的登录密码。

## 认证方式

先创建 OCR 登录客户端：

```python
from pathlib import Path

from tlbb_bot.ban_admin_login import BanAdminClient, load_config

config = load_config(Path(".env"))
client = BanAdminClient(config, session_path=Path("ban_admin_session.json"), timeout=25)
client.login()
```

后续业务请求优先使用 `BanAdminClient` 的领域方法。确实没有封装时才直接使用 `client.request_json()`：

```python
resp = client.request_json("GET", "/user/getUserInfo")
```

`client.request_json()` 使用登录客户端内部同一个 `urllib` opener 和 `CookieJar`，不会丢登录 cookie；请求会自动带 `x-token`。只读请求遇到 HTTP `401`，或 `code=7 / 授权已过期` 等明确的认证失效响应时，会重新执行 OCR 登录并重试一次。无认证失效信息的 HTTP `403` 不会触发重登。

写请求默认不重试。网络超时或断连发生在写请求发出后时，结果可能已经生效，必须先查询后台状态或人工核对，不能直接重发。

## 基础信息接口

### 获取当前后台用户

用于填封禁接口里的 `agentid` 和 `agent`。

```python
user_info = client.get_current_user()
agentid = int(user_info["ID"])
agent = str(user_info["userName"])
```

### 获取后台区服列表

封禁接口必须传后台自己的区服 `ID/configName`，不要直接使用游戏监听协议里的区服 ID。

```python
servers = client.get_servers()

# 示例：按后台区服 ID 找
server = next(row for row in servers if int(row["ID"]) == 28)
serverid = int(server["ID"])
servername = server["configName"]
```

## 模糊查询玩家昵称

封禁前如果用户提供的昵称可能不完整，先调用：

```text
POST /api/gameOrder/getFuzNames
```

领域方法会从当前登录用户读取 `agentid/agent`，从后台区服列表核对 `serverid/servername`，并固定提交接口要求的 `channel="1"` 和空白附加字段。调用方只提供昵称、用户选择的区服 ID 和预期区服名称：

```python
matches = client.find_similar_roles(
    role_name="念．γ",
    server_id=29,
    expected_server_name="二十九区",
    expected_agent_id=66,  # 可选
)

for match in matches:
    print(match["roleName"], match["account"])
```

返回值是零个或多个结构化候选：

```python
[
    {
        "roleName": "念．γ",
        "account": "DD20260626",
    },
]
```

后台目前把候选编码在 `msg` 文本中，而不是 `data`；客户端已经负责解析并去重。该 POST 只执行查询，因此认证失效时允许重登并重试一次。

不要自动选择第一条候选执行封禁。应把全部候选展示给操作者，确认完整昵称、账号和区服后，再把确认后的完整 `roleName` 传给封禁或禁言方法。

## 封禁 / 解除封禁 / 禁言 / 解除禁言

统一接口：

```text
POST /api/gameConfig/banUser
```

后台表单确认的 `channel` 含义：

```text
1 = 禁言
2 = 解除禁言
3 = 封号
4 = 解除封号
```

优先调用领域方法，不要自行拼接 `agentid`、`agent` 或 `channel`。客户端会实时读取当前后台用户和区服列表，核对区服 ID/名称，并禁用写请求自动重试。

封禁角色：

```python
resp = client.ban_role(
    role_name="角色名",
    server_id=28,
    expected_server_name="后台区服名称",
    expected_agent_id=66,  # 可选，用于绑定预期的当前后台用户
)
```

解除封禁：

```python
resp = client.unban_role(
    role_name="角色名",
    server_id=28,
    expected_server_name="后台区服名称",
)
```

禁言角色：

```python
resp = client.mute_role(
    role_name="角色名",
    server_id=28,
    expected_server_name="后台区服名称",
)
```

解除禁言：

```python
resp = client.unmute_role(
    role_name="角色名",
    server_id=28,
    expected_server_name="后台区服名称",
)
```

成功通常返回：

```json
{
  "code": 0,
  "msg": "..."
}
```

`server_id` 必须使用后台区服列表中的 `ID`，不能传游戏监听协议里的区服 ID。客户端要求同时提供 `expected_server_name`，可防止 ID 填错后对其他区服执行操作。角色名和区服名不能包含首尾空白、NUL 或换行。

## 查找游戏用户密码

用户管理页面：

```text
http://tl16.hgwmsc.cn:81/#/layout/GameUsers/Users
```

该页面对应后台前端 `gameUsers` 模块，不是 `agentUsers` 代理管理模块。

### 用户列表查询

```text
GET /api/gameUsers/getGameUsersList
```

领域方法默认移除响应中的 `passwd`：

```python
rows = client.list_game_users(
    account="用户账号",
    page=1,
    page_size=10,
)

for row in rows:
    print(row["ID"], row["account"], row.get("closed"))
```

列表字段里会包含：

```text
ID
CreatedAt
account
passwd
closed
agentId
belong
```

页面展示密码时会用星号遮挡，但原始接口会返回 `passwd`。领域方法默认不把它交给调用方，避免日志和调试输出泄露密码。

### 查询单个游戏用户

```text
GET /api/gameUsers/findGameUsers
```

```python
user_row = client.find_game_user(10001)
print(user_row["account"], user_row.get("closed"))
```

只有明确需要读取密码时才调用：

```python
password = client.get_game_user_password(10001)
# 仅交给需要使用密码的受控流程；不要 print、记录日志或写回普通结果对象。
```

机器人按账号查询时使用精确且唯一的账号解析：

```python
credential = client.get_game_user_password_by_account("game_user_account")
print(credential.account, credential.password)
```

`credential.password` 是按需求提供的明文密码，但 `GameUserPassword.__repr__()` 不会包含它。后台列表查询是模糊搜索；客户端会分页、本地 `casefold()` 精确过滤并按 ID 去重，零条或多条精确结果都会拒绝返回密码。

## 修改游戏用户密码

统一更新接口：

```text
PUT /api/gameUsers/updateGameUsers
```

使用领域方法时必须同时提供用户 ID 和预期账号，防止输错 ID 后修改到其他账号。客户端会读取最新记录，只提交 `ID/account/passwd/closed/belong/agentId` 白名单字段，并禁用 PUT 自动重试。

```python
result = client.update_game_user_password(
    10001,
    "new-password-123",
    expected_account="game_user_account",
    verify=True,
)
print(result)  # 结果不包含新旧密码
```

机器人也可以直接按账号更新：

```python
result = client.update_game_user_password_by_account(
    "game_user_account",
    "new-password-123",
    verify=True,
)
```

后台页面更新时使用的核心字段：

```json
{
  "ID": 10001,
  "account": "game_user_account",
  "passwd": "new-password-123",
  "closed": 0,
  "belong": "",
  "agentId": ""
}
```

`closed` 常见含义：

```text
0 = 正常
1 = 已封禁
```

注意：这个 `closed` 是用户管理页里的账号状态字段；封号/禁言角色仍使用 `/gameConfig/banUser` 的 `channel` 参数。

该后台没有提供版本号、ETag 或 PATCH 接口。读取用户记录和提交完整更新之间仍存在并发覆盖 `closed/belong/agentId` 的窗口；对高并发账号应在执行后复查状态。

## 后台管理员密码说明

后台管理员用户模块另有接口：

```text
POST /api/user/resetPassword
```

后台页面确认的行为是把指定后台用户密码重置为固定临时密码。领域方法要求绑定目标 ID 的精确确认文本，默认拒绝重置当前登录用户，并禁用 POST 自动重试：

```python
result = client.reset_admin_password_to_default(
    77,
    confirmation="RESET ADMIN 77 TO 123456",
)
print(result)  # 不包含临时密码
```

这个接口是后台管理用户密码重置，不是游戏用户 `passwd`，也不是游戏角色封禁接口。接口成功后必须立即让目标管理员修改临时密码。客户端目前没有管理员列表接口可用于确认 ID 对应的用户名，因此执行者仍需在后台页面人工核对目标 ID。

## 执行前检查

生产调用前只记录非敏感目标信息，不要打印 token、cookie 和真实密码：

```python
print({
    "operation": "ban_role",
    "server_id": 28,
    "expected_server_name": "后台区服名称",
    "role_name": "角色名",
})
```

领域方法在 `code != 0` 时抛出 `BanAdminAPIError`，响应结构不符合约定时抛出 `BanAdminContractError`。不要盲目重试真实封禁、解封、改密码或管理员密码重置操作。

# TLBB 机器人整合说明

机器人层把两套独立后台整合为统一命令：

- `TLBBBackendClient`：按玩家账号查询代理归属。
- `BanAdminClient`：区服、昵称模糊查询、角色操作、游戏账号查密和改密。
- `TLBBBotService`：线程安全的业务门面。
- `BotCommandProcessor`：命令解析、私聊限制、候选消歧和一次性确认。
- `telegram_bot`：Telegram 私聊、持久按钮菜单和 polling 入口。
- `bot_cli`：无需消息平台 token 的本地交互入口。

Telegram 适配器基于 `python-telegram-bot 21.6`。业务逻辑仍与平台解耦，本地入口和 Telegram 共用同一套安全校验与确认流程。

## 配置

实际 `.env` 需要同时包含：

```env
API_BASE_URL=https://tlcw.sxjzsz.com/prod-api
API_USERNAME=代理后台账号
API_PASSWORD=代理后台密码

BAN_ADMIN_LOGIN_URL=http://tl16.hgwmsc.cn:81/#/login
BAN_ADMIN_USERNAME=封号后台账号
BAN_ADMIN_PASSWORD=封号后台密码
BAN_ADMIN_OCR_URL=http://127.0.0.1:8000/ocr6

TELEGRAM_BOT_TOKEN=BotFather 提供的 token
TELEGRAM_SUPER_ADMIN_ID=总管理员的 Telegram 数字 ID
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_ADMIN_STORE_PATH=.telegram-admins.json
TELEGRAM_SECRET_MESSAGE_TTL_SECONDS=60
```

完整占位模板见 `example.bot.env`。不要提交真实账号、密码、token 或 session 文件。

`TELEGRAM_SUPER_ADMIN_ID` 是不可由机器人修改的根权限。首次配置步骤：

1. 私聊机器人并发送 `/whoami`。
2. 把返回的数字 ID 写入 `TELEGRAM_SUPER_ADMIN_ID`。
3. 重启机器人。

为兼容旧配置，当 `TELEGRAM_SUPER_ADMIN_ID` 未设置且
`TELEGRAM_ALLOWED_USER_IDS` 恰好只有一个 ID 时，该 ID 会被视为总管理员。
`TELEGRAM_ALLOWED_USER_IDS` 可继续配置逗号分隔的静态管理员，但这些管理员
只能通过修改 `.env` 删除。

总管理员通过“管理员管理”新增的普通管理员保存在
`TELEGRAM_ADMIN_STORE_PATH` 指定的 JSON 文件中，默认是项目根目录下的
`.telegram-admins.json`。新增和删除立即生效，不需要重启；总管理员自身不能
通过机器人删除。存储文件损坏时机器人会拒绝启动，不会静默放开权限。

## Telegram 启动

先检查 OCR：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

再使用后台脚本的 Python 3.13 启动 polling：

```powershell
$env:PYTHONPATH = ".\src"
& "C:\Users\Administrator\miniconda3\python.exe" -m tlbb_bot.telegram_bot --env .\.env --check
& "C:\Users\Administrator\miniconda3\python.exe" -m tlbb_bot.telegram_bot --env .\.env
```

`--check` 会验证 token、调用 `getMe`、安装私聊命令菜单后退出，不启动 polling。

机器人启动后私聊发送 `/start`。普通管理员的输入框上方会常驻三个主功能按钮：

```text
查代理
查改密码
封号禁言
```

总管理员额外显示“管理员管理”，其中提供：

```text
管理员列表
新增管理员
删除管理员
```

新增或删除必须输入对方通过 `/whoami` 获得的 Telegram 数字 ID，并进行二次
确认。普通管理员不能查看列表或修改权限。删除管理员时，该用户尚未确认的封号、
禁言或改密操作会一并失效。也可使用 `/admins`、`/add_admin <ID>` 和
`/remove_admin <ID>` 发起相同流程。

`查改密码` 会展开“查询密码 / 修改密码”，`封号禁言` 会展开昵称查询、区服列表、封禁、解封、禁言和解除禁言。所有后台功能只允许白名单用户在私聊中使用；群聊不会调用后台。

本地交互入口仍可用于维护和排查：

```powershell
$env:PYTHONPATH = ".\src"
& "C:\Users\Administrator\miniconda3\python.exe" -m tlbb_bot.bot_cli --env .\.env
```

## 命令

查询账号所属代理：

```text
/agent <账号> [game_id]
```

`game_id` 既可能是数字，也可能是后台返回的十六进制字符串；账号跨游戏重复时，把查询结果中的原值传回即可。

每条结果会根据直接所属代理 `agentId` 输出 `是否归属369团队：是/否`。当前
369 团队代理 ID 为：`7080`、`7181`、`7078`、`7103`、`7167`、`7111`、
`7184`、`7105`、`7185`、`7114`、`7600`、`7597`、`7685`。判断不使用
`agentParents` 上级代理链；同一账号存在多条游戏记录时会逐条判断。

列出封号后台区服：

```text
/servers
```

模糊查询玩家昵称：

```text
/roles <区服ID> <昵称片段>
```

角色操作：

```text
/role <ban|unban|mute|unmute> <区服ID> <完整昵称>
/confirm <确认码>
```

机器人会先重新查询候选。只有完整昵称唯一匹配时才生成确认码；确认时还会再次检查昵称、账号和区服。不会把模糊结果第一项直接用于封禁。

按账号查询明文密码：

```text
/password <账号>
```

该命令按需求返回明文密码。Telegram 响应启用 `protect_content`，并在 `TELEGRAM_SECRET_MESSAGE_TTL_SECONDS` 后自动删除；设置为 `0` 可关闭删除。Telegram 无法阻止截图或通知预览，因此仍不能记录或转发密码。

按账号更新密码：

```text
/set_password <账号>
```

Telegram 按钮流程会在收到新密码后立即删除该入站消息，再生成确认操作。本地入口使用隐藏输入。确认信息和成功响应都不会回显新密码。

取消当前操作者的待确认操作：

```text
/cancel
```

## 安全约束

- 封禁、解封、禁言、解除禁言和改密都需要一次性确认码，默认 300 秒过期。
- 每个操作者只保留一个待确认操作；确认码绑定操作者且只能使用一次。
- 所有角色写操作和密码更新都禁用自动重试。
- 查密和改密先对后台模糊搜索结果进行本地精确匹配；零条或多条时拒绝继续。
- 新密码只短暂保存在进程内存的待确认状态中，不持久化，也不会出现在对象 `repr` 中。
- Telegram token 和 Bot API 请求 URL 不进入日志；`httpx` 和 Telegram 请求日志被限制为警告级别。
- 封号后台当前使用明文 HTTP，必须运行在可信网络或 VPN 中。

## Telegram 行为

- `/start`、`/menu`：恢复主菜单。
- `/whoami`：未授权用户也可查看自己的数字 ID。
- `/admins`：仅总管理员查看管理员列表。
- `/add_admin <ID>`、`/remove_admin <ID>`：仅总管理员发起权限变更并二次确认。
- `/servers`：查看后台区服。
- `/cancel`：取消当前待确认操作。
- 人工启动时默认丢弃此前积压的 Telegram 更新，避免执行旧的敏感命令。
- polling 遇到 Telegram 传输层 `NetworkError` 时会退出旧连接，并按 5、10、20、40、60 秒退避重连；同一个 Application 会被复用，故障期间的新更新、管理员状态和进程内待确认操作都会保留。
- 离开确认界面、切换功能或发生异常时，会同时撤销服务层待确认操作。

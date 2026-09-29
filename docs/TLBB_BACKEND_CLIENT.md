# TLBB 后台自动登录与业绩查询客户端

这个文件说明如何在其他项目里复用 TLBB 后台登录和代理业绩查询能力。

核心代码在：

- `src/tlbb_backend.py`

该模块是独立客户端，不依赖 Telegram 机器人、SQLite、配置文件或定时任务，只依赖：

```text
requests
```

## 环境变量

不要在代码里写死账号密码。建议在新项目里配置：

```env
API_BASE_URL=https://tlcw.sxjzsz.com/prod-api
API_USERNAME=你的后台账号
API_PASSWORD=你的后台密码
```

## 基础用法

```python
from src.tlbb_backend import TLBBBackendClient

client = TLBBBackendClient.from_env()

# 自动登录后查询指定日期所有代理业绩
rows = client.get_agent_rank(
    "2026-07-01 00:00:00",
    "2026-07-01 23:59:59",
)

for row in rows:
    print(
        row.get("agentId"),
        row.get("nickname") or row.get("rolename"),
        row.get("total_recharge", 0),
        row.get("paid_order_count", 0),
        row.get("user_reg_count", 0),
    )
```

`get_agent_rank()` 会自动：

- 调用 `/admin/agent/login` 登录。
- 从登录响应读取 `data.token`。
- 把 token 放到后续请求的 `token` header。
- 分页拉取 `/admin/data/agentDataRank`。
- 尝试读取 `/admin/agent/getSubAgentList`，给排行数据补充 `nickname`、`username`、`legacyAgentId`。

## 查询玩家账号所属代理

后台玩家管理页面使用：

```text
POST /admin/user/list
```

客户端已封装为：

```python
from src.tlbb_backend import TLBBBackendClient

# 直接读取 .env 中的 API_BASE_URL、API_USERNAME、API_PASSWORD。
client = TLBBBackendClient.from_env_file(".env")

player = client.get_player_agent("玩家账号")
if player is None:
    print("未找到玩家")
else:
    print(
        player["account"],
        player["agentId"],       # 直接所属代理 ID
        player["agentParents"],  # 上级代理链
        player.get("username"),  # 代理后台用户名
        player.get("nickname"),  # 代理昵称
    )
```

返回结构示例：

```python
{
    "account": "player-account",
    "agentId": 7103,
    "agentParents": [7049, 7050],
    "gameId": 1,
    "gameName": "逍遥天龙",
    "isStop": False,
    "createTime": "2026-07-27T00:00:00.000Z",
    "currentAgentId": 7103,
    "legacyAgentId": 6103,
    "username": "agent-user",
    "nickname": "代理昵称",
}
```

后台对账号执行的是模糊搜索。客户端会继续按账号进行大小写不敏感的精确匹配，避免把相似账号当成目标账号；接口响应中的密码字段不会向调用方返回。

代理后台 token 失效时，客户端会识别 `登录失效!`、未登录、token/授权过期等
明确认证错误，清除旧 token、重新登录，并对已知只读接口重试一次。
`/admin/user/list` 虽使用 POST，但仅用于查询，因此显式允许该单次重试；未知
POST 请求默认不因认证过期自动重发。新登录后仍失败时会立即报错，不会循环。

如果相同账号存在于多个游戏，会抛出 `TLBBBackendError`，此时传入游戏 ID：

```python
player = client.get_player_agent("玩家账号", game_id=1)
```

真实后台的游戏 ID 可能是整数，也可能是十六进制字符串；客户端支持两种形式。

也可以获取全部精确匹配项：

```python
players = client.get_player_agents("玩家账号")
```

`from_env_file()` 会直接解析磁盘上的 `KEY=VALUE` 配置，不依赖 `python-dotenv`。如果启动环境已经注入了环境变量，也可以继续使用 `TLBBBackendClient.from_env()`。

也可以直接运行示例：

```powershell
python .\examples\tlbb_player_agent_usage.py "玩家账号" --env .\.env
```

## 查询某几个代理

```python
from src.tlbb_backend import TLBBBackendClient

client = TLBBBackendClient.from_env()

agents = client.get_agent_performance(
    "2026-07-01 00:00:00",
    "2026-07-01 23:59:59",
    agent_ids=[7103, 7167, 7111],
)

print(agents[7167]["total_recharge"])
```

返回结构是：

```python
{
    7167: {
        "agentId": 7167,
        "total_recharge": 1234,
        "order_count": 10,
        "paid_order_count": 8,
        "user_reg_count": 3,
        "nickname": "西西里",
        "legacyAgentId": 6182,
        "currentAgentId": 7167,
    }
}
```

## 查询当天实时数据

```python
rows = client.fetch_today_realtime(timezone_name="Asia/Shanghai")
```

这个方法会查询今天 `00:00:00` 到 `23:59:59`，并传 `dataType="today"`，用于当前日实时数据。

## 查询某天完整数据

```python
from datetime import date

index_data, agent_rows = client.fetch_date(date(2026, 7, 1))
```

`index_data` 来自 `/admin/data/indexData`。

`agent_rows` 来自 `/admin/data/agentDataRank`。

## 常用字段

| 字段 | 含义 |
| --- | --- |
| `agentId` | 当前后台代理 ID |
| `legacyAgentId` | 旧代理 ID，如果后台返回了 legacy 映射 |
| `nickname` | 主播/代理昵称 |
| `username` | 后台用户名 |
| `total_recharge` | 充值金额 |
| `order_count` | 总订单数 |
| `paid_order_count` | 已付订单数 |
| `user_reg_count` | 新用户数 |

## 直接复制到新项目

如果另一个项目不是这个仓库结构，可以直接复制：

```text
src/tlbb_backend.py
```

然后在新项目中安装：

```bash
pip install requests
```

如果你的新项目不使用 `src` 包路径，可以把文件放到项目根目录并这样导入：

```python
from tlbb_backend import TLBBBackendClient
```

## 错误处理

客户端统一抛出 `TLBBBackendError`：

```python
from src.tlbb_backend import TLBBBackendClient, TLBBBackendError

try:
    client = TLBBBackendClient.from_env()
    rows = client.fetch_today_realtime()
except TLBBBackendError as exc:
    print(f"TLBB 后台查询失败: {exc}")
```

## 注意事项

- 后台认证 header 是 `token`，不是 `Authorization: Bearer`。
- token 由客户端登录后自动设置；一般脚本每次运行重新登录即可。
- 日期参数用完整时间字符串：`YYYY-MM-DD HH:MM:SS`。
- 当天实时数据建议使用 `fetch_today_realtime()`，它会传 `dataType="today"`。
- 不要把真实账号密码提交到代码仓库。

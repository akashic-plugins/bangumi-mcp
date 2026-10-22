# bangumi-mcp

Akashic Plugin API v3 插件，用 Bangumi 官方 API 分页查询收藏列表、查询单个条目的准确进度、设置“在看/看过”，通过章节接口逐集更新动画观看进度，并按 AniList 计划放送时间记录在看动画的新一集提醒。

## 安全边界

- 只连接 `https://api.bgm.tv` 和 `https://graphql.anilist.co`，使用符合官方要求的 User-Agent。
- Bangumi Access Token 和 AniList Access Token 通过插件配置程序写入 Akashic 私有凭据库；固定配置与工具结果里只有不可变引用，没有明文 Token。
- 所有远端写入先生成包含作品名和目标状态/集数的预览；确认记录单次使用并在 10 分钟后过期。
- 完整收藏查询或累计读取达到 100 条时，先显示只读查询范围与预计请求数，并等待本轮之后的逐字确认。
- 动画进度使用 `PATCH /v0/users/-/collections/{subject_id}/episodes` 和明确的章节 ID，不用条目级 `ep_status` 修改动画集数。

## 安装

Akashic 只安装 Git 已提交快照。从 Akashic 仓库执行：

```bash
.venv/bin/python main.py plugin-install \
  --source https://github.com/akashic-plugins/bangumi-mcp.git \
  --marketplace github
```

安装输出会给出数据目录，默认是：

```text
<workspace>/plugin-data/bangumi-github/
```

## 配置

登录 Bangumi 后，前往 [Bangumi Access Token 页面](https://next.bgm.tv/demo/access-token) 生成个人 Token。该 Token 是账号的 API 访问凭据，不是 Bangumi 密码。

V3 插件不再读取手写的 `config.local.toml`；配置由插件自己的命令写入固定输入与私有凭据库。**不要把 Token 放进命令行参数**（会进入进程列表），用环境变量传递：

```bash
export AKASHIC_BANGUMI_ACCESS_TOKEN="<Bangumi Access Token>"

.venv/bin/python main.py bangumi-configure
```

宿主的 `main.py setup` 只初始化 Core，**不再发现或执行插件的 `configure.py`**；配置入口是本插件通过 `entrypoints` 声明的 `bangumi-configure` 命令。

该命令只从**已提交的插件选择**里查找，所以要先安装并启用插件，否则会以退出码 2 结束：

- 从未安装 → `当前空选择没有命令 provider: bangumi-configure`
- 已安装但未启用 → `命令 bangumi-configure 需要唯一 provider，当前有 0 个`

可选配置项（同样只用环境变量）：

| 环境变量 | 含义 |
|---|---|
| `AKASHIC_BANGUMI_ANILIST_TOKEN` | AniList Access Token；**启用放送提醒时必须提供** |
| `AKASHIC_BANGUMI_ANIME_PUSH_ENABLED` | `1` / `true` 启用放送提醒抓取 |
| `AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES` | 提前提醒分钟数，`0` 至 `1440` |
| `AKASHIC_BANGUMI_DISPLAY_TIMEZONE` | 展示用 IANA 时区，默认 `Asia/Shanghai` |
| `AKASHIC_BANGUMI_USER_AGENT` | 覆盖默认 User-Agent |

**放送提醒必须同时满足两个条件**：`ANIME_PUSH_ENABLED` 为真，且 AniList Token 非空。
只打开开关而没有 Token 时，后台刷新**不会启动**（`background_refresh_enabled` 要求两者同时成立），
本地事件库也不会新增记录，界面上不会有任何报错。启用前请先确认 Token 已写入。

`media_id_overrides`（AniList 自动匹配失败时的人工映射）需要在固定配置里手工补充，左侧是 Bangumi subject ID：

```jsonc
// <workspace>/plugin-data/bangumi-github/config.input.json
"media_id_overrides": { "501963": 123456 }
```

配置完成后检查：

```bash
.venv/bin/python main.py plugin-doctor bangumi@github
```

运行中的 Akashic 会观察配置变化并发布新的 committed generation；新会话会加载 `bangumi` Skill 与插件工具。

### 从旧版本升级

旧版本把 Token 明文写在 `plugin-data/bangumi-github/config.local.toml`。V3 运行时遇到该文件会拒绝加载，必须先显式升级：

```bash
.venv/bin/python main.py bangumi-configure --upgrade
```

升级会把明文 Token 转入私有凭据库、写出 `config.input.json`，并把旧文件与其命名备份保存在 `.plugin-credentials/bangumi-github/upgrades/` 的恢复点里，便于人工核对。

## 放送提醒

- 提醒依据是 AniList `nextAiringEpisode.airingAt`，精度规范化到分钟；它表示计划放送，不代表流媒体、字幕或片源已经上线。
- `notify_before_minutes=0` 从计划放送时刻开始等待投递；可设置为 `0` 至 `1440` 的整数以提前提醒。
- 插件只处理 Bangumi 标记为“在看”的动画，并在记录提醒前通过逐集接口确认该集尚未标记为“看过”。
- 刷新循环把到期事件写入 `plugin-data/bangumi-github/anime_updates.db`，再通过 `eventmail` 告警邮箱交给 `wake` 插件投递到用户渠道。
- 到达提醒时刻后，Akashic 在下一次 tick 读取事件。Gate、Judge、会话繁忙或 channel 状态可能使消息延后数分钟。
- **投递前提**：Akashic 需要安装并启用 `eventmail` 与 `wake`，且为 `wake` 配置投递目标（`delivery` 的 channel / recipient / session）。未配置时告警会留在邮箱里，不会送到用户，但不会报错。
- `eventmail` 不可用时自动降级：抓取与本地事件库照常运行，`get_anime_update_alerts` / `acknowledge_anime_update_alerts` 仍可手动查看与清理。
- 启用 `anime_push` 必须配置 AniList Access Token（见上方环境变量表）。
- AniList 与 Bangumi 没有共同稳定 ID。自动匹配只接受唯一严格候选；失败时可用固定配置中的 `media_id_overrides` 明确指定 AniList media ID。
- 缓存、待投递事件和 ACK 保存在同一 plugin-data 目录下的 `anime_updates.db`。关闭 `anime_push.enabled` 不会删除该数据库。

## 设计文档

- [插件 V2 → V3 迁移方案](docs/design/plugin-v3-migration.md)
- [插件契约刷新（跟随 Akashic 插件边界重构）](docs/design/plugin-contract-refresh.md)
- [用户收藏列表设计](docs/design/collection-list.md)
- [收藏查询意图与展示边界](docs/design/collection-query-intent.md)
- [收藏完整与大量查询确认边界](docs/design/collection-query-confirmation.md)
- [追更放送提醒设计](docs/design/anime-update-push.md)

## 开发验证

测试必须在**宿主环境**里运行：插件的工具结果直接使用
`plugins.ledger.contract.ContentPart`，导入它就等于导入宿主。

```bash
git clone https://github.com/kachofugetsu09/akashic-agent ~/akashic-agent
cd ~/akashic-agent && python -m pip install -r requirements.txt -r requirements-dev.txt

cd <本仓库>
AKASHIC_AGENT_ROOT=~/akashic-agent \
  ~/akashic-agent/.venv/bin/python -m pytest
```

`AKASHIC_AGENT_ROOT` 也可以不设：默认查找同级的 `../akashic-agent`。
只有宿主源码存在**且**其依赖可导入时测试才会执行，否则收集阶段直接报错，
不会用本地桩假装通过。

单元测试全部使用假的 HTTP 会话，不访问真实 Bangumi 账户，也不需要 Access Token。

测试分三层：

| 文件 | 覆盖 |
|---|---|
| `tests/test_service.py` 等 | 纯领域逻辑：收藏查询、确认、AniList、事件库、配置解析 |
| `tests/test_target_contract.py` | 契约形状：`docs/design/plugin-contract-refresh.md` 承诺的重构后状态 |
| `tests/test_upstream_contract.py` | 把契约放进**真实宿主**验证（加载器、ServiceKey 身份、工具结果类型） |

### 测试为什么能导入相对导入的 `plugin.py`

`plugin.py` 用 `from .bangumi.…`（宿主把插件注册成包，插件根不在 `sys.path`）。
测试通过 `tests/conftest.py` 注册的伪包 `bangumi_test_plugin` 拿到同样的包身份，
所以测试里写 `from bangumi_test_plugin import plugin`。这与同组织
`akashic-plugins/fitbit-mcp` 的做法一致。

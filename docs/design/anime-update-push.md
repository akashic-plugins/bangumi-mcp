# 追更放送提醒（主动推送）设计

- 状态：Implemented（修订 5）
- 目标版本：`0.5.0`
- 日期：2026-08-08
- 依据：[Bangumi 官方 API](https://bangumi.github.io/api/)、[AniList API](https://docs.anilist.co/)、Akashic `create-proactive-source` skill、`proactive_v2` 实现

## 1. 问题和用户意图

插件当前可以查询收藏、观看进度并更新状态，但只能在用户发起请求后执行。追更放送提醒补充一个主动能力：根据 AniList 的计划放送时刻，提醒用户正在 Bangumi 标记为「在看」且尚未观看的新一集。

本功能是**计划放送提醒**，不是流媒体、字幕或片源上线检测。AniList 的 `airingAt` 表示计划放送时刻；提醒文案不得把它扩大解释为某个平台已经可以观看。

## 2. 目标

1. 使用 AniList 的计划放送时间，按分钟粒度提醒正在看的动画新一集。
2. 只处理 Bangumi 收藏状态为「在看」的动画。
3. 在产生提醒前读取对应 Bangumi 章节收藏，已标记看过的章节不提醒。
4. 同一 Bangumi `episode_id` 在成功投递后不重复提醒。
5. 提供独立开关；关闭提醒不影响插件现有查询和写入能力。
6. 外部刷新、映射、待投递事件和 ACK 状态全部由插件 MCP 拥有。
7. AniList 不可用或条目映射不可靠时不生成提醒，不降级为日期粒度或网页抓取。

## 3. 精度承诺和非目标
### 3.1 精度承诺

- `airingAt` 是秒级 Unix 时间戳；插件把提醒时刻规范化到分钟。
- 到达提醒时刻后，事件会在 Akashic 下一次 proactive tick 被读取和投递。
- proactive tick、Gate、Judge、目标会话 busy 和外部 channel 状态都可能造成数分钟或更久的延迟。
- 本功能不承诺在计划放送时刻的同一分钟送达，只承诺使用该分钟作为提醒依据并在后续可用 tick 尽快投递。

### 3.2 非目标

- 不检测视频网站、字幕组或下载源是否已经更新。
- 不提供 Bangumi 日期粒度提醒。
- 不使用 Playwright、搜索引擎或网页解析作为 fallback。
- 不做放送倒计时页面或下周预告。
- 不修改 Akashic Core 的调度、Gate、Judge、Deliver 或 ACK 协议。
- 不用插件 `PluginJobSpec` 驱动刷新；缓存新鲜度由 MCP lifespan 内的后台任务维护。

## 4. 已确认事实和未知边界

### 4.1 Bangumi

1. 当前用户通过 `GET /v0/me` 取得真实 username；收藏列表使用 `GET /v0/users/{username}/collections`，其中 username 必须 URL 编码。
2. `subject_type=2&type=3` 可以筛选「动画 + 在看」。收藏列表需要按官方分页完整读取。
3. 普通章节使用 `GET /v0/episodes?subject_id={id}&type=0&limit=&offset=` 分页取得。
4. 条目下的逐集收藏使用 `GET /v0/users/-/collections/{subject_id}/episodes?episode_type=0&limit=&offset=` 分页取得。这里的用户段必须是字面量 `-`；该接口不接受由 `/v0/me` 得到的真实 username。
5. 上述分页响应是 `{total, limit, offset, data}`，`data` 中每项为 `{episode, type, updated_at}`；`episode` 是完整章节对象。
6. 到期时复核单集可以使用 `GET /v0/users/-/collections/-/episodes/{episode_id}`，两个用户/条目占位段同样都是字面量 `-`。响应是单个 `{episode, type, updated_at}`，不是分页包装。
7. 逐集收藏的 `type` 使用独立的 `EpisodeCollectionType`：`0=未收藏`、`1=想看`、`2=看过`、`3=抛弃`。它不同于条目收藏的 `SubjectCollectionType`（`1..5`）；只有逐集 `type == 2` 表示该集已看过。
8. 列表中的 `ep_status` 或插件的 `reported_episode_progress` 只是摘要，不能证明某一集已经看过。
9. Bangumi 章节只有 `airdate` 日期，没有计划放送的时、分或时区。
10. 收藏 `updated_at` 不是可靠的章节或放送更新时间，不参与提醒判断。

### 4.2 AniList

1. `Media.nextAiringEpisode` 提供未来下一集的 `episode` 和 `airingAt`。
2. `nextAiringEpisode` 在一集放送后可能推进到下一集，因此必须在放送前把 schedule 持久化；到点后不能依赖实时查询找回刚刚放送的集。
3. AniList episode number 不是 Bangumi `episode_id`。插件必须把 AniList 集数唯一映射到 Bangumi `type=0` 章节，事件身份最终使用 Bangumi `episode_id`。
4. 标题搜索不是稳定外键。自动映射需要验证标题、首播时间、季度、格式和已知总集数；多候选、字段冲突或置信不足时不得自动接受。
5. 用户可以在插件私密配置中提供显式 `subject_id -> AniList media_id` 覆盖，覆盖值仍需读取 AniList 条目并验证基本类型。

### 4.3 未知边界

- AniList 和 Bangumi 没有共同的官方稳定 ID，部分动画只能由用户配置显式映射。
- 外部计划可能临时延后或取消。事件进入 `pending` 前允许更新 schedule；进入 `pending` 后保留当时的不可变事件快照。
- Akashic proactive source 是 best-effort 主动投递链路，不提供端到端严格恰好一次发送。

## 5. Owner 和总体结构

```text
┌─ Bangumi MCP lifespan
│  ├─ catalog refresh
│  │  ├─ Bangumi 在看动画
│  │  └─ Bangumi 普通章节
│  ├─ schedule refresh
│  │  ├─ 严格解析 Bangumi ↔ AniList 映射
│  │  └─ 缓存 nextAiringEpisode
│  ├─ due evaluation
│  │  └─ scheduled → pending / suppressed / expired
│  └─ <plugin-data>/anime_updates.db
│
├─ Akashic proactive tick
│  └─ fetch_tool 只读 pending alert 快照
│
├─ Gate / Judge / Resolve / Deliver
│  └─ Akashic Core 拥有外部发送
│
└─ Deliver 成功
   └─ ack_tool: pending → acked
```

| 对象 | 权威 owner | 消费者 |
|---|---|---|
| Bangumi Token、AniList Token、映射覆盖和开关 | plugin-data `config.local.toml` | Bangumi MCP、插件声明 |
| AniList schedule、Bangumi episode 映射、pending/ACK 和重试状态 | Bangumi MCP SQLite | proactive fetch/ACK 工具 |
| source catalog、Gate、Judge、delivery dedupe | Akashic Core | proactive runtime |
| 外部消息和主动会话历史 | Akashic Core | channel、session store |

本功能不需要 Akashic runtime patch。插件只使用公开的 MCP 和 `ProactiveSourceSpec` 合同。

## 6. 插件声明和配置

### 6.1 配置模型

`plugin.py` 的 `BangumiConfig` 新增嵌套配置；Token 字段使用 `SecretStr`。MCP runtime config 使用 `repr=False`，异常、日志和工具结果不得包含 Token。

```toml
[anime_push]
enabled = true
notify_before_minutes = 0
display_timezone = "Asia/Shanghai"
anilist_token = "<AniList Access Token>"

[anime_push.media_id_overrides]
"501963" = 123456
```

- `enabled=false` 时插件不声明 source，MCP 也不启动提醒刷新任务；现有工具继续可用。
- `enabled` 默认值为 `false`，升级插件不会在未配置时自动开启外部刷新和主动提醒。
- `notify_before_minutes=0` 表示从计划放送时刻开始等待投递；大于 `0` 表示提前 N 分钟。
- `notify_before_minutes` 必须是 `0..1440` 的整数，`bool` 不得作为整数接受。
- `display_timezone` 只影响文案展示，不影响绝对时间比较；必须通过 `zoneinfo` 验证。
- 启用 `anime_push` 必须提供非空的 AniList Access Token；该 Token 只能存在 plugin-data 私密配置中。
- `media_id_overrides` 的 key 是 Bangumi subject ID，value 是 AniList media ID；两者必须是正整数。

### 6.2 Source 声明

宿主 proactive 是否启用由 Akashic Core 管理，插件只检查自己的开关。

```python
def proactive_sources(self) -> list[ProactiveSourceSpec]:
    config = self.context.config
    if not config.anime_push.enabled:
        return []
    return [
        ProactiveSourceSpec(
            id="anime_updates",
            channels=("alert",),
            server="bangumi",
            fetch_tool="get_anime_update_alerts",
            ack_tool="acknowledge_anime_update_alerts",
            fetch_page_size=50,
        )
    ]
```

稳定 source 身份为 `bangumi@<marketplace>:anime_updates`，具体 marketplace 由安装实例决定。

## 7. 刷新和检测流程

### 7.1 MCP 生命周期

FastMCP lifespan 在 `anime_push.enabled=true` 时启动一个受控后台任务，停止时取消并等待任务退出。网络请求不在 SQLite 写事务内执行。

后台任务维护三种节奏：

1. **catalog refresh**：启动时执行，随后默认每 6 小时执行一次并加入抖动。
2. **schedule refresh**：启动时执行，随后默认每 30 分钟执行一次并加入抖动。
3. **due evaluation**：每分钟只读取本地 schedule，计算是否进入提醒窗口；除到期前的观看状态复核外不访问网络。

这些频率属于插件缓存新鲜度，不驱动 Akashic agent，也不承诺外部消息的精确送达时刻。

### 7.2 Catalog refresh

1. 调用 `/v0/me` 取得 username。
2. 分页读取 `/v0/users/{username}/collections?subject_type=2&type=3`。
3. 对每个在看 subject 分页读取 `/v0/episodes?subject_id={id}&type=0`。
4. 用 Bangumi `episode_id` 保存普通章节及章节号；章节号缺失、非正数或同一 subject 内不唯一时不建立自动 schedule 映射。
5. 不再在看列表中的 subject 标记为 inactive；不删除既有 schedule、event 或 ACK。
6. 新发现或由 inactive 重新变为 active 的 subject 只登记未来 schedule，不补推已经过去的放送。

### 7.3 AniList 映射

匹配顺序：

1. 使用配置中的显式 media ID 覆盖并验证 AniList 类型为 anime。
2. 自动搜索时优先比较 Bangumi 原文名与 AniList native title 的规范化精确匹配。
3. 同时核对首播年份/季度、format 和可用的 episode count。
4. 只有唯一候选满足全部可用约束时才保存自动映射。
5. 中文译名包含匹配、模糊相似度和“第几季”文本猜测只能用于诊断候选，不得自动提交映射。
6. 映射失败或冲突时记录脱敏健康错误，并跳过该 subject；不得生成日期级提醒。

自动映射保存匹配依据的 fingerprint。Bangumi 或 AniList 的关键字段变化后必须重新验证，不得永久信任旧的模糊结果。

#### 7.3.1 GraphQL 请求契约

所有请求使用 `POST https://graphql.anilist.co` 和 JSON body，通过 GraphQL variables 传值，不把标题或 ID 拼入查询文本。显式配置 AniList Token 时才发送 `Authorization: Bearer ...`；Token 仍只来自 plugin-data 私密配置。

自动搜索固定读取第一批候选，MVP 使用 `page=1, perPage=25`：

```graphql
query SearchAnime($search: String!, $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo {
      total
      currentPage
      lastPage
      hasNextPage
    }
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      type
      format
      status
      episodes
      season
      seasonYear
      startDate {
        year
        month
        day
      }
      title {
        romaji
        english
        native
      }
      nextAiringEpisode {
        episode
        airingAt
      }
    }
  }
}
```

`search` 使用 Bangumi 原文名。插件只在这批候选中执行 7.3 的严格字段校验；零个或多个候选通过时都跳过自动映射，不继续翻页扩大模糊搜索，也不接受单纯的搜索排序第一名。

显式 media ID 验证和已保存映射的 schedule refresh 使用按 ID 查询：

```graphql
query AnimeById($id: Int!) {
  Media(id: $id, type: ANIME) {
    id
    type
    format
    status
    episodes
    season
    seasonYear
    startDate {
      year
      month
      day
    }
    title {
      romaji
      english
      native
    }
    nextAiringEpisode {
      episode
      airingAt
    }
  }
}
```

AniList 即使返回 HTTP `200` 也可能带顶层 `errors`；存在 `errors`、`data` 缺失、`Media` 为空或字段类型非法都按该 subject 刷新失败处理，不得覆盖旧映射或 schedule。`nextAiringEpisode=null` 只表示当前没有下一集，按 7.4 的空值规则处理。

### 7.4 Schedule refresh

1. 对 active 且映射已验证的 subject 使用 `AnimeById` 查询读取 AniList `nextAiringEpisode`。
2. 将 AniList episode number 唯一映射到 Bangumi 普通章节的 `episode_id`。
3. 保存或更新 `scheduled` 记录，包括 `airing_at`、`notify_at` 和来源 fingerprint。
4. 计算公式：

   ```text
   notify_at = floor_to_minute(airingAt) - notify_before_minutes
   due       = now >= notify_at
   ```

5. 同一 schedule 在进入 `pending` 前允许因 AniList 延期而更新 `airing_at` 和 `notify_at`。
6. AniList 已推进到下一集时，不覆盖上一集已经保存的 schedule。
7. AniList 返回空值时不创建新 schedule，也不删除已经持久化的未来 schedule；已有 schedule 由后续刷新或过期规则处理。

### 7.5 Due evaluation

对达到 `notify_at` 的 `scheduled` 记录：

1. subject 必须仍为 active，映射 fingerprint 必须仍有效。
2. 调用 `GET /v0/users/-/collections/-/episodes/{episode_id}`；必须使用两个字面量 `-`，不得替换为真实 username 或 subject ID。
3. 校验响应为单个 `{episode, type, updated_at}`，且 `episode.id` 等于待复核的 `episode_id`。
4. 逐集 `type == 2` 时转为 `suppressed`，不生成事件；这里不得套用条目收藏的 `1..5` 状态表。
5. 逐集 `type` 为 `0`、`1` 或 `3` 时视为尚未标记看过，可以继续生成提醒。
6. `400`、`401`、`404`、网络失败、响应结构非法或未知 `type` 都保持 `scheduled`，按失败策略重试；不得把失败或未知值当作未观看。
7. 尚未看过则在一个 SQLite 事务内写入不可变事件 payload，并转为 `pending`。
8. 对因停机而错过的 schedule，只在 `notify_at` 后 6 小时恢复为 pending；超过窗口转为 `expired`，避免重启后补推陈旧提醒。

## 8. 事件和 ACK 契约

### 8.1 Fetch

`get_anime_update_alerts(offset: int, limit: int)` 只读取 SQLite 中按 `created_at,event_id` 稳定排序的 pending 快照，不访问 Bangumi 或 AniList。`limit` 由 source 固定传入 `50`，offset 必须非负。

```json
[
  {
    "event_id": "episode:1234567",
    "kind": "alert",
    "source_type": "anime_schedule",
    "source_name": "Bangumi × AniList",
    "title": "《作品名》第 7 集计划于 20:00 放送",
    "content": "计划放送时间：2026-08-05 20:00（Asia/Shanghai）。这是放送提醒，不代表任何平台已经上线。",
    "severity": "low",
    "scheduled_at": "2026-08-05T12:00:00Z"
  }
]
```

- `event_id` 固定为 `episode:{bangumi_episode_id}`。
- 无论是否提前提醒，标题和正文都只陈述绝对计划放送时刻，不使用“将在 N 分钟后”等可能因 proactive 延迟而失真的相对时间。
- payload 在进入 `pending` 时冻结；后续标题或 schedule 编辑不改写已经待投递的事件。
- 返回不得包含 Access Token、AniList Token、原始响应或内部重试细节。

### 8.2 ACK

`acknowledge_anime_update_alerts(event_ids: list[str])` 在单个 SQLite 事务内执行：

1. 只接受本 source 已知的 `episode:<id>`。
2. `pending → acked`，记录 `acked_at`。
3. 已经 `acked` 的 ID 重复 ACK 成功返回，保持幂等。
4. 未知、`scheduled`、`suppressed` 或 `expired` 的 ID 使整批 ACK fail-loud，不做部分提交。

Akashic Core 只在真实外部送达后调用成功 ACK。dispatch 失败时 pending 保留；若消息已送达但插件 ACK 失败，Core delivery dedupe 会阻止相同 evidence 再次发送，并在后续去重路径重试 ACK。

外部 channel 成功返回后、Core 写入 delivery 状态前仍存在进程崩溃窗口，因此本设计不承诺端到端严格恰好一次。

## 9. 持久状态和保留边界

状态数据库固定为：

```text
<workspace>/plugin-data/bangumi-<marketplace>/anime_updates.db
```

最小逻辑对象：

| 对象 | 正常增加 | 允许原位更新 | 逻辑失效 | 物理删除 |
|---|---|---|---|---|
| subject mapping | 新在看条目或确认映射 | active、验证 fingerprint、最近核验时间 | inactive / invalid | MVP 不自动删除 |
| episode catalog | 新普通章节 | 标题、章节号和最近核验时间 | subject inactive | MVP 不自动删除 |
| schedule | 新 AniList future episode | 仅 `scheduled` 阶段更新时间和 fingerprint | suppressed / expired | MVP 不自动删除 |
| event | 到达提醒窗口时创建 | 仅 pending→acked 和 `acked_at` | acked | MVP 不自动删除 |
| poll state | 首次执行某类刷新 | 最近成功/失败、连续失败次数和 next retry | 配置禁用时停止推进 | MVP 不自动删除 |

- 普通卸载保留 plugin-data，重新安装后继续复用数据库。
- `enabled=false` 只停止 source 和刷新任务，不删除或重置数据库。
- 数据库缺失表示首次启用；创建 schema 后静默建立未来 schedule 基线。
- I/O、SQLite、schema 或完整性错误必须 fail-loud，不得解释为空状态或自动重建。
- 当前没有自动 retention 或物理减少协议。未来需要清理时必须单独设计影响预览、备份、恢复和确认。
- 恢复证据是 plugin-data 的 SQLite backup、`PRAGMA integrity_check`、schema version 和应用级只读 smoke。

## 10. 失败、重试和并发

### 10.1 外部失败

| 情况 | 行为 |
|---|---|
| 网络超时、连接失败、`5xx` | 保留旧快照；15 分钟起指数退避，最高 6 小时并加入抖动 |
| `429` | 优先遵守 `0..86400` 秒的合法 `Retry-After`，否则使用相同指数退避 |
| Bangumi `401/403` | 记录配置故障，24 小时后重试；不输出 Token |
| AniList `401/403` | 记录 Token 配置故障，不静默回退匿名访问，24 小时后重试 |
| 单个 subject 失败 | 保留该 subject 旧状态，其他 subject 可提交成功结果 |
| 在看列表失败 | 不改变 active/inactive 集合 |
| 映射冲突 | 标记 mapping invalid，不生成提醒 |

后台刷新失败不得清空现有 scheduled 或 pending。`fetch_tool` 继续返回已有 pending；如果 SQLite 不可读则 source 明确失败，不返回伪造的 `[]`。

### 10.2 并发和提交

- 同一 MCP 进程只允许一个刷新协调器；重复 tick 不启动第二个网络刷新。
- 网络响应先在内存中完成边界校验，再开启短 SQLite 事务提交。
- fetch 使用只读事务；ACK 与 due transition 使用写事务。
- 数据库启用 busy timeout；写事务冲突失败并重试，不静默丢弃状态。
- 进程停止时先阻止新 refresh，再取消并等待 in-flight；已经提交的 pending 保持可恢复。

## 11. 隐私和日志

- Bangumi Access Token 和 AniList Access Token 只存在 plugin-data 的 `config.local.toml`；启用 `anime_push` 时两者都必须配置。
- Token 不得进入 Git、命令参数、异常正文、对象 repr、MCP 返回或日志。
- HTTP 错误详情沿用现有 Bangumi Token 替换，并为 AniList Token 增加相同脱敏。
- 日志只记录 subject ID、episode ID、AniList media ID、状态转换、请求分类、HTTP 状态码和退避时间。
- 不记录 Authorization header、完整配置、原始 API 响应、用户评论或标签。
- 事件正文只包含提醒所需的作品名、集数和计划放送时间。

## 12. 实施范围

### `plugin.py`

- 新增 `AnimePushConfig` 和私密字段。
- 声明 alert source。
- 发布时把插件版本提升到 `0.5.0`。

### `mcp/src/client.py`

- 新增普通章节分页读取，并在现有条目逐集收藏分页能力之外增加按 `episode_id` 查询单集收藏的方法。

### `mcp/src/anilist.py`

- 新增 AniList client，使用固定 GraphQL 查询，并统一超时、状态码、`Retry-After` 和敏感信息脱敏。

### `mcp/src/anime_updates.py`

- 拥有 SQLite schema、映射、刷新协调器、due evaluation、fetch 和 ACK。
- 接受注入 clock 和假 client；测试不得通过 MCP 参数修改 `now`。

### `mcp/src/server.py`

- 用 FastMCP lifespan 启停刷新协调器。
- 注册 `get_anime_update_alerts` 和 `acknowledge_anime_update_alerts`。

### 文档和 Skill

- README 增加私密配置、精度承诺、独立开关和故障说明。
- `bangumi` Skill 只说明如何解释放送提醒，不授予模型直接修改 reminder 状态的工具。

## 13. 验证和验收

### 13.1 合同

1. `anime_push.enabled=false` 时 source 列表为空，现有 MCP 和 Skill 能力不变。
2. source 声明 `channels=("alert",)`，每项 `kind="alert"` 且包含稳定 `event_id`。
3. Core readiness 能发现 fetch/ACK 工具，MCP stdio 和 lifespan 正常启动、停止。
4. Token 不出现在 repr、异常、日志、工具结果或测试失败文本中。

### 13.2 时间和 schedule

1. `notify_at = floor_to_minute(airingAt) - notify_before_minutes`。
2. 到达 `notify_at` 前不产生 pending，到达后在下一次本地 evaluation 产生一次。
3. 放送前延期会更新 scheduled 时间；已经 pending 的 payload 不被改写。
4. AniList 从第 N 集推进到第 N+1 集时，第 N 集已保存 schedule 不丢失。
5. 重启后恢复未到期 schedule 和 pending；停机错过不超过 6 小时的提醒可恢复，超过窗口转 expired。
6. 实际 proactive 投递允许晚于计划时间，不断言同一分钟送达。

### 13.3 映射和观看状态

1. 显式 media ID 覆盖通过基本验证后生效。
2. `SearchAnime` 使用 variables 和固定候选上限；唯一严格候选自动映射，模糊、多候选和季数冲突全部拒绝。
3. `AnimeById` 能验证显式覆盖并刷新 `nextAiringEpisode`；HTTP `200` 中的 GraphQL `errors` 仍按失败处理。
4. AniList 集数必须唯一映射到 Bangumi 普通章节 `episode_id`。
5. 条目逐集分页路径只能使用 `/v0/users/-/collections/{subject_id}/episodes`；单集复核路径只能使用 `/v0/users/-/collections/-/episodes/{episode_id}`。
6. 单集响应的 `episode.id` 必须匹配；逐集 `type == 2` 时转 suppressed，`0/1/3` 不得误判为条目收藏状态。
7. 单集读取失败、未知 `type` 或非法响应时不假定未看。
8. subject 不再在看时不产生新的 pending；重新在看只登记未来 schedule。

### 13.4 ACK、恢复和故障

1. 冷启动静默建立未来基线，不补推过去提醒。
2. pending 重复 fetch 返回相同 event ID；成功 ACK 后不再返回。
3. ACK 重复调用幂等；混入未知 ID 时整批不提交。
4. dispatch 失败不 ACK；ACK 失败后 pending 保留，并由 Core delivery dedupe 补偿。
5. Bangumi/AniList 暂时失败保留旧 schedule 和 pending，并按退避重试。
6. 损坏数据库、未知 schema 和非法持久字段 fail-loud，不重建为空库。
7. 并发 refresh、fetch 和 ACK 不产生重复 event、丢失 pending 或部分 ACK。
8. 全部测试使用假 HTTP client、临时时钟和临时 plugin-data，不访问真实账户或正式 workspace。

## 14. 回滚

运行时回滚优先把 `[anime_push].enabled` 设为 `false`，由插件热重载移除 source 并停止刷新任务。查询和写入能力继续服务，`anime_updates.db` 保留。

代码回滚到不声明 source 的版本时同样不删除数据库。重新安装支持该 schema 的版本后可以恢复 schedule、pending 和 ACK。永久删除提醒数据不属于本功能，必须由名称明确、带备份和单独确认的数据管理操作实现。

# 用户收藏列表设计

- 状态：Accepted
- 目标版本：`0.2.0`
- 后续修订：`0.3.0` 起的工具拆分与展示语义见 [收藏查询意图与展示边界](collection-query-intent.md)，该文档取代本文的 MCP 参数与 Skill 分页规则。
- 日期：2026-08-03
- 依据：[Bangumi 官方 API 文档](https://bangumi.github.io/api/) 与 [OpenAPI 定义](https://bangumi.github.io/api/dist.json)

## 1. 背景

插件当前可以查询单个条目是否收藏、收藏状态和动画逐集进度，但不能回答以下跨条目问题：

- 我正在看哪些动画？
- 我搁置了哪些作品？
- 我的动画收藏有多少条？
- 列出某一种收藏状态下的作品。

下一阶段增加当前 Token 所属用户的收藏列表查询。这里的“完整”表示所有收藏都能通过稳定分页访问，而不是在一次 MCP 调用中返回整个账号的全部数据。

## 2. 目标

1. 查询当前用户的收藏列表。
2. 按条目类型和收藏状态过滤。
3. 返回明确的分页信息，使全部收藏可以逐页访问。
4. 为每条收藏提供适合对话使用的有界摘要。
5. 保持现有 Token、日志和 Bangumi 写入安全边界不变。
6. 避免为列表中的每个动画额外请求逐集状态。

## 3. 非目标

首个版本不实现：

- 任意 Bangumi 用户的收藏查询；只查询当前 Access Token 所属用户。
- 将完整收藏导出为文件。
- 按标题进行服务端搜索或自定义排序。
- 返回收藏评论、用户标签、条目简介或图片。
- 缓存或同步完整收藏到 plugin-data。
- 批量修改收藏状态或观看进度。
- 根据 `updated_at` 实现可靠的“最近更新”排序。

## 4. 官方 API 合同

使用：

```text
GET /v0/users/{username}/collections
```

用户名先通过现有的 `GET /v0/me` 获取。这样可以读取 Token 所属用户的私有收藏，同时不向 MCP 调用方开放任意用户名参数。

官方查询参数：

| 参数 | 约束 | 设计映射 |
|---|---|---|
| `subject_type` | 可选，`1/2/3/4/6` | 书籍、动画、音乐、游戏、三次元 |
| `type` | 可选，`1` 至 `5` | 想看、看过、在看、搁置、抛弃 |
| `limit` | `1` 至 `50`，官方默认 `30` | 插件默认 `10`，最大 `50` |
| `offset` | 大于等于 `0` | 插件默认 `0` |

响应为 `Paged_UserCollection`：

```json
{
  "total": 120,
  "limit": 10,
  "offset": 0,
  "data": []
}
```

每条 `UserSubjectCollection` 包含 `subject_id`、`subject_type`、收藏状态、评分、`ep_status`、`vol_status`、隐私标记和可选的 `subject` 摘要。

官方文档明确指出，修改评分、评价或章节观看状态时，`updated_at` 可能不会更新。插件可以原样展示该字段，但不得把它解释为可靠的收藏修改时间，也不得据此承诺“最近更新”排序。

## 5. MCP 工具合同

新增只读工具：

```python
list_collections(
    subject_type: Literal[
        "all", "book", "anime", "music", "game", "real"
    ] = "all",
    status: Literal[
        "all", "wish", "completed", "watching", "on_hold", "dropped"
    ] = "all",
    limit: int = 10,
    offset: int = 0,
) -> str
```

参数映射：

| MCP 值 | API 值 | 中文含义 |
|---|---:|---|
| `book` | `subject_type=1` | 书籍 |
| `anime` | `subject_type=2` | 动画 |
| `music` | `subject_type=3` | 音乐 |
| `game` | `subject_type=4` | 游戏 |
| `real` | `subject_type=6` | 三次元 |
| `wish` | `type=1` | 想看 |
| `completed` | `type=2` | 看过 |
| `watching` | `type=3` | 在看 |
| `on_hold` | `type=4` | 搁置 |
| `dropped` | `type=5` | 抛弃 |

`all` 表示不向 Bangumi 发送对应过滤参数。`bool` 不得作为整数参数接受；越界的 `limit` 或 `offset` 必须在请求前失败。

### 5.1 返回结构

```json
{
  "user": {
    "username": "example"
  },
  "filters": {
    "subject_type": "anime",
    "status": "watching"
  },
  "page": {
    "total": 12,
    "limit": 10,
    "offset": 0,
    "returned": 10,
    "has_more": true,
    "next_offset": 10
  },
  "collections": [
    {
      "subject": {
        "id": 123,
        "title": "作品名",
        "type": 2,
        "type_value": "anime",
        "type_label": "动画",
        "episodes": 12,
        "volumes": 0
      },
      "collection": {
        "type": 3,
        "status": "watching",
        "status_label": "在看",
        "rating": 0,
        "reported_episode_progress": 5,
        "reported_volume_progress": 0,
        "private": false,
        "updated_at": "2026-08-03T12:00:00+08:00"
      }
    }
  ]
}
```

### 5.2 字段规则

- `subject.title` 优先使用非空 `name_cn`，否则使用 `name`，并沿用现有标题清理和长度上限。
- 官方 schema 中 `subject` 是可选字段。缺失时仍返回 `subject_id` 和 `subject_type`，`title`、`episodes`、`volumes` 设为 `null`，不得为补齐标题逐条调用条目接口。
- `reported_episode_progress` 是列表接口的只读 `ep_status` 摘要，不代表从第 1 集开始连续看过，也不用于任何写操作。
- 需要准确逐集状态或检查中间缺集时，调用现有 `get_collection_status(subject_id)`。
- 不返回 `comment`、用户 `tags`、条目简介和图片，避免把长文本、私密内容及无关媒体放入模型上下文。
- 保留 API 数值、稳定英文值和中文标签，既方便程序判断，也方便用户阅读。

## 6. 分页与“完整”语义

工具每次只发起一页收藏请求，不在 MCP 层自动追逐所有分页：

- 默认 `limit=10`。
- 最大 `limit=50`，与官方合同一致。
- `has_more` 根据 `offset + returned < total` 计算。
- 有下一页时，`next_offset = offset + returned`。
- 没有下一页时，`next_offset = null`。
- 当 `returned == 0` 但 `offset < total` 时，视为远端分页提前结束并明确报错。

所有条目都能通过 `next_offset` 继续访问，因此列表在数据覆盖上是完整的。单次结果保持有界，避免把大型收藏一次性注入上下文。

收藏可能在翻页期间被用户或其他客户端修改，官方接口不提供事务快照。因此跨页读取不能承诺绝对一致的时间点视图。工具不静默重试，也不伪造快照身份。

## 7. Skill 行为

更新 `bangumi` Skill：

1. 用户询问某类收藏时，优先使用最窄的 `subject_type` 和 `status` 过滤。
2. 普通“列出”请求默认只读取第一页，并告诉用户总数及是否还有下一页。
3. 只询问数量时，可以用 `limit=1` 读取 `total`，不扫描全部条目。
4. 用户明确要求继续时，使用返回的 `next_offset`，不得猜测 offset。
5. 需要跨页分析时逐页汇总，不把所有原始页面重新拼接到最终回复。
6. 单轮自动读取最多 4 页、共 200 条；超过后要求用户缩小类型或状态范围。完整文件导出留给后续专用能力。
7. 列表结果中的 `reported_episode_progress` 只能称为“列表记录的进度”。需要精确逐集结论时查询单条目。
8. 此工具完全只读，不需要写入确认，也不得与批量写入流程绑定。

## 8. 代码边界

### `mcp/src/client.py`

新增一页读取方法，负责：

- 对用户名进行 URL 编码。
- 发送官方过滤和分页参数。
- 验证响应是对象，且 `total/limit/offset/data` 类型合法。
- 验证 `data` 中每项为对象。
- 不自动读取下一页。

### `mcp/src/service.py`

负责：

- 校验并映射 MCP 枚举与整数范围。
- 通过 `/v0/me` 解析当前用户名。
- 将官方响应归一化为有界返回结构。
- 处理可选 `subject`，不产生 N+1 请求。
- 计算 `has_more` 和 `next_offset`。

### `mcp/src/server.py`

注册 `list_collections`，工具描述必须明确“只读、分页、当前 Token 用户”。

### `skills/bangumi/SKILL.md`

增加列表查询路由、分页规则、进度摘要限制和单轮自动读取上限。

### `plugin.py`

实现发布时将插件版本提升为 `0.2.0`，确保 Akashic GitHub 安装得到新的不可变 cache 版本。

## 9. 安全与隐私

- 继续只连接 `https://api.bgm.tv`。
- Access Token 继续只从 plugin-data 的 `config.local.toml` 读取。
- 请求头、异常、日志和工具结果不得包含 Token。
- 收藏列表可能包含私有收藏，MCP 结果只用于当前会话，不写入仓库或持久缓存。
- 不记录完整 Bangumi 响应正文。
- 新能力没有 Bangumi 写操作，不复用或创建确认记录。
- 现有状态与逐集写入仍必须执行 prepare、下一轮逐字确认、单次 commit。

## 10. 错误语义

| 情况 | 行为 |
|---|---|
| 过滤枚举无效 | 请求前抛出 `BangumiInputError` |
| `limit` 或 `offset` 越界 | 请求前抛出 `BangumiInputError` |
| Token 无效或权限不足 | 保留脱敏后的 Bangumi HTTP 错误 |
| 当前用户缺少 username | 明确报错，不猜测用户身份 |
| 分页结构或收藏条目无效 | fail-loud，不返回部分伪成功结果 |
| 可选 `subject` 缺失 | 保留收藏，返回有限 subject 信息 |
| 请求超时 | 报告读取失败；只读调用可由用户重新发起 |
| 翻页期间总数变化 | 不声称事务一致性，继续以每页返回为准 |

## 11. 测试计划

### Client 测试

- 使用编码后的当前用户名访问正确 endpoint。
- `all` 过滤不会发送 `subject_type` 或 `type`。
- 其他过滤值映射为正确 API 数值。
- `limit` 和 `offset` 原样发送。
- 合法分页响应被解析。
- 非对象分页、非法计数、非数组 data 和非法条目 fail-loud。
- API 错误即使回显 Token 也会脱敏。

### Service 测试

- 默认参数返回第一页和正确分页元数据。
- 五种条目类型与五种收藏状态映射正确。
- 拒绝 bool、越界 limit 和负 offset。
- 中文标题优先，空中文标题回退原名。
- 缺少 `subject` 时保留条目且不触发额外请求。
- `ep_status` 只映射为 `reported_episode_progress`。
- 空页、最后一页和中间页的 `next_offset` 正确。
- 单次服务调用只产生一次 `/v0/me` 和一次列表请求。

### Skill 与合同测试

- Skill 明确列表进度不是准确连续逐集状态。
- Skill 明确分页和单轮自动读取上限。
- 新工具只读，不进入确认存储。
- Plugin API v2 合同继续通过。
- FastMCP 启动后能列出新增工具。
- GitHub 安装和 committed generation 加载 smoke 继续通过。

## 12. 验收标准

1. 能查询当前用户未过滤或按类型、状态过滤的收藏页。
2. `limit` 始终不超过 50，返回值包含可继续请求的分页信息。
3. 列表中的每项都能稳定关联到 Bangumi `subject_id`。
4. 可选 subject 缺失不会丢掉合法收藏，也不会产生 N+1 请求。
5. 不把 `ep_status` 描述成准确连续观看进度，不用它执行写入。
6. 不依赖 `updated_at` 实现“最近更新”语义。
7. 不新增 Token、日志、缓存或写入风险。
8. 全部测试、Plugin API v2 合同和真实 Akashic 安装 smoke 通过后才能发布 `0.2.0`。

## 13. 后续方向

在分页列表稳定后，再分别设计：

- 只返回聚合结果的收藏统计工具。
- 面向文件而非模型上下文的完整收藏导出。
- 用户标签和评论的显式按需读取。
- 基于列表候选集、再调用单条目接口的精确动画进度分析。

这些能力不得通过扩大 `list_collections` 的默认返回体隐式加入。

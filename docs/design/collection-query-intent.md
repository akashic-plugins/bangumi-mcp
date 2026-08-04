# 收藏查询意图与展示边界

- 状态：`Accepted`
- 目标版本：`0.3.0`
- 官方依据：[Bangumi API](https://bangumi.github.io/api/) 与 [OpenAPI 定义](https://bangumi.github.io/api/dist.json)
- 补充并修正：[用户收藏列表设计](collection-list.md) 的 MCP 工具与 Skill 分页语义
- 后续修订：`0.4.0` 起的完整/大量查询确认、不可见 `offset` 和无 200 条上限语义见 [收藏完整与大量查询确认边界](collection-query-confirmation.md)，该文档取代本文的直接完整查询行为。

## 1. 问题

Bangumi 收藏列表接口的 `limit` 取值为 `1` 至 `50`，官方默认为 `30`。插件 `0.2.0` 将 `limit` 直接暴露给模型，虽然默认值是 `10`，模型仍可在普通“列出”请求中传入 `50`，导致用户未要求全部结果时收到过长列表。

反过来，如果用户明确说“全部列出”，仍然只返回 10 条也不符合请求。API 运输批量与用户展示数量必须由查询意图决定，不能由同一个自由 `limit` 参数同时承担。

## 2. 行为决策

| 用户意图 | MCP 工具 | Bangumi 请求 | 最终展示 |
|---|---|---|---|
| 普通“列出” | `list_collections` | 固定 `limit=10` | 当前页最多 10 条，同时说明总数与是否有下一页 |
| 明确“继续/下一页” | `list_collections` | 固定 `limit=10`，使用上页 `next_offset` | 下一页最多 10 条 |
| 只询数量 | `count_collections` | `limit=1` | 只返回总数 |
| 明确“全部列出” | `list_all_collections` | 每页 `limit=50` 分批取完 | 列出全部返回条目，不再截成 10 条 |
| 明确完整统计或跨条目分析 | `list_all_collections` | 每页 `limit=50` 分批取完 | 只输出所需结论，不重复全部原始数据 |

普通列表不预取用户尚未请求的条目。明确完整读取则使用官方最大分页大小，减少 API 往返。

## 3. MCP 工具合同

### `list_collections`

```python
list_collections(
    subject_type: SubjectTypeFilter = "all",
    status: CollectionStatusFilter = "all",
    offset: int = 0,
) -> str
```

公开 schema 不包含 `limit`。服务层始终向 Bangumi 传入 `10`。

### `count_collections`

```python
count_collections(
    subject_type: SubjectTypeFilter = "all",
    status: CollectionStatusFilter = "all",
) -> str
```

只取一条来获得 `total`，不返回收藏条目。

### `list_all_collections`

```python
list_all_collections(
    subject_type: SubjectTypeFilter = "all",
    status: CollectionStatusFilter = "all",
) -> str
```

工具内部每页读取 50 条，直到获得全部结果。返回 `complete=true`、`total`、`returned`、`api_page_size` 和 `request_count`。

## 4. 完整性与上限

- 完整读取最多 200 条，即最多 4 个 Bangumi 分页。
- 首页 `total` 超过 200 时立即失败，并要求缩小类型或状态范围。
- 跨页期间 `total` 发生变化时失败，不把时间点不一致的结果声称为完整快照。
- 跨页出现重复 `subject_id` 时失败。
- 空页、分页参数不匹配或条目数超过 `limit` 时 fail-loud。
- 不引入持久缓存，不将收藏列表写入 plugin-data。

## 5. Skill 语义

- 普通“列出”不能自动追逐后续页。
- “继续”必须使用上一页返回的 `next_offset`。
- 只有明确的“全部/所有/完整”意图才可调用完整工具。
- “全部列出”必须展示全部返回条目；不得以默认页大小再次截断。
- 完整分析只输出用户要求的结论。

## 6. 验收

1. “列出我搁置的动画”在共 24 部时只返回前 10 部，并说明总数为 24。
2. “全部列出我搁置的动画”在共 24 部时只发起一次 `limit=50` 请求，并返回全部 24 部。
3. 120 条完整读取使用 `offset=0/50/100` 三个分页。
4. 201 条完整读取明确失败，不返回伪完整结果。
5. 数量查询使用 `limit=1`。
6. 列表查询仍为只读，不改变任何写入确认边界。

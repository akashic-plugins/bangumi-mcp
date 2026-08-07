---
name: bangumi
description: 分页查询用户的 Bangumi 收藏列表、单个条目状态和动画逐集观看进度，解释计划放送提醒，并在用户对当前预览逐字确认后设置条目状态或推进动画进度。用于用户提到 Bangumi 收藏、在看、看过、看到第几集、更新观看进度或收到番剧放送提醒时。
---

# Bangumi

使用 `bangumi` MCP 查询收藏，并严格执行只读大量查询确认和一次一确认的写入流程。

## 收藏列表

查询当前 Token 用户的收藏。优先使用最窄的作品类型和收藏状态过滤，不要请求任意其他用户名。

- 普通“列出”请求调用 `list_collections`；它固定返回最多 10 条、总数和不透明 `query_id`。不得在同一轮自动追逐后续页。
- 用户明确要求“继续”或“下一页”时，才调用 `continue_collection_query(query_id)`。不得猜测或自行构造 offset；只使用上一页返回的 `query_id`。
- 用户只询问数量时调用 `count_collections`；它只读取 1 条来取得总数，不扫描全部收藏。
- 任何需要遍历全部候选收藏的请求，包括“全部/所有/完整”、完整分析和评分筛选，都先调用 `prepare_collection_query`。即使候选集少于 100 条，或用户已经明确说“全部”，也必须显示 `target` 和 `confirmation_text` 后结束本轮。
- 普通分页累计读取最多 90 条不需要确认；下一页会使累计读取达到或超过 100 条时，`continue_collection_query` 会返回确认预览。此时同样显示预览并结束本轮，不得继续读取。
- 下一条用户消息去除首尾空白后必须与查询 `confirmation_text` 逐字一致，才调用 `execute_prepared_collection_query`。prepare 和 execute 禁止在同一轮调用；模糊同意、旧确认或改变条件都要重新预览。
- 一次查询确认覆盖同一固定计划的全部分页和内存结果翻页，不得每 50 条或每个展示页重复确认。查询确认是只读授权，不能传给 `commit_prepared_update`。
- 评分条件使用 `operation="filter"` 和 `min_rating`/`max_rating`，由插件确认后以每页 50 条扫描并筛选；不得让模型用 10 条展示页手工扫描。
- 对“全部列出”，使用 `return_all_matches=True`（评分筛选）或 `operation="list_all"`，最终回复必须保留全部结果语义。完整查询没有 100 或 200 条静默上限。
- 完整统计使用 `operation="analyze"`，只输出用户要求的结论，不重复粘贴全部原始数据。
- `reported_episode_progress` 只是列表记录的只读进度摘要，不代表从第 1 集开始连续看过，也不得用于写入。

## 单条目查询

调用 `get_collection_status(subject_id)`。如用户未提供条目 ID 或 Bangumi URL，先请用户提供；不要猜测 ID。

结果中的动画进度要区分：

- `highest_watched_episode`：最高标记为看过的集数。
- `watched_through_episode`：从第 1 集开始连续看过的集数。
- `unwatched_before_highest` 非空时，明确说明中间存在未标记集数，不要把最高集数说成连续进度。

## 写入

每次写入严格执行以下顺序：

1. 设置条目状态时调用 `prepare_collection_status_update`；更新动画进度时调用 `prepare_anime_progress_update`。
2. 向用户完整显示返回的 `target` 和 `confirmation_text`。
3. 立即结束本轮。禁止在调用 prepare 的同一轮调用 `commit_prepared_update`，即使用户此前给过长期授权或说过“以后都确认”。
4. 下一条用户消息只有在去除首尾空白后与 `confirmation_text` 逐字一致，才算本次明确确认。模糊同意、旧确认、修改目标或新的附带条件都不算；重新预览。
5. 把当前用户消息原文作为 `confirmation_text`，连同对应 `confirmation_id` 调用 `commit_prepared_update`。
6. 报告实际结果。确认过期、目标变化或结果未知时不得自动重试写入；先重新查询，再重新预览。

条目状态只支持“在看”和“看过”。动画进度必须使用逐集接口；不得尝试通过条目级 `ep_status` 修改动画集数，也不得把设置进度和设置条目状态合并为一次未单独确认的操作。

## 放送提醒

主动提醒中的时间是 AniList 提供的计划放送时间。向用户解释时必须保持以下边界：

- 只能说该集“计划放送”，不能说某个流媒体、字幕组或下载源已经上线。
- 提醒可能因 Akashic proactive tick、会话繁忙或外部 channel 状态延后，不要把实际送达时间解释为计划时间发生变化。
- 标题和正文中的绝对时间及其时区是权威展示，不要改写为“刚刚”“N 分钟后”等相对时间。
- `get_anime_update_alerts` 和 `acknowledge_anime_update_alerts` 由 Akashic 主动投递链路调用。模型不得为了查询、重放、跳过或修改提醒而手动调用它们。
- 用户想停止提醒时，说明应在 plugin-data 私密配置中设置 `[anime_push].enabled = false`；不要删除或重建提醒数据库。

## 凭据

不要请求用户在对话中粘贴 Access Token。Token 只应写入 Akashic 插件数据目录的 `config.local.toml`，不得出现在回复、日志、命令参数或工具结果中。

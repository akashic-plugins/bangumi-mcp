---
name: bangumi
description: 分页查询用户的 Bangumi 收藏列表、单个条目状态和动画逐集观看进度，并在用户对当前预览逐字确认后设置条目状态或推进动画进度。用于用户提到 Bangumi 收藏、在看、看过、看到第几集或更新观看进度时。
---

# Bangumi

使用 `bangumi` MCP 查询收藏，并严格执行一次一确认的写入流程。

## 收藏列表

查询当前 Token 用户的收藏。优先使用最窄的作品类型和收藏状态过滤，不要请求任意其他用户名。

- 普通“列出”请求调用 `list_collections`；它固定返回最多 10 条。只读取一页，并说明总数和是否还有下一页。
- 用户明确要求“继续”或“下一页”时，才再调用 `list_collections`，并使用上一页的 `next_offset`；不得猜测 offset，不得在普通列出的同一轮自动追逐后续页。
- 用户只询问数量时调用 `count_collections`；它只读取 1 条来取得总数，不扫描全部收藏。
- 只有用户明确说“全部列出”、“所有”、“完整列表”，或明确要求基于全部结果分析时，才调用 `list_all_collections`。该工具向 Bangumi 每页 50 条分批取完。
- 对“全部列出”，最终回复必须列出工具返回的全部条目，不得再截成 10 条。对完整统计或分析，只输出用户要求的结论，不重复粘贴全部原始数据。
- `list_all_collections` 最多 200 条。超过时如实说明工具错误，要求用户缩小作品类型或收藏状态范围；不得把前 200 条声称为全部。
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

## 凭据

不要请求用户在对话中粘贴 Access Token。Token 只应写入 Akashic 插件数据目录的 `config.local.toml`，不得出现在回复、日志、命令参数或工具结果中。

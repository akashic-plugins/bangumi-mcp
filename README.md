# bangumi-mcp

Akashic Plugin API v2 插件，用 Bangumi 官方 API 分页查询收藏列表、查询单个条目的准确进度、设置“在看/看过”，通过章节接口逐集更新动画观看进度，并按 AniList 计划放送时间主动提醒在看动画的新一集。

## 安全边界

- 只连接 `https://api.bgm.tv` 和 `https://graphql.anilist.co`，使用符合官方要求的 User-Agent。
- Bangumi Access Token 和 AniList Access Token 只从 Akashic plugin-data 的 `config.local.toml` 读取，不写入仓库、日志或工具结果。
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

登录 Bangumi 后，前往 [Bangumi Access Token 页面](https://next.bgm.tv/demo/access-token) 生成个人 Token。该 Token 是账号的 API 访问凭据，不是 Bangumi 密码。

在该目录创建权限为 `0600` 的 `config.local.toml`：

```toml
access_token = "<在此填写 Bangumi Access Token>"
user_agent = "akashic-plugins/bangumi-mcp/0.5.0 (https://github.com/akashic-plugins/bangumi-mcp)"

[anime_push]
enabled = true
notify_before_minutes = 0
display_timezone = "Asia/Shanghai"
anilist_token = "<在此填写 AniList Access Token>"

# 只有自动匹配失败时才需要配置；左侧是 Bangumi subject ID。
[anime_push.media_id_overrides]
"501963" = 123456
```

已安装旧版本的用户如果在本地配置中显式设置了 `user_agent`，更新插件后也需要将其改为上述新标识。不要把 Token 放进命令行参数或聊天消息。配置完成后检查：

```bash
.venv/bin/python main.py plugin-doctor bangumi@github
```

运行中的 Akashic 会观察配置变化并发布新的 committed generation；新会话会加载 `bangumi` Skill 和 MCP 工具。

## 放送提醒

- 提醒依据是 AniList `nextAiringEpisode.airingAt`，精度规范化到分钟；它表示计划放送，不代表流媒体、字幕或片源已经上线。
- `notify_before_minutes=0` 从计划放送时刻开始等待投递；可设置为 `0` 至 `1440` 的整数以提前提醒。
- 到达提醒时刻后，Akashic 在下一次 proactive tick 读取事件。Gate、Judge、会话繁忙或 channel 状态可能使消息延后数分钟。
- 插件只处理 Bangumi 标记为“在看”的动画，并在生成提醒前通过逐集接口确认该集尚未标记为“看过”。
- AniList 与 Bangumi 没有共同稳定 ID。自动匹配只接受唯一严格候选；失败时可在私密配置的 `media_id_overrides` 中明确指定 AniList media ID。
- 启用 `anime_push` 必须配置 AniList Access Token；它只能写入上述 plugin-data 私密文件，不要放在命令行、对话或仓库中。
- 缓存、待投递事件和 ACK 保存在同一 plugin-data 目录下的 `anime_updates.db`。关闭 `anime_push.enabled` 不会删除该数据库。

## 设计文档

- [用户收藏列表设计](docs/design/collection-list.md)
- [收藏查询意图与展示边界](docs/design/collection-query-intent.md)
- [收藏完整与大量查询确认边界](docs/design/collection-query-confirmation.md)
- [追更放送提醒设计](docs/design/anime-update-push.md)

## 开发验证

```bash
python -m pip install -r mcp/requirements.txt -r requirements-dev.txt
pytest
PYTHONPATH=/path/to/plugin-contracts \
  python -m akashic_plugin_contracts check plugin.py
```

单元测试全部使用假的 HTTP 会话，不访问真实 Bangumi 账户，也不需要 Access Token。

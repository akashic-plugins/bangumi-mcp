# bangumi-mcp

Akashic Plugin API v2 插件，用 Bangumi 官方 API 分页查询收藏列表、查询单个条目的准确进度、设置“在看/看过”，并通过章节接口逐集更新动画观看进度。

## 安全边界

- 只连接 `https://api.bgm.tv`，使用官方 Access Token 和符合官方要求的 User-Agent。
- Access Token 只从 Akashic plugin-data 的 `config.local.toml` 读取，不写入仓库、日志或工具结果。
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
user_agent = "akashic-plugins/bangumi-mcp/0.4.0 (https://github.com/akashic-plugins/bangumi-mcp)"
```

已安装旧版本的用户如果在本地配置中显式设置了 `user_agent`，更新插件后也需要将其改为上述新标识。不要把 Token 放进命令行参数或聊天消息。配置完成后检查：

```bash
.venv/bin/python main.py plugin-doctor bangumi@github
```

运行中的 Akashic 会观察配置变化并发布新的 committed generation；新会话会加载 `bangumi` Skill 和 MCP 工具。

## 设计文档

- [用户收藏列表设计](docs/design/collection-list.md)
- [收藏查询意图与展示边界](docs/design/collection-query-intent.md)
- [收藏完整与大量查询确认边界](docs/design/collection-query-confirmation.md)

## 开发验证

```bash
python -m pip install -r mcp/requirements.txt -r requirements-dev.txt
pytest
PYTHONPATH=/path/to/plugin-contracts \
  python -m akashic_plugin_contracts check plugin.py
```

单元测试全部使用假的 HTTP 会话，不访问真实 Bangumi 账户，也不需要 Access Token。

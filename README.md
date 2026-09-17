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

V3 插件不再读取手写的 `config.local.toml`；配置由插件自己的配置程序在显式 setup 时写入固定输入与私有凭据库。**不要把 Token 放进命令行参数**（会进入进程列表），用环境变量传递：

```bash
export AKASHIC_BANGUMI_ACCESS_TOKEN="<Bangumi Access Token>"

.venv/bin/python main.py setup          # 选择 bangumi，运行插件 configure.py
```

可选配置项（同样只用环境变量）：

| 环境变量 | 含义 |
|---|---|
| `AKASHIC_BANGUMI_ANILIST_TOKEN` | AniList Access Token；放送提醒需要 |
| `AKASHIC_BANGUMI_ANIME_PUSH_ENABLED` | `1` / `true` 启用放送提醒抓取 |
| `AKASHIC_BANGUMI_ANIME_PUSH_BEFORE_MINUTES` | 提前提醒分钟数，`0` 至 `1440` |
| `AKASHIC_BANGUMI_DISPLAY_TIMEZONE` | 展示用 IANA 时区，默认 `Asia/Shanghai` |
| `AKASHIC_BANGUMI_USER_AGENT` | 覆盖默认 User-Agent |

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
.venv/bin/python main.py setup          # 选择 bangumi 后运行 configure.py --upgrade
```

升级会把明文 Token 转入私有凭据库、写出 `config.input.json`，并把旧文件与其命名备份保存在 `.plugin-credentials/bangumi-github/upgrades/` 的恢复点里，便于人工核对。

## 放送提醒

> 当前 V3 版本**只保留抓取与查询**：插件会把到期事件写入本地数据库，并提供 `get_anime_update_alerts` / `acknowledge_anime_update_alerts` 手动工具，但**暂不自动投递**。原因见[迁移方案](docs/design/plugin-v3-migration.md)：上游 V3 尚未提供外部插件可用的公开主动信息源能力。

- 提醒依据是 AniList `nextAiringEpisode.airingAt`，精度规范化到分钟；它表示计划放送，不代表流媒体、字幕或片源已经上线。
- 插件只处理 Bangumi 标记为“在看”的动画，并在记录提醒前通过逐集接口确认该集尚未标记为“看过”。
- AniList 与 Bangumi 没有共同稳定 ID。自动匹配只接受唯一严格候选；失败时可用固定配置中的 `media_id_overrides` 明确指定 AniList media ID。
- 缓存与待投递事件保存在 `plugin-data/bangumi-github/anime_updates.db`；关闭 `anime_push.enabled` 不会删除该数据库。

## 设计文档

- [插件 V2 → V3 迁移方案](docs/design/plugin-v3-migration.md)
- [用户收藏列表设计](docs/design/collection-list.md)
- [收藏查询意图与展示边界](docs/design/collection-query-intent.md)
- [收藏完整与大量查询确认边界](docs/design/collection-query-confirmation.md)
- [追更放送提醒设计](docs/design/anime-update-push.md)

## 开发验证

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
pytest
```

单元测试全部使用假的 HTTP 会话，不访问真实 Bangumi 账户，也不需要 Access Token。

# neuro的彩蛋博客

AstrBot 的静态周记插件，附带可选的历史美股模拟游戏。版本 **v0.1.0**，与提取时 AstrBot 的版本一致。

- **记忆周记**：北京时间周日 21:00 集中评价本周新增记忆；没有适合发布的内容就不写。
- **炒股周记（可选）**：北京时间周三 21:00 连续回放五个历史交易日，然后发布「这一周，neuro玩炒股游戏」。不是现实里每天等待半小时。
- **同一个首页**：纸张、胶带、粉色星星；每条短评固定配表情，不采用聊天的概率出图。
- **表情轮换**：同标签先打乱图库，用完候选再重复，并避免相邻重复；同篇重渲染稳定。
- **资讯对得上决策**：只展示明确引用且当时可见的新闻，中文展示、英文折叠；按行情/持仓决策时不硬配新闻。
- **独立费用账本**：不受聊天每日额度影响，博客默认共享每日 5 美元应用侧保护，可自行下调。

没有捆绑原作者的账号、服务器、记忆、文章、表情素材、Alpaca 密钥或模型密钥。默认关闭，不能安装后直接运行。

## 目录

| 目录 | 用途 |
| --- | --- |
| 根目录 | 可直接安装的 AstrBot 记忆博客插件 |
| `optional/astrbot_plugin_neuro_stocks/` | 可选股票插件，需要单独复制安装 |
| `services/budget_gateway/` | 配套独立预算服务 |
| `examples/` | 无真实凭据的配置示例 |
| `deploy/` | Caddy 静态发布示例 |
| `scripts/stocks_control.py` | 本地管理员开关，不把控制 token 暴露给网页 |
| `tests/` | 虚构记忆、行情和模型的离线测试 |

## 安装记忆博客

建议 Python 3.11+。需要 AstrBot 提供 `StarTools.get_data_dir`、`persona_manager.get_persona`、插件注册与配置接口；模板没有声明未经验证的最低 AstrBot 版本。记忆源当前适配 Iris SQLite 的 `memories` 表，字段为 `memory_id, content, metadata, group_id, timestamp, persona_id`。其他记忆系统需适配 `core.read_memories`，不要直接假设数据库结构相同。

1. 在 AstrBot 的 `data/plugins` 中克隆，**目录名使用 `astrbot_plugin_neuro_blog`**：

   ```sh
   git clone https://github.com/Sonnety14/qqbot-plugin-neuro_blog.git astrbot_plugin_neuro_blog
   pip install -r astrbot_plugin_neuro_blog/requirements.txt
   ```

2. 配好下文独立预算服务。将 `examples/blog-private.example.json` 复制为 AstrBot **插件数据目录** `data/plugin_data/astrbot_plugin_neuro_blog/private.json`，填写内部网关地址、该路由 token 和已核验模型名称。不要放入插件 Git 目录或公开目录。
3. 在 AstrBot 创建/选择你的人设，在插件配置填写：
   - `persona_id`：该人设 ID。
   - `memory_db`：Iris `metadata.db` 绝对路径，只读。
   - `memory_persona`：该库内的记忆命名空间。
   - `sticker_root`：自备图库根目录。
   - `sticker_index`：自备索引 JSON 绝对路径，格式见 `examples/stickers.example.json`。
4. 重新加载插件，配置完成后将 `enabled` 设为 `true`。股票插件可复用资源配置，记忆周记仍可保持关闭。

图库仅读取 `enabled: true` 的有效文件。`happy1.png`、`happy2.png` 会归为 `happy` 标签；纯数字文件按父目录标签分组。路径必须留在图库目录内。支持 PNG/JPEG/WebP/GIF，不提供未获授权的原作者表情素材。

首次启用会建立已有记忆基线，**不回填旧记忆**。每分钟观察只更新本地记录，不调用模型。周日集中按最多 20 条一批评价，冻结记录和模型原始回复后发布。发生结果未知/格式错误时停止自动重复付费，保留状态供核查；发布文件失败可复用已保存结果。

默认处理该命名空间里的群聊和私聊记忆，并做敏感词、号码、链接及模型公开性筛选；这些检查不能保证所有隐私都被发现。请只接入获得公开许可的记忆命名空间。公开页面不显示 QQ/群号；昵称来自元数据，缺失则不显示。

## 独立预算服务

详见 [配置与费用说明](services/budget_gateway/README.md)。这不是普通 OpenAI 接口：插件同时需要鉴权的 `GET /路由/status` 与 `POST /路由/chat/completions`，状态必须声明 `budget_scope: blog`。不能直接把供应商 URL 填进插件。

随机 token 可在本机运行 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成；记忆、股票和管理各用不同的值。

复制 `examples/gateway-private.example.json` 到仓库之外的私有目录，配置自备 Gemini API key、两条随机路由 token、模型及审核后的价格。记忆和股票分别使用 `blog_memory`、`blog_stocks`。它们共享独立博客账本，与聊天数据库分开；模板只支持经过计费适配的 Gemini 请求。

```sh
pip install -r services/budget_gateway/requirements.txt
cd services
export NEURO_BUDGET_CONFIG=/private/neuro-blog/gateway.json
python -m uvicorn budget_gateway.app:load_app --factory --host 127.0.0.1 --port 8089 --no-access-log
```

Windows PowerShell 使用 `$env:NEURO_BUDGET_CONFIG='C:/private/neuro-blog/gateway.json'`。示例目录应由你建立并限制访问权限；容器部署时使用专用内部网络，`127.0.0.1` 指向当前容器，不一定是宿主机。

## 可选炒股游戏

按照 [股票模块说明](optional/astrbot_plugin_neuro_stocks/README.md) 安装。只需历史数据权限，**不连接券商交易端点、不用真实资金**。默认没有赛季且不开启，管理员必须显式 `start` 再 `resume`。行情/新闻只取模拟时点可见部分；新闻内容不能作为工具指令。

## 静态网页部署

插件输出在 `data/plugin_data/astrbot_plugin_neuro_blog/public/`。**仅公开这个目录**，不要公开状态数据库、原始记忆、模型响应或整个 plugin_data。

设置 `NEURO_BLOG_PUBLIC_DIR` 为该目录绝对路径，`NEURO_BLOG_EDGE_NETWORK` 为你的已有代理网络，然后运行：

```sh
docker compose -f deploy/compose.yml up -d
```

在主站反向代理把 `/neuro-blog` 和 `/neuro-blog/*` 转发到内部 `neuro-blog:8080`；现有 Caddy 示例会去除 `/neuro-blog` 前缀。没有主代理时也可自行用静态服务器发布，并适配固定 `/neuro-blog/` 路径。

主页不需要登录、没有评论，并设置 `noindex`。不主动加入网站导航。**知道地址的人仍可访问；隐藏入口和 noindex 不是访问控制。** 模板仅借鉴周记组织形式，不包含官方博客文章或代码。

## 验证与维护

```sh
pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试不使用真实模型和账户。新环境仍需自行验证 AstrBot 接口、Iris 表结构、模型定价与调用权限、Alpaca 数据权限、时区及反向代理。首次生成要等启用后的新记忆与下一次截止时间；首次股票周批次要等开启后的周三 21:00。

不提供网页管理员控制台，因为各网站认证体系不同；提供本地控制客户端与鉴权 HTTP 接口。若自行接入控制台，凭据必须留在后端，并保留管理员认证及来源/CSRF 校验。

许可证：MIT，仅覆盖本仓库原创代码。第三方依赖、图库素材及行情/新闻数据各自遵循其许可；数据访问权限不自动等于公开再分发权限。

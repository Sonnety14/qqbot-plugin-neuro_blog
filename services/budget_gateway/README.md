# 独立博客预算网关

将仓库 `examples/gateway-private.example.json` 复制到仓库外配置。本模板仅支持已实现的 Gemini 原生 `countTokens` / `generateContent` 计费流程，不自动认定其他 OpenAI 兼容上游免费。

`pricing` 必须填写：

- `model`：你已核验支持该请求格式、JSON 输出与 `low` 思考档位的模型。
- `version`：此次价格审核标识。
- `valid_from` / `valid_until`：ISO 日期，左闭右开。过期会停止调用，需要重新核验价格；已锁定的账日不会因改配置自动退款/解锁。
- `input_nusd_per_token`、`output_nusd_per_token`、`cached_nusd_per_token`：每 token 的整数纳美元，1 美元 = 10^9 纳美元。每百万 token 美元单价乘 1000 即对应数值；这是换算公式，不是供应商报价。

示例中保留字符串占位符，未填应启动失败。核验依据使用供应商的[价格页](https://ai.google.dev/gemini-api/docs/pricing)及[生成文档](https://ai.google.dev/api/generate-content)，不能沿用别人的旧价。每进程只能运行一个模型/价格配置实例。

`database` 与 `blog_database` 必须是不同 SQLite 路径。所有博客路由指定 `budget_scope: blog`，客户端无权切换账本。默认 `blog_daily_limit_nusd: 5000000000`（5 美元），可下调；股票客户端会拒绝高于 5 美元的路由上限。模板不配置聊天路由。

在仓库的 `services` 目录启动 `python -m uvicorn budget_gateway.app:load_app --factory --host 127.0.0.1 --port 8089 --no-access-log`；环境变量 `NEURO_BUDGET_CONFIG` 指向私有配置。每个 route 使用不同随机 token，上游 API key 只存在服务端配置。

SQLite 事务预留避免并发重复使用余额，按真实 usage 结算缓存、输入、输出/思考费用；不自动重试。结果未知保留预留，可能暂停服务，需核对供应商记录，不能直接删库、清零或假定请求免费。

这是应用侧费用保护，不是供应商账单绝不会超额的保证；价格变化、额外收费或上游统计差异仍需核查。账户消费监控由部署者维护。

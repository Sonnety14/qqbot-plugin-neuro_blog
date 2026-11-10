# 可选历史炒股游戏

将本目录复制到 AstrBot `data/plugins/astrbot_plugin_neuro_stocks`，同时安装主博客插件；分别安装 `requirements.txt`。根目录插件安装不会自动启用此模块。

## 数据与配置

1. 自行申请 [Alpaca 数据凭据](https://app.alpaca.markets/)。导入使用 `data.alpaca.markets` 的历史股票分钟线与新闻，不使用券商下单接口。根据账号实际权限选择 `iex` 或 `sip`，权限不足会失败，不偷偷换数据源。
2. 在私有 shell 环境设置 `APCA_API_KEY_ID`、`APCA_API_SECRET_KEY`，不要把值写入脚本或 Git。按行情包覆盖的股票与日期核查拆股、分红，建立 `actions.json`：

   ```json
   {
     "review": {"complete": true, "through": "2020-02-28T21:00:00+00:00"},
     "actions": []
   }
   ```

   日期仅为格式示例。只有确认没有适用公司行动时才能用空数组；不能因为接口暂时返回空就视为已核验。拆股格式：`id,type=split,symbol,effective_at,numerator,denominator`；分红格式：`id,type=dividend,symbol,effective_at,pay_at,cash`。不支持的公司行动必须补适配或换赛季，不能跳过。
3. 在安装后的 `data/plugins` 目录运行：

   ```sh
   python -m astrbot_plugin_neuro_stocks.import_history --feed iex --actions /private/neuro-blog/actions.json --output /private/neuro-blog/history.json
   ```

   默认导入约一个月前开始的 20 个交易日，也可用 `--start YYYY-MM-DD` 指定已过去的历史日期。完整权限、公司行动和数据校验未通过时不推进。
4. 将仓库 `examples/stocks-private.example.json` 复制至插件数据目录 `data/plugin_data/astrbot_plugin_neuro_stocks/private.json`，填写历史包路径、`blog_stocks` 路由 token、模型、人设 ID（留空复用主博客配置）与随机控制 token。重载插件。

## 本地管理

控制端默认仅监听回环 `127.0.0.1:18791`。在能够连接该端点的机器运行仓库根目录脚本：

```sh
python scripts/stocks_control.py --config /private/neuro-blog/stocks.json status
python scripts/stocks_control.py --config /private/neuro-blog/stocks.json start
python scripts/stocks_control.py --config /private/neuro-blog/stocks.json resume
python scripts/stocks_control.py --config /private/neuro-blog/stocks.json pause
```

`--config` 指向你实际的 stocks 私有配置文件。远程管理使用 SSH 隧道或受限内部网络，不把端口开放到公网。HTTP 是 `GET /status` 和 `POST /control`，后者 JSON `{"action":"start|resume|pause"}`，都需 Bearer 控制 token（至少 32 字符）。不要把 token 放到浏览器。

## 规则和恢复

- 十万美元模拟本金，仅整数股、现金买卖，不做空、不融资。单只股票仓位上限 25%，每天最多 4 笔成交。
- 每周三北京时间 21:00 按历史半小时格连续运行五个交易日；常规五日共 65 次决策与一次周记，提前收盘按交易所日历处理。
- 决策只能看到已完成行情及当时新闻；下一分钟模拟撮合，加买卖滑点和手续费。使用 XNYS 日历与已核验公司行动，不用自然日代替交易日。
- 摘要加近期原文、平均成本和成交记录保留跨日情景，不依赖聊天缓存。付费请求中断不会盲目重发，已完成游标、成交和文章持久化去重。
- 暂停阻止后续请求；已发出的调用可能仍计费。恢复继续当前批次。赛季结束且最后一期周记完成后才能新建，不覆盖历史账本。
- 新闻必须明确引用可见 ID；仅展示匹配的中文资讯。没有引用则显示行情/持仓依据。每条札记固定表情，观望记录折叠。
- 静态输出默认位于主博客 `public/stocks/`，同级汇入 `/neuro-blog/` 首页；只发布图表、汇总文章和素材副本。

数据包固定，不自动滚动获取下一季。禁止运行中替换历史包以绕过哈希校验。原始新闻及行情不随公开模板提供。

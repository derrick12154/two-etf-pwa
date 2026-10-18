# 转债月调仓 PWA

安装地址：https://derrick12154.github.io/two-etf-pwa/convertible-bond/

iPhone：用 Safari 打开安装地址，点“分享”→“添加到主屏幕”→“添加”。若系统显示“作为 Web App 打开”，保持开启。首次联网载入后可以离线查看保存的快照，离线数据会明确标识。

## 自动更新

`.github/workflows/convertible-bond.yml` 在北京时间工作日 23:35 拉取收盘数据，并在次日 07:35 补跑。GitHub 定时任务可能延迟；中国市场节假日沿用最近交易日。Actions 的 `workflow_dispatch` 支持手动重跑。

自动更新范围是候选价格、正股价格、合同现金流估算 YTM、偏离排名，以及全市场估值散点和拟合曲线。调仓频率仍为每月首个交易日，候选清单不是实盘持仓清单。历史回测和三个对照序列是已经生成的研究快照，区间固定标为 2019–2026-10-16，不会把最新行情日期冒充回测终点。

取数使用东方财富公开网站的转债条款表 `RPT_BOND_CB_LIST` 和历史估值表 `RPTA_WEB_KZZ_LS`。不需要用户密钥。取数校验包括：日期不倒退、距离当前不超过 14 天、有效在市报价覆盖不低于 98%、报价与估值日期一致、价格与溢价的恒等关系、全部模型预测为有限值。校验失败将中止 Action，继续保留上一次部署和已提交的数据。

模型 v2 与原研究保持一致：以转股价值的二阶样条，加纯债价值、剩余年限和发行规模的对数为解释变量，Ridge alpha=20；按债券代码 CRC32 划分五折，每券使用未包含自身的训练样本预测，训练响应在 2%/98% 分位截尾。拟合图将逐券预测按转股价值 20 元分箱取中位数。

资金规则按 19.86% 那组原回测表述：最多十只，新买标的均分可用现金，已有持仓保持数量；只有偏离更低且价格更低的新券才替换持仓，强赎公告按月度规则处理。它不是每月把每只券重新配到 10%。

## 维护

```bash
python -m pip install -r convertible-bond/requirements.txt
python convertible-bond/scripts/update_market.py
python convertible-bond/scripts/build_site.py --site _site
```

Pages 构建只复制静态文件白名单，脚本和原始数据响应不进入网站产物。原仓库的周度 PWA 和这个子目录 PWA 分别使用独立的应用标识与缓存前缀；两个发布工作流共用 `pages` 并发组。

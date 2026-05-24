# A 股 Kronos 预测 MVP

这个仓库把 shiyu-coder/Kronos 落成了一个可本地测试、可部署到美国 VPS、可由 Cloudflare Pages 提供静态前端的 A 股预测研究工具。

## 当前能力

- 支持 A 股单只股票预测，也支持按全市场范围同步日线数据，内置 AkShare、BaoStock、TuShare 多源 fallback。
- 北交所日线同步会优先走 AkShare 的新浪日线源，避免把 TuShare `daily` 权限当成默认前提。
- 使用 SQLite 缓存历史 K 线，避免每次预测都实时抓网。
- 通过 Kronos 官方接口适配多路径预测输出。
- 未来预测日期使用上交所交易日历，不再只按工作日推算。
- 前端静态页支持运行时 API Base 配置，可直接给 Cloudflare Pages 使用。
- 网页默认针对未来 7 个交易日做概率分析，直接展示看涨/看跌判断、上涨概率、波动风险、终点区间和代表情景，而不是把所有路径原样铺开。
- 页面支持访问密码保护；输入正确密码后，先进入股票输入首页，再进入单股票详情页。
- 后端支持可配置 CORS，适合“CF 静态页 + 独立 API 域名 + 美国 VPS”部署。
- GitHub Actions 可在北京时间收盘后自动同步全市场 A 股，并把 SQLite 缓存上传到 VPS。
- 推送到 GitHub main 后可自动把最新代码部署到 VPS，不覆盖 `data`、`.env` 和 `.venv`。

## 架构

- 前端：Cloudflare Pages 发布 [kronos_mvp/static](kronos_mvp/static)
- 后端：FastAPI API，运行在美国 VPS
- 预测：Kronos 小模型，默认 NeoQuasar/Kronos-small
- 数据：SQLite 本地缓存 + 每日同步
- 调度：GitHub Actions 每日执行同步任务

## 本地 Windows 快速开始

1. 创建虚拟环境。

```powershell
c:/python314/python.exe -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. 安装项目依赖。

```powershell
pip install -r requirements.txt
git clone https://github.com/shiyu-coder/Kronos.git vendor/Kronos
```

3. 准备环境变量。

```powershell
Copy-Item .env.example .env
```

本项目会自动读取工作目录下的 .env。最少需要确认下面几项：

- KLINE_DB_PATH
- PYTHONPATH
- KRONOS_MODEL
- KRONOS_TOKENIZER
- KRONOS_DEVICE
- DATA_PROVIDERS

其中 `PYTHONPATH` 应指向 Kronos 源码目录。默认建议使用 `vendor/Kronos`。

如果你希望本地页面也受访问密码保护，可额外设置：

- APP_ACCESS_PASSWORD

4. 先同步一只股票的历史数据。

```powershell
python -m kronos_mvp.cli sync 600519
```

如果要在本地先跑一遍全市场同步，可以使用：

```powershell
python -m kronos_mvp.cli sync --all
```

第一次全市场同步会比较久；后续同样再跑 `sync --all` 时，会按本地 SQLite 中每只股票的最新日期增量抓取，不再把每只股票整段历史重复写入数据库。

全市场同步现在还会默认写入进度文件，长任务如果中断，再次执行同样的 `sync --all` 会自动从未完成队列继续；单只股票临时失败会按 `--max-retries` 做重试。需要从头重建队列时，可以显式加上 `--reset-progress`。

如果要手动只跑某个交易所分片，可以使用：

```powershell
python -m kronos_mvp.cli sync --all --market sh
python -m kronos_mvp.cli sync --all --market sz
python -m kronos_mvp.cli sync --all --market bj
```

5. 启动本地 API。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_local_api.ps1
```

6. 打开 http://127.0.0.1:8000；如果配置了 `APP_ACCESS_PASSWORD`，先输入访问密码，再输入股票代码并点击“开始预测”。

当前页面默认使用 12 条采样路径，对未来 7 个交易日做概率分析，不再在网页上手动修改路径数。首页只负责输入股票代码；点击“开始预测”后才进入详情页。详情页顶部提供“返回首页”，并直接展示上涨概率、波动放大概率、终点区间和代表情景。网页预测默认直接读取本地 SQLite 缓存，避免把页面请求绑在实时抓数上；生产数据刷新依赖每日 GitHub Actions，同步失败时不会拖垮前端预测请求。

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

注意：默认的 python -m unittest -v 在当前仓库不会自动发现 tests 目录里的测试，使用 discover 命令。

### Git 上传规则

- 要上传到 GitHub：源码、[.github/workflows/update-a-share-data.yml](.github/workflows/update-a-share-data.yml)、README、部署模板。
- 不要上传到 GitHub：.venv、.env、data 目录、本地 SQLite、[.github/deploy-memory.md](.github/deploy-memory.md)、任何私钥、Token、密码或服务器私密文件。
- 不要忽略整个 .github 目录；如果 .github 不上传，GitHub Actions 就不会生效。

### Secrets 怎么填

- VPS_HOST：你的服务器地址。
- VPS_USER：SSH 用户。你现在填 root 就对。
- VPS_PASSWORD：如果服务器是 root + 密码登录，就把登录密码放这里，放在 GitHub Secrets，不进仓库。
- VPS_SSH_KEY：如果服务器是密钥登录，才填写这里；内容是私钥原文，放在 GitHub Secrets，不进仓库。
- VPS_DATA_DIR：Actions 上传 data/candles.db 的目标目录。这个值要和 VPS 上实际部署目录一致。

如果你的项目在 VPS 上部署目录就是 /home/yupiaoyuce，并且应用读取的数据库路径是 data/candles.db，那么填 /home/yupiaoyuce 是对的。因为工作流上传的是 data/candles.db，最终会落成 /home/yupiaoyuce/data/candles.db。

如果你后端实际部署在别的目录，那么 VPS_DATA_DIR 就应该填那个真实部署目录，而不是 /home/yupiaoyuce。

- TUSHARE_TOKEN：TuShare Pro 的访问令牌，去 tushare.pro 注册后获得。它是可选的，不是必须。

当前工作流的 provider 顺序是 akshare, baostock, tushare。也就是说：

- 如果你没填 TUSHARE_TOKEN，AkShare 和 BaoStock 还能先跑；只有轮到 TuShare 时才会因为没 token 跳过或报错。
- 如果你想提高数据源 fallback 的稳定性，建议补上 TUSHARE_TOKEN。
- 如果你暂时不用 TuShare，也可以先不填，后续再补。

## 已知边界

- 这是研究型预测工具，不是交易建议系统。
- 当前 MVP 只做单股票日线预测，没有做分钟级实时推理。
- 预测结果只基于 K 线序列，不包含公告、财报、舆情和资金面。
- 如需微调，请参考 Kronos 官方仓库里的 Qlib 示例，当前仓库默认使用预训练模型直接推理。
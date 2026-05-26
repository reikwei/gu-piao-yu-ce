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
- 详情页支持按按钮加载独立资金面分析，展示资金净额、资金净流入占比、融资余额、融资买入额，并给出综合结论。
- 页面支持访问密码保护；输入正确密码后，先进入股票输入首页，再进入单股票详情页。
- 后端支持可配置 CORS，适合“CF 静态页 + 独立 API 域名 + 美国 VPS”部署。
- GitHub Actions 可在北京时间收盘后自动同步全市场 A 股 K 线，并把 `data/candles.db` 上传到 VPS。
- GitHub Actions 还可独立同步相对强弱缓存，并把 `data/relative_strength.db` 上传到 VPS。
- GitHub Actions 还可独立在北京时间每周日 21:09 运行一次行业修复任务，专门补 `data/relative_strength.db` 里的行业映射和行业 K 线缓存。
- GitHub Actions 还可独立在北京时间 18:09 同步资金面快照，写入独立 SQLite 缓存。
- 相对强弱独立工作流当前默认在北京时间工作日 18:20 触发；若当天不是 A 股交易日，会在安装依赖后直接跳过同步和上传。
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
- RELATIVE_DB_PATH
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

如果你刚给 K 线缓存新增了字段，例如要把历史换手率整段回填进 `data/candles.db`，可以手动执行一次全量重抓：

```powershell
python -m kronos_mvp.cli sync --all --full-refresh
```

也可以只对单只股票执行：

```powershell
python -m kronos_mvp.cli sync 600835 --full-refresh
```

全市场同步现在还会默认写入进度文件，长任务如果中断，再次执行同样的 `sync --all` 会自动从未完成队列继续；单只股票临时失败会按 `--max-retries` 做重试。需要从头重建队列时，可以显式加上 `--reset-progress`。

GitHub Actions 的 `Update A-share K-line Data` 工作流现在也支持手动 `workflow_dispatch` 时把 `full_refresh` 设为 `true`，用于全量回填 K 线字段。注意这只能修复 `candles.db` 里的历史字段缺口；相对强弱行业映射和行业 K 线仍由独立的 `update-relative-strength-data.yml` 维护，不会随着 K 线工作流自动补齐。`full_refresh` 现在会在写库前校验 provider 返回历史是否覆盖现有缓存的最早日期；如果返回的是更晚起始的部分历史，同步会直接报错并保留原缓存，避免把不完整结果误当成成功的全量刷新。

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

API 侧现在会在进程内复用同一组 Kronos 模型实例，避免每次预测都重新加载模型；同时 `/api/predict/{symbol}` 默认启用账户级频率限制，环境变量 `PREDICT_RATE_LIMIT_REQUESTS` 与 `PREDICT_RATE_LIMIT_WINDOW_SECONDS` 可调整每个账户在窗口期内的预测次数上限。账号登录入口统一为 `/api/auth/login`；旧的 `/auth/login` 已停用，管理员也应使用同一入口登录，只是默认用户名仍是 `admin`。如果部署在 Nginx 反代后，建议把 `KRONOS_PREWARM_ON_STARTUP=1` 打开，让服务在启动阶段完成模型冷加载；反代层也应把 `proxy_read_timeout`/`proxy_send_timeout` 调到至少 180 秒，避免第一次预测或长路径预测在 60 秒默认超时下被提前截断。

如果你希望本地先同步最近半个月交易日的资金面数据，并在后续继续按缺口增量补齐，可以执行：

```powershell
python -m kronos_mvp.cli --fund-db data/fund_factors.db sync-funds --history-days 15
```

`sync-funds` 现在默认维护最近 15 个 A 股交易日的资金面窗口：

- 数据库里缺失的交易日会自动回补。
- 最新交易日会在每次运行时重新刷新。
- GitHub Actions 工作流也会按同样的增量策略执行，避免资金库永远只停在单日快照。

如果你希望把“相对强弱”所需的指数、行业映射和行业 K 线也同步到本地缓存，可以执行：

```powershell
python -m kronos_mvp.cli --relative-db data/relative_strength.db sync-relative --history-days 30
```

如果只想为某几只股票补齐相对强弱依赖，也可以直接带股票代码：

```powershell
python -m kronos_mvp.cli --relative-db data/relative_strength.db sync-relative 600835 600519 --history-days 30
```

如果你怀疑行业映射缓存本身有缺口，想强制重新抓一遍东财行业映射，可以附带：

```powershell
python -m kronos_mvp.cli --relative-db data/relative_strength.db sync-relative --history-days 30 --refresh-mappings
```

`sync-relative` 会维护 `data/relative_strength.db`，其中包含：

- 股票到东财行业板块的映射。
- 用于市场基准比较的指数日线缓存。
- 用于行业相对强弱比较的行业板块日线缓存。

日常的 `Update Relative Strength Data` 会在工作日 18:20 跑增量同步，并在非交易日自动跳过。若要单独修复行业映射与行业 K 线，可以使用独立的 `Repair Relative Industry Data` workflow：它会在北京时间每周日 21:09 运行一次，且保留手动 `workflow_dispatch` 入口，不受交易日守卫影响。该 repair workflow 现在会强制刷新行业映射，不再单纯复用 5 天内的映射缓存；但未指定 `symbols` 的空参运行仍属于 best-effort，若个别行业 K 线上游瞬时失败，workflow 可能保持成功并在 summary 记录 warning。若要对指定股票做严格修复校验，请手动传入 `symbols`。

全市场模式下，行业映射会优先复用现有缓存；只有映射缺失或超过 5 天未刷新时才会重新全量抓取。指数与行业 K 线仍按现有数据库中的最新日期做增量补齐。

如果是手动指定股票运行 `sync-relative 600835 300779 --history-days 30`，即便全量行业映射接口临时断连，也会额外尝试用东财单票信息补录这些股票的行业映射，再继续拉取对应行业 K 线，减少 workflow 绿了但指定股票行业侧仍为空的情况。

默认回补窗口现在是 30 天。因为相对强弱分析当前只使用 5 日和 20 日窗口，30 天缓存已经足够支撑日常结论，同时能降低空库或新库初始化时的同步耗时。

详情页顶部的“资金面分析”按钮会读取 `data/fund_factors.db`，展示最新交易日数据和多日趋势指标，包括：资金净额、资金净流入占比、3 日/5 日/10 日累计主力净流入、连续净流入天数、融资余额 3 日斜率与加速度，以及“单日异动 / 持续趋势”的资金结论区分。若最新交易日缺少融资明细，融资 3 日趋势会自动回退到最近 3 个可用融资样本，并在文案中明确标注。综合结论区会叠加 `data/relative_strength.db` 中的“相对强弱层”，用个股相对指数和所属行业的超额表现参与冲突裁决；若行业侧样本尚未补齐，会明确提示当前仅完成大盘基准比较。价量确认层除了成交量，还会结合成交额归一化和换手率判断放量是否真实成立。

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

注意：默认的 python -m unittest -v 在当前仓库不会自动发现 tests 目录里的测试，使用 discover 命令。

### Git 上传规则

- 要上传到 GitHub：源码、[.github/workflows/update-a-share-data.yml](.github/workflows/update-a-share-data.yml)、[.github/workflows/update-relative-strength-data.yml](.github/workflows/update-relative-strength-data.yml)、README、部署模板。
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
- 资金面分析目前依赖独立的每日同步快照，不会直接替代 Kronos 主模型；综合结论只把资金面作为辅助修正层。
- 如需微调，请参考 Kronos 官方仓库里的 Qlib 示例，当前仓库默认使用预训练模型直接推理。
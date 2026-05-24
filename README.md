# A 股 Kronos 预测 MVP

这个仓库把 shiyu-coder/Kronos 落成了一个可本地测试、可部署到美国 VPS、可由 Cloudflare Pages 提供静态前端的 A 股预测研究工具。

## 当前能力

- 支持 A 股单只股票预测，也支持按全市场范围同步日线数据，内置 AkShare、BaoStock、TuShare 多源 fallback。
- 北交所日线同步会优先走 AkShare 的新浪日线源，避免把 TuShare `daily` 权限当成默认前提。
- 使用 SQLite 缓存历史 K 线，避免每次预测都实时抓网。
- 通过 Kronos 官方接口适配多路径预测输出。
- 未来预测日期使用上交所交易日历，不再只按工作日推算。
- 前端静态页支持运行时 API Base 配置，可直接给 Cloudflare Pages 使用。
- 网页默认展示 3 条预测路径，点击“预测”会先自动同步最新数据，再发起推理。
- 后端支持可配置 CORS，适合“CF 静态页 + 独立 API 域名 + 美国 VPS”部署。
- GitHub Actions 可在北京时间收盘后自动同步全市场 A 股，并把 SQLite 缓存上传到 VPS。

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

6. 打开 http://127.0.0.1:8000，直接点“预测”即可；如需单独补拉缓存，再点“仅同步缓存”。

当前页面默认把“路径数”固定为 3，不再在网页上手动修改。点击“预测”时会优先尝试同步最新股票数据；如果同步失败但本地缓存仍可用，页面会继续使用缓存完成预测。

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

注意：默认的 python -m unittest -v 在当前仓库不会自动发现 tests 目录里的测试，使用 discover 命令。

## Cloudflare Pages + 美国 VPS 部署

### 1. 部署静态前端

- Cloudflare Pages 的发布目录指向 [kronos_mvp/static](kronos_mvp/static)
- 把 [kronos_mvp/static/config.js](kronos_mvp/static/config.js) 里的 apiBaseUrl 改成你的 API 域名，例如 https://api.example.com
- 这个静态前端不承载模型推理，只负责调用后端 API 和渲染图表

### 2. 部署 API 到 VPS

- 用 Python 3.10+ 创建虚拟环境
- 安装 requirements.txt，并把 Kronos 源码仓库 clone 到 `vendor/Kronos`
- 当前生产目录是 `/home/yupiaoyuce`
- 用 [.env.example](.env.example) 生成生产 .env，并至少设置：

  PYTHONPATH=vendor/Kronos
  APP_ALLOW_ORIGINS=https://你的-pages-域名
  APP_SITE_TITLE=土豆A股预测研究
  KRONOS_DEVICE=cpu

- systemd 模板见 [deploy/systemd/kronos-mvp.service](deploy/systemd/kronos-mvp.service)
- Nginx 反代模板见 [deploy/nginx/kronos-api.conf](deploy/nginx/kronos-api.conf)

当前生产部署已经验证通过的方式是：

- `tghao.cc.cd` 同域承载前端和 API
- Nginx 反代到 `127.0.0.1:8000`
- FastAPI 同时提供 `/` 页面、`/health` 和 `/api/*`
- Let's Encrypt 证书部署在源站，Cloudflare 橙云继续保留

如果从当前这台 Windows 机器 SSH 到生产 VPS，需要额外走本地临时代理 `127.0.0.1:7895`。

### 3. Cloudflare 入口建议

- 前端域名走 Cloudflare Pages
- API 域名走 Cloudflare 代理回美国 VPS
- Pages 域名必须加入 APP_ALLOW_ORIGINS，避免浏览器跨域失败

## GitHub Actions 每日同步

工作流文件是 [.github/workflows/update-a-share-data.yml](.github/workflows/update-a-share-data.yml)。

- 默认在 UTC 08:30 执行，对应北京时间 16:30
- 默认按全市场 A 股执行同步；如果手动触发时填写 symbols，则只同步传入的股票列表。
- 只安装同步任务所需依赖，不安装完整推理依赖
- 第一次跑全市场会最慢；后续每日任务会根据本地 SQLite 已有的最新日期，对每只股票做增量同步。
- 全市场任务会拆成沪市、深市、北交所三个并行 job，各自维护独立 SQLite 分片和进度文件，最后再合并成单个 `candles.db` 上传到 VPS。
- 每个分片 job 都会把 SQLite 分片和进度文件保存到 Actions cache；如果某次运行中断或部分失败，下次同分片任务会继续从剩余队列恢复，而不是从头再扫一遍。
- 工作流启用了并发互斥和更长超时，避免上一次全市场任务还没结束时下一次调度重叠。
- 网页上的“仅同步缓存”按钮主要用于手动补拉某只股票或临时刷新缓存。
- 可选 Secrets：

  VPS_HOST
  VPS_USER
  VPS_PASSWORD
  VPS_SSH_KEY
  VPS_DATA_DIR
  TUSHARE_TOKEN

如果配置了 VPS 相关 Secrets，工作流会把 data/candles.db 上传到 VPS 数据目录。认证方式支持两种：

- 密码登录：填写 VPS_HOST、VPS_USER、VPS_PASSWORD、VPS_DATA_DIR。
- 密钥登录：填写 VPS_HOST、VPS_USER、VPS_SSH_KEY、VPS_DATA_DIR。

兼容旧配置：如果你以前误把 VPS 登录密码填进了 VPS_SSH_KEY，而不是私钥内容，当前 workflow 也会按密码处理；但后续还是建议把它迁到 VPS_PASSWORD，避免名字继续误导。

### Git 上传规则

- 要上传到 GitHub：源码、[.github/workflows/update-a-share-data.yml](.github/workflows/update-a-share-data.yml)、README、部署模板。
- 不要上传到 GitHub：.venv、.env、data 目录、本地 SQLite、[.github/deploy-memory.md](.github/deploy-memory.md)、任何私钥、Token、密码或服务器私密文件。
- 不要忽略整个 .github 目录；如果 .github 不上传，GitHub Actions 就不会生效。

### Secrets 怎么填

- VPS_HOST：你的服务器地址。当前就是 172.245.147.13。
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
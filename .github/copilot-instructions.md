# 项目宪法

## 1. 项目目标

- 本仓库是一个基于 Kronos 的 A 股预测研究工具，不是自动交易系统。
- 目标部署形态是：Cloudflare Pages 静态前端 + 美国 VPS FastAPI API + GitHub Actions 每日数据同步 + SQLite 本地缓存。
- 用户输入股票代码后，应能看到历史 K 线与未来多路径预测结果。

## 2. 产品边界

- MVP 只支持 A 股单只股票日线预测。
- MVP 支持历史数据同步、SQLite 缓存、Kronos 多路径预测、静态网页展示。
- MVP 不做分钟级实时推理、不做自动下单、不承诺收益、不把结果表述为投资建议。
- 预测页面默认面向研究和可视化，不做复杂账户体系或后台 CMS。

## 3. 技术硬约束

- 预测后端必须对接 Kronos 官方接口：from model import Kronos, KronosTokenizer, KronosPredictor。
- 默认模型使用 NeoQuasar/Kronos-small，默认 tokenizer 使用 NeoQuasar/Kronos-Tokenizer-base。
- Kronos 上游仓库当前不能直接 `pip install git+https://github.com/shiyu-coder/Kronos.git`；部署时应 clone 源码目录并通过 PYTHONPATH 暴露 `model` 包。
- 输入数据至少包含 open、high、low、close；volume 和 amount 可选但建议提供。
- lookback 默认不超过 512，保持和 Kronos-small/base 的推荐上下文长度一致。
- 未来预测时间戳必须使用 A 股交易日历，不能只按周末过滤。
- 数据优先走本地缓存，不能把“每次请求都实时抓取外部数据”当成默认路径。

## 4. 数据同步原则

- 默认 provider 顺序是 AkShare、BaoStock、TuShare，多源失败后再报错。
- 美国 VPS 上不假设 AkShare 一定稳定可用，必须保留 fallback 和本地缓存。
- GitHub Actions 可以作为每日调度器，但不能被描述成“国内节点数据源”。
- 每日同步建议在北京时间收盘后执行，默认按 16:30 对齐。

## 5. 部署原则

- 前端静态资源目录是 [kronos_mvp/static](kronos_mvp/static)，应可直接发布到 Cloudflare Pages。
- 前端必须支持可配置 API Base URL，不能把 API 地址写死为只能同源访问。
- 后端必须支持可配置 CORS，以兼容 Cloudflare Pages 独立域名访问 API。
- 美国 VPS 负责模型推理和 API，不把 Cloudflare 当成模型计算节点。
- 生产环境必须显式处理时区，涉及定时和交易日判断时以 Asia/Shanghai 语义为准。

## 6. 开发要求

- 新改动必须优先修根因，不接受仅为演示而暴露 baseline 预测模式。
- 测试命令统一使用 python -m unittest discover -s tests -v。
- README、部署模板、环境变量样例必须和代码行为保持一致。
- 若引入新的运行前提，例如交易日历库、CORS 配置、Cloudflare API Base 配置，必须同步更新文档。
- Git 上传默认排除 .venv、.env、data 和其他私密文件，但不能排除整个 .github 目录；GitHub Actions 工作流必须进入仓库。
- 域名使用的是tghao.cc.cd 已经解析到cf的cdn，橙云。
- 生产vps是（ssh链接vps要加上临时http代理127.0.0.1 7895链接） 172.245.147.13 已经配置了秘钥，可以直接 ssh root@ 链接操作。生产环境的目录放在 /home/yupiaoyuce
- github仓库地址 https://github.com/reikwei/gu-piao-yu-ce.git
- 更新github仓库的命令，统一成一个bat脚本 push_git.bat 只需在项目目录运行./push_git.bat 就可以提交当前改动并推送到github远程仓库的main分支了
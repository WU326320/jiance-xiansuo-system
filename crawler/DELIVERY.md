# 采集模块交付说明（R1）

- 交付路径：`/workspace/ip-monitor/crawler`
- 配置版本：`config_version=2026-11-05.1`，`caliber_version=v1.0`
- 阶段：R1 采集跑通（淘宝/天猫、京东、1688 列表采集 + 登录态 + 运行记录 + 风控停机）
- 浏览器模式：`playwright` / `cdp` 三平台可配置；京东默认 `cdp`，复用系统真实
  Chrome（`connect_over_cdp`），解决京东对自动化 Chromium 的 418 / 登录页黑屏；
  Windows 10 本机用 `config/windows.yaml`，三平台默认 `cdp`，无需 DISPLAY/X server
- 测试：Linux **204 passed**；Windows 10 回归目标 **0 failed**（X11 专用用例合理
  跳过，本次新增用例均为平台无关的离线逻辑），源码覆盖率 **84%**（全部离线
  fixture，未访问真实电商网站；CDP 部分使用假 Chrome 进程/假 RemoteSession）
- 真实轻量冒烟：CDP 模式启动本机 Chrome 打开京东登录页，`title=京东-欢迎登录`，
  DOM 正常加载（374,693 字节），随后只关闭本次启动的 Chrome

## 1. 代码结构

```
crawler/
  cli.py                     # python -m crawler login/check-login/run/detail
  runner.py                  # 关键词矩阵、平台编排、JSONL/运行记录/图片下载
  login.py                   # 有界面人工登录（DISPLAY）、登录态确认
  config.py                  # YAML 校验、路径、profile 目录 0700
  models.py                  # Item / ItemPatch / RunRecord（pydantic）
  core/
    browser.py               # Scrapling StealthySession / RemoteSession(CDP) + StaticBrowser
    chrome.py                # 真实 Chrome 定位、精确启动参数、CDP 端口探测
    cookies.py               # 登录 Cookie 只读判定（淘宝/1688 CDP 严格校验）
    throttle.py              # 随机间隔、RPM、每日请求上限（持久化）
    retry.py                 # 网络/5xx/429 退避；RISK_CONTROL/登录不重试
    risk.py                  # 最终 URL/可见文案/可见 DOM 的登录页与滑块/验证码判定
    riskstate.py             # 同日风控熔断（state/risk/{platform}-{date}.json）
    parse.py                 # 1万+ 下限、价格区间、ID/URL、GBK、去重
    schema.py                # 最低字段校验、缺失 flags、详情 patch 合并
    jsonl.py                 # 统一 JSONL、run.json、run_logs.jsonl
    images.py                # 主图下载（平台 Referer，失败降级）
    jsonutil.py / dom.py     # JSON/HTML 容错解析
  platforms/
    base.py                  # 三平台统一 check_login/search/fetch_detail 流程
    taobao.py / jd.py / ali1688.py
    selectors.py             # 选择器集中处
  tests/                     # 204 个离线单测 + fixtures
config/rules.yaml            # 采集配置（Linux/默认）
config/windows.yaml          # Windows 10 profile：三平台真实 Chrome/CDP
samples/2026-11-05/          # 各平台 20 行样例 + run 记录 + run_logs
tools/make_samples.py        # 离线生成样例
```

### 统一接口

```python
check_login() -> bool
search(keyword, max_pages, *, ip_code=None) -> list[Item]
fetch_detail(product_ids) -> list[ItemPatch]
```

### 浏览器模式（三平台通用）

`config/rules.yaml`：

```yaml
browser:
  mode: playwright          # 全局默认：playwright | cdp
  platform_modes:
    jd: cdp                 # 京东默认 cdp；写 playwright 可退回
  port: 9222                # 实际端口 = 9222 + 平台序号
  display: ":6"
  chrome_executable: null   # null = which google-chrome/google-chrome-stable/chromium
  startup_timeout_seconds: 20
```

- `playwright`：原 Scrapling `StealthySession` + `profiles/{platform}`。
- `cdp`：定位系统 Chrome 后严格用
  `--user-data-dir=profiles/{platform}-chrome --remote-debugging-port=<port>`
  启动有界面浏览器（Linux 用 `DISPLAY=:6`，Windows/macOS 用原生窗口），不加
  `--headless` / `--enable-automation`
  等自动化参数；随后 `RemoteSession`（Playwright `connect_over_cdp`）连接现有
  默认 context，共享人工登录 Cookie/localStorage。
- 若端口已有 Chrome，直接复用；否则自动启动。程序退出只断开 CDP，不结束
  用户已有 Chrome，也不删除 profile。

## 2. 运行方法

```bash
cd /workspace/ip-monitor/crawler
source .venv/bin/activate

# 浏览器路径（venv 已默认；换 shell 时显式设置）
export PLAYWRIGHT_BROWSERS_PATH=/workspace/tools/ms-playwright

# 1) 人工登录（有界面；DISPLAY 默认 :6；京东默认走 cdp，其余默认 playwright）
python -m crawler login taobao        # 也支持 --platform taobao
python -m crawler login jd            # 启动/复用真实 Chrome，只导航一次登录页
python -m crawler login 1688

# 2) 登录态检查
python -m crawler check-login all    # 也支持 --platform all

# 3) 采集
python -m crawler run --platform all --date 2026-11-05
python -m crawler run --platform taobao --keyword 葫芦兄弟 --max-pages 1 --limit-keywords 1
python -m crawler run --platform jd --no-images

# 4) 详情补抓
python -m crawler detail --platform taobao --ids-file ids.txt --date 2026-11-05
```

Linux 无 X server 时先启动（Windows/macOS 跳过此步）：

```bash
Xvfb :6 -screen 0 1440x900x24 &
export DISPLAY=:6
```

输出位置：

```
out/{date}/{platform}.jsonl
out/{date}/{platform}.run.json
out/{date}/run_logs.jsonl
out/{date}/images/{platform}/
logs/{date}_{platform}.log
logs/screenshots/{date}_{platform}_{risk|login_risk|detail_risk}_*.png
state/daily/{platform}-{date}.json
profiles/{platform}/            # playwright 模式 0700，Cookie 只在这里
profiles/{platform}-chrome/     # cdp 模式 0700，真实 Chrome 的 Cookie 只在这里
```

## 3. 测试结果

命令：

```bash
.venv/bin/python -m pytest -q
# pytest.ini 已默认带 --cov=crawler --cov-report=term-missing
```

结果：`204 passed`（京东/1688 多策略解析 + debug 快照后的最近一次全量执行；离线，约 7 秒）。

覆盖率（`.coveragerc` 已排除 `crawler/tests/*`、`crawler/__main__.py`，下表只统计源码）：

| 模块 | Stmts | Miss | Cover |
|---|---:|---:|---:|
| crawler/__init__.py | 1 | 0 | 100% |
| crawler/cli.py | 110 | 8 | 93% |
| crawler/config.py | 315 | 41 | 87% |
| crawler/core/browser.py | 371 | 78 | 79% |
| crawler/core/chrome.py | 156 | 29 | 81% |
| crawler/core/cookies.py | 40 | 8 | 80% |
| crawler/core/dom.py | 116 | 18 | 84% |
| crawler/core/errors.py | 25 | 0 | 100% |
| crawler/core/images.py | 74 | 19 | 74% |
| crawler/core/jsonl.py | 99 | 8 | 92% |
| crawler/core/jsonutil.py | 219 | 29 | 87% |
| crawler/core/logging_utils.py | 29 | 4 | 86% |
| crawler/core/parse.py | 200 | 18 | 91% |
| crawler/core/retry.py | 52 | 5 | 90% |
| crawler/core/risk.py | 174 | 15 | 91% |
| crawler/core/riskstate.py | 29 | 1 | 97% |
| crawler/core/schema.py | 51 | 10 | 80% |
| crawler/core/stats.py | 29 | 0 | 100% |
| crawler/core/throttle.py | 112 | 6 | 95% |
| crawler/login.py | 195 | 35 | 82% |
| crawler/models.py | 115 | 8 | 93% |
| crawler/platforms/__init__.py | 16 | 0 | 100% |
| crawler/platforms/ali1688.py | 287 | 70 | 76% |
| crawler/platforms/base.py | 239 | 43 | 82% |
| crawler/platforms/helpers.py | 74 | 14 | 81% |
| crawler/platforms/jd.py | 278 | 60 | 78% |
| crawler/platforms/selectors.py | 6 | 0 | 100% |
| crawler/platforms/taobao.py | 136 | 5 | 96% |
| crawler/runner.py | 340 | 86 | 75% |
| **TOTAL** | **3888** | **618** | **84%** |

覆盖率未覆盖的主要是：真实 Scrapling 浏览器错误分支、键盘中断/浏览器异常分支、部分
CLI 参数错误分支。实际浏览器启动、headless/headful、持久化 profile、截图已用本地
静态 HTML 做过冒烟（未访问电商站点）：

```
status 200 ... has_hello True
headful ok 200 True
profile ... 0o700
screenshot ... .png
```

### 单测覆盖点

- `1万+`→10000、`2.5万+`→25000、`300+`→300、`10万+人付款`→100000、
  `已售1000+件`→1000、`100+条评价`→100，且保留原文；
- 价格区间取最低、面议→null；`null` 不填 0；
- 1688 GBK：`葫芦娃` → `%BA%F9%C2%AB%CD%DE`；
- 淘宝 JSON/DOM、京东 JSON 价格 + DOM、1688 JSON/DOM、详情页、下架；
- `(platform, product_id)` 单次/跨次 JSONL 去重；
- 配置默认值/环境变量/profile 0700/非法配置；
- 节流间隔、RPM、每日上限持久化；
- 重试退避、风控/登录不重试；
- 风控页/登录页/正常页判定，截图路径；
- baxia 防爬脚本、隐藏预加载验证层、商品标题里的“验证码”不误报；可见 baxia
  弹层/iframe、nc 滑块、JDJRV 滑块、punish/`_____tmd_____` 验证页 URL 判风控；
- runner 登录失败停平台、搜索中风控停平台、每日上限、图片失败降级、详情补抓；
- 同日风控熔断：命中后写 `state/risk/{platform}-{date}.json`，同日再次运行不发请求；
- CLI `login/check-login/run/detail` 分发和退出码；
- CDP：`browser.mode`/平台覆盖/端口推导、京东默认 cdp、Linux `which` 查找顺序、
  Windows Program Files/%LOCALAPPDATA%/Edge 兜底、Windows `creationflags` 独立启动、
  Windows/macOS 跳过 DISPLAY、固定启动参数与 Linux `DISPLAY=:6`、已有 Chrome 复用、
  启动超时/立即退出清理、
  假 `RemoteSession` 连接默认 context、登录只导航一次、`pt_key`/`thor` 被动检测、
  URL 离开 passport 检测、profile 保留、失败只结束本次启动的 Chrome；
  淘宝 CDP `_nk_`/`cookie2`+`unb`、1688 CDP `__cn_logon__=true` 严格校验，
  `login.taobao.com` 跳转不算 1688 登录成功。

## 4. 样例输出

离线生成（不访问电商网站）：

```bash
.venv/bin/python tools/make_samples.py
```

| 文件 | 行数 | 说明 |
|---|---:|---|
| `samples/2026-11-05/taobao.jsonl` | 20 | 淘宝/天猫混合，含模糊销量、缺失评论/店铺 |
| `samples/2026-11-05/jd.jsonl` | 20 | 价格异步补全、销量缺失、自营/POP |
| `samples/2026-11-05/1688.jsonl` | 20 | 价格区间、成交件数、实力商家/诚信通 |
| `samples/2026-11-05/{platform}.run.json` | 3 | 运行记录，login_ok/items_ok/pages |
| `samples/2026-11-05/run_logs.jsonl` | 3 | 供后端 run_logs |
| `samples/logs/2026-11-05_{platform}.log` | 3 | 请求/解析日志片段 |
| `samples/rules.sample.yaml` | 1 | 样例配置 |

样例中 `image_path=null`，因为离线样例不下载图片；生产运行开启 `crawl.image.enabled`
后写 `out/{date}/images/` 并填相对路径。

字段示例（京东）：

```json
{"platform":"jd","product_id":"900000","url":"https://item.jd.com/900000.html","title":"葫芦兄弟 儿童卫衣 京东第1款","shop_id":"4000","shop_name":"京东店铺0","shop_label_raw":"京东自营","shop_type_raw_hint":"京东自营","price":29.9,"price_raw":"29.90","sales_raw":null,"sales":null,"comments_raw":"1万+条评价","comments":10000,"main_image_url":"https://img14.360buyimg.com/n7/jd_0.jpg","image_path":null,"category":null,"ip_code":"huluxiongdi","search_keyword":"葫芦兄弟","page":1,"rank":1,"crawled_at":"2026-11-05T13:42:44.045345+08:00","listed":true,"flags":["销量缺失","按下限估算"]}
```

## 5. 借鉴的开源项目

| 项目 | 许可证 | 借鉴内容 | 是否复制代码 |
|---|---|---|---|
| [cv-cat/catbus](https://github.com/cv-cat/catbus) | MIT | 统一命令/JSON 信封、`RISK_CONTROL` 一等错误即停、profile 目录 0700、原子写 | 未复制；按思路自行实现 Python 版 |
| [zhangjiancong/MarketSpider](https://github.com/zhangjiancong/MarketSpider) | MIT | 淘宝/京东/1688 搜索字段模型（链接/价格/名称/店铺/评论）、京东奇数页与滚动、登录 Cookie 流程思路 | 未复制；字段模型参考后重写 |
| [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | 非商业学习许可 1.1 | 借鉴 `ENABLE_CDP_MODE` 思路：复用系统真实 Chrome、按平台独立 profile/端口、CDP 连接已登录浏览器；并借鉴“按平台隔离登录态” | **未复制**；Chrome 启动、端口探测、登录等待均按本项目结构自行实现 |
| [ShilongLee/Crawler](https://github.com/ShilongLee/Crawler) | 非商业使用许可 | 仅借鉴“按平台账号隔离/服务化”的思路 | **未复制** |

Scrapling 0.5.1 为框架依赖（BSD-3-Clause），使用其 `StealthySession`、持久化上下文和
响应解析能力；没有使用 `solve_cloudflare` 处理本场景验证码。

## 6. 验收对照（§3 采集模块）

| 验收项 | 实现 | 证据 |
|---|---|---|
| 三平台统一接口 | `PlatformBase` + 三个子类 | `test_registry_interfaces` |
| product_id/url/title/price/主图/时间/listed | Item 必有字段；缺失 price/图记 flag | `test_validate_*`、样例 |
| shop_id 选填，不卡流程 | 缺失 → null，不再标 `店铺ID缺失`；店名为空才标 `店铺名缺失` | `test_taobao_*`、样例 |
| sales/comments 原文 + 下限 | `*_raw` + `parse_fuzzy_int` | `test_parse_fuzzy_int` |
| 搜索词/ip_code | 矩阵写入每条 Item | `test_build_keyword_matrix` |
| product_id 失败丢弃 | `validate_item` + drop_reasons | `test_validate_*`、runner |
| 每平台持久化 profile | playwright: `user_data_dir`；cdp: `<platform>-chrome`；均 0700 | `test_profile_dirs_are_private`、`test_browser_config_*`、browser 冒烟 |
| 登录失效停平台 | `check_login` / `LoginRequired` | `test_run_platform_login_failure_stops` |
| 风控停平台 + 截图 + 不重试 | `RiskBlocked`、`classify_page`、`retry._NO_RETRY` | `test_risk_page_*`、`test_run_platform_risk_*` |
| 节流/max_pages/每日上限 | 全部来自 YAML | `test_throttle.py` |
| 详情策略 C4/B7 | 采集层只提供 `fetch_detail`；触发由判定层按分数/金额带决定 | 接口测试、README |
| 1688 GBK | `gbk_quote` | `test_gbk_quote_and_invalid_char` |
| 京东异步价格 | 滚动 + 价格 JSON 映射 | `test_jd_json_and_dom_search` |
| 京东 CDP 反反爬 | `browser.mode=cdp`：真实 Chrome + `connect_over_cdp`，不附加自动化参数 | `test_cdp_*`、`test_chrome.py`、真实轻量冒烟 |
| 三平台通用可配置 | `browser.mode` + `platform_modes` + `ports` | `test_browser_config_*` |
| 登录只导航一次 | CDP 打开登录页 1 次，之后只读 URL/Cookie | `test_cdp_login_navigates_once_*` |
| 登录后保留 profile | Chrome profile 0700，退出只断开 CDP、不删除 | `test_cdp_login_*`、真实冒烟 |
| check-login/run 走 CDP | 默认工厂按平台模式创建 `ScraplingBrowser` | `test_default_factory_uses_cdp_for_jd` |

## 7. 已知限制 / 需实测确认

1. **真实页面选择器/接口未实测**：沙箱要求不访问电商站点；`selectors.py` 与各平台
   `_from_json` 的键名按常见结构编写，首次实跑需按实际响应校准。
2. **淘宝 mtop 签名**：只监听浏览器自身发出的 JSON，不伪造签名；若接口需要额外
   token/风控参数，需实跑确认浏览器会话是否已自动携带。
3. **京东异步加载**：滚动次数、价格接口域名、列表页条数、`page=1,3,5` 是否与当前
   版本一致，需要实跑验证；价格非空率指标尚未在真实页面统计。
4. **1688**：GBK 编码已单测；真实中文词命中率、DOM/接口结构、实力商家/诚信通标签
   需要实跑；当前 `keyword_hit_warn_ratio=0.2` 只是保守 warning 阈值。
5. **主图下载**：带平台 Referer，但防盗链、CDN 域名、content-type 需要实跑；
   下载失败只降级为 `image_path=null` + `主图下载失败`，不影响商品行。
6. **profile 静态加密未实现**：当前仅 0700；`CLUE_PROFILE_KEY` 环境变量已预留，
   加密方案需与后端确认。Cookie 不导出、不写日志。
7. **真实登录未自动化测试**：`login` 必须人工操作；Xvfb 下的 headful 启动已用本地
   HTML 冒烟。登录流程只导航一次，之后靠 URL 离开登录域名或登录 Cookie 被动判断，
   淘宝/京东/1688 的真实信号需要 Xuan 首次实跑确认。
   CDP 模式已用真实 Chrome 做轻量冒烟：仅打开京东登录页，`title=京东-欢迎登录`，
   DOM 正常加载，随后只关闭本次启动的 Chrome；未做登录后的真实采集。
8. **CDP 依赖与进程边界**：要求本机安装 Google Chrome（或配置
   `browser.chrome_executable`；Windows 找不到 Chrome 时用 Edge 兜底）。Linux
   还需要 `DISPLAY=:6` 可用；Windows/macOS 使用原生窗口，不检查也不设置 DISPLAY。
   程序退出只断开 CDP，不主动结束 Chrome，避免误关用户/1688 等其他窗口；需要
   释放端口时手动关闭对应窗口。
   同一 profile 被另一个未带调试端口的 Chrome 占用时，启动会失败并在错误里给出
   profile/端口信息。
9. **风控识别是启发式**：只在最终 URL 命中验证页、可见文案命中滑块/验证拦截词、
   或可见 DOM 命中滑块/验证码节点时判风控。脚本/样式/隐藏节点里的 baxia、
   captcha、验证码不会再触发；仍可能因站点改版误判或漏判，命中即停是保守策略，
   宁可少抓。截图在 headless 下仍可保存，但页面内容可能受登录/风控影响。
10. **平台并行**：`crawl.parallel_platforms=true` 时每平台独立线程 + 独立浏览器
   profile；未做真实并发压测。
11. **同日风控熔断**：命中验证码后写 `state/risk/{platform}-{date}.json`，同日运行直接跳过；
    如需提前恢复，人工删除该标记文件（或次日自然失效）。
12. **范围边界**：`shop_type` 归一、金额、聚合、详情触发、SMTP 报警、DB 入库不在
    本模块；本模块只产出规范 JSONL + 运行记录。
13. **样例为合成 fixture**：`samples/` 不代表真实商品/价格，仅验证字段和输出格式。
14. 拼多多未启用；代码中没有拼多多模块。

## 8. 建议的首轮实跑步骤

1. `Xvfb :6 ...` 或接桌面，`python -m crawler login taobao` 人工登录；
   三个平台各一次，确认 `profiles/{platform}` 或 `profiles/{platform}-chrome`
   0700。京东默认 cdp，会直接拉起真实 Chrome。
2. `python -m crawler check-login --platform all`，确认三平台 OK
   （京东/淘宝/1688 都按各自 `browser` 配置连接或启动）。
3. 每个平台用 1 个词 × 1 页试跑：
   `python -m crawler run --platform taobao --keyword 葫芦兄弟 --max-pages 1 --limit-keywords 1`
4. 检查 `out/{date}/{platform}.jsonl` 的字段完整率、`logs/{date}_*.log` 的请求间隔，
   对照 1688 标题命中率、京东价格非空率。
5. 用空 profile 模拟登录失效、用本地假页面模拟风控，确认 `run.json` 的
   `login_ok=false` / `risk_blocked=true` 和截图路径。命中风控后同日重跑应被
   `state/risk/{platform}-{date}.json` 熔断。
6. 按实跑 DOM 修正 `platforms/selectors.py` 与 JSON 键名；只改配置不改代码即可调
   节流/页数/上限。

## 9. Windows 10 pytest 11 个失败逐项分析

现场：Windows 10 + Python 3.13，用户目录含中文和全角括号
`C:\Users\ZhuanZ\...`，pytest 临时目录
`...\Temp\pytest-of-ZhuanZ（无密码）\pytest-1\...`。原始输出见
`/workspace/ip-monitor/pytest-win.txt`（11 failed, 169 passed）。逐项结论：**11 项全为
测试写死的 Linux/POSIX 假设或测试隔离不足，未发现生产代码在 Windows 下的真实 bug；
因此只改测试，未改采集逻辑。** 当时 Linux 全量 `180 passed`（本次 baxia 修复后
为 `195 passed`）；Windows 目标为
`179 passed + 1 skipped`（跳过的是只验证 X11 的用例）。

| # | 失败用例 | 原因分类 | 具体原因 | 处理 |
|---:|---|---|---|---|
| 1 | `test_browser.py::test_scrapling_browser_wraps_session` | POSIX 权限假设 | `save_login_marker` 写出的 `login.json` 断言 `st_mode & 0o777 == 0o600`；Windows 的 `st_mode` 为 `33206`（`0o100666`），`os.chmod` 只能控制只读位，不表达 0600。生产代码写文件/`os.replace` 正常。 | 新增 `assert_private_mode(path, 0o600)`：Windows 只断言文件存在，POSIX 才断言权限位。 |
| 2 | `test_chrome.py::test_browser_config_defaults_and_ports` | POSIX 权限假设 | `browser_profile_dir("jd")` 目录断言 0700；Windows 返回 `16895`（`0o40777`），无 0700 语义。目录创建本身正常。 | 改用 `assert_private_mode(..., 0o700)`，Windows 跳过权限位断言。 |
| 3 | `test_chrome.py::test_find_chrome_executable_prefers_which_order` | Linux 查找顺序假设 + 真实 Chrome 干扰 | 该用例验证 POSIX `CHROME_CANDIDATES` 的 `which` 顺序；Windows 分支会先查固定安装路径，本机真实 `C:\Program Files\Google\Chrome\Application\chrome.exe` 命中，根本不会走 `which`。这是平台分支设计，不是查找 bug。 | 新增 `posix_chrome` fixture，显式把 `crawler.core.chrome.sys.platform` 设为 `linux`，在 Windows 上也只测试 POSIX 分支。 |
| 4 | `test_chrome.py::test_find_chrome_executable_missing` | Linux 查找假设 + 真实 Chrome 干扰 | 用例把 `shutil.which` 置空后期望 `BrowserUnavailable`；Windows 先命中真实安装的 Chrome，故不会抛异常。 | 同样使用 `posix_chrome` fixture，固定走 POSIX 且 `which` 为空的分支。 |
| 5 | `test_chrome.py::test_ensure_chrome_launches_only_fixed_arguments` | Linux 启动参数假设 + POSIX 权限假设 | 用例期望 `start_new_session=True`、`DISPLAY=:6`、可执行文件 `/usr/bin/google-chrome`；Windows 正确行为是 `creationflags`（独立进程组/脱离控制台）、不传 `DISPLAY`，该分支已由 `test_windows.py` 覆盖。末尾还断言 profile 0700。 | 加 `posix_chrome` fixture 固定测试 POSIX 分支；权限改 `assert_private_mode`；Windows 行为继续由 `test_ensure_chrome_windows_detached_no_display` 覆盖。 |
| 6 | `test_config.py::test_profile_dirs_are_private` | POSIX 权限假设 | 三平台 profile 目录断言 0700；Windows `st_mode` 不反映 ACL。`ensure_private_dir` 在 Windows 仍会创建目录并尝试 `chmod`（只影响只读位），不会失败。 | 改用 `assert_private_mode(path, 0o700)`：Windows 断言目录存在，Linux 断言 0700。 |
| 7 | `test_images.py::test_download_success_and_reuse` | 路径分隔符假设 | 生产代码返回原生路径 `images\taobao\123.jpg`，这是 Windows 正确行为；测试用字符串 `endswith("images/taobao/123.jpg")` 写死 `/`。 | 改为 `Path(path) == Path("images") / "taobao" / "123.jpg"`，按路径语义比较，跨平台一致。 |
| 8 | `test_login.py::test_display_validation` | Linux X11 专用假设 | 该用例验证空 `DISPLAY`/非法 `DISPLAY` 失败；Windows/macOS 原生窗口不需要 X11，`validate_display` 有意直接返回 `(True, "")`，由 `test_windows.py::test_validate_display_skipped_on_native_platforms` 覆盖原生分支。 | 在 Windows/macOS 上 `pytest.mark.skipif`，原因写明“不使用 X11，X11 分支由 Linux 覆盖”；Linux 继续执行完整断言。 |
| 9 | `test_runner.py::test_same_day_risk_circuit_breaker` | POSIX 权限假设 | 风控标记 `state/risk/taobao-2026-11-05.json` 断言 0600；Windows `st_mode` 为 `33206`。熔断逻辑本身（写入、同日二次运行不再请求）已通过。 | 改用 `assert_private_mode(marker, 0o600)`，Windows 只断言标记文件存在。 |
| 10 | `test_throttle.py::test_daily_budget_new_date_resets` | POSIX 权限假设 | 每日预算 `daily.json` 断言 0600；Windows 无该权限位。跨日期重置逻辑本身已通过。 | 改用 `assert_private_mode(path, 0o600)`。 |
| 11 | `test_windows.py::test_find_chrome_windows_localappdata` | 测试隔离不足（非代码 bug） | 用例把 `%LOCALAPPDATA%` 指向临时目录，但 `find_chrome_executable` 在 Windows 按“Program Files → Program Files (x86) → %LOCALAPPDATA% → PATH”查找，本机真实 Chrome 在 Program Files 先命中，导致返回真实路径而不是临时路径。 | 在该用例中显式 monkeypatch `WINDOWS_CHROME_CANDIDATES` 只保留 `%LOCALAPPDATA%` 候选，并清空 `WINDOWS_EDGE_CANDIDATES`，彻底隔离真实已安装 Chrome/Edge；查找代码不改。 |

补充说明：

- 未修改 `crawler/core/chrome.py`、`config.py`、`login.py`、`images.py` 等生产逻辑；
  这 11 项均不是 Windows 运行时功能缺陷。
- `find_chrome_executable` 的 Windows 分支保留真实优先级，仅测试侧隔离真实 Chrome；
  新增/保留的 `test_find_chrome_windows_program_files`、`..._localappdata`、
  `..._edge_fallback` 仍覆盖 Program Files、LocalAppData、Edge 兜底。
- POSIX 权限断言只在 `os.name == "posix"` 时执行；Windows 下仍断言文件/目录确实创建。
  这不是放宽生产安全要求，而是 Windows 的 ACL 模型无法用 `st_mode` 的 0600/0700 表示。
- 未发现时区/日期相关失败；配置中的 `Asia/Shanghai` 由 `tzdata` 提供，当前用例均使用
  显式日期或 UTC fixture，不受 Windows 本地时区影响。

## 10. baxia 风控误报修复（2026-11-05）

### 问题

淘宝每个页面（包括已登录的正常页面）都会加载 `baxiaCommon.js`、`aplus-baxia`
或写入 `window.initBaxia`。旧版 `risk.py` 把整段 HTML 小写后做 `baxia` 子串匹配，
因此正常登录页也被判成风控；同类宽泛文案还包括 `captcha`、`nc_1_wrapper` 和
单独的“验证码”，会命中脚本、隐藏预加载节点、商品标题或正常短信验证码登录页。
京东/1688 与淘宝共用同一套 `RISK_TEXT_MARKERS`，存在同样问题。

### 修复内容

- `crawler/core/risk.py`
  - 风控文案只从**可见 DOM 文本**提取：跳过 `script/style/noscript/template/svg/
    canvas`、注释，以及 `display:none`、`visibility:hidden`、`opacity:0`、
    `hidden` 属性和常见隐藏 class 的节点。
  - 删除 `baxia`、`captcha`、`nc_1_wrapper`、单独“验证码”等过宽文案；保留
    “拖动滑块/滑块验证/请完成安全验证/人机验证/访问受限/异常流量”等强拦截文案。
  - 风控 URL 只看**最终页面 URL**（`punish`、`_____tmd_____`、`x5sec`、
    `/risk_handler`、`/captcha`、`/verify` 等）及验证域名；不再扫描整段 HTML，
    避免正常页面里隐藏 iframe/redirectURL 的 `_____tmd_____` 误报。
  - DOM 风控选择器增加 baxia 弹层/iframe、nc 滑块、JDJRV 滑块、captcha/punish
    iframe 等，并要求节点在静态 HTML 中也可见。
- `crawler/core/browser.py`
  - `_collect_dom_hints()` 从“选择器 count>0”改为 `locator.first.is_visible()`，
    隐藏的预加载验证层不再作为风控线索。
- 京东/1688：共用修复后的判定；新增 JDJRV 滑块选择器，1688 继续覆盖
  `.nc-container` / `#nc_1_wrapper` 等阿里滑块结构。

### 测试

- 新增 fixture `crawler/tests/fixtures/normal_baxia.html`：正常已登录页面，
  含 baxia 脚本、`window.initBaxia`、`aplus-baxia`，以及隐藏的 baxia/nc 预加载层。
- 新增用例：
  - `test_normal_page_with_baxia_scripts_and_hidden_layers_is_ok`（淘宝/京东/1688）
  - `test_visible_baxia_dialog_or_iframe_is_risk`
  - `test_login_iframe_redirect_url_with_tmd_query_is_not_risk`
  - `test_visible_slider_without_text_is_risk_for_all_platforms`（nc/JDJRV/1688）
  - `test_verification_page_url_is_risk`（punish/`_____tmd_____`/risk_handler 等）
  - `test_broad_words_in_scripts_or_product_titles_are_not_risk`
  - `test_collect_dom_hints_only_returns_visible_nodes` /
    `test_collect_dom_hints_ignores_hidden_nodes`
- 全量结果：`.venv/bin/python -m pytest -q` → **195 passed**。
- 另用 `logs/debug/` 下 7 份真实正常页面快照做离线回归，全部返回 `ok`。

## 11. 京东 / 1688 搜索列表解析加固（2026-11-05）

### 问题定位

Windows 本机 Edge+CDP、已登录、无风控，关键词“奥特曼”各采 1 页：

- 淘宝：48 条，另有 2 条 `product_id` 缺失被丢弃，来源 `taobao-dom`；
- 京东、1688：`pages_fetched=2`（登录检查 + 1 页搜索），但 `items_ok=0`、
  `items_dropped=0`，说明 `parse_search` **一张商品卡都没匹配到**，不是字段校验
  丢弃，而是列表 DOM/接口结构已经改版。

### GitHub 调研（2026 年仍维护 / 近期有真实页面实跑）

| 项目 | Stars / 最近提交 | 对当前列表页的关键参考 |
|---|---|---|
| [ShilongLee/Crawler](https://github.com/ShilongLee/Crawler) | 1.5k / 2026-10 | 京东搜索仍可用 HTML：`#J_goodsList li.gl-item`、`p-name`/`p-price`/`p-shop`，页码 `page=2n-1`，并从页面脚本取 `result_count` |
| [zhangjiancong/MarketSpider](https://github.com/zhangjiancong/MarketSpider) | 446 / 2025-05 | 京东 `#J_goodsList > ul > li`；1688 使用 `#sm-offer-list > div` 及 `mojar-element-title` / `mojar-element-price` / `mojar-element-company` 结构 |
| [xingxing68876/price-radar](https://github.com/xingxing68876/price-radar) | 2026-09 真实 CDP 实跑 | 京东 2026-09 新结构：卡片是 hash class 的 `[data-sku]` 元素，**没有 `<a href>`**，标题/价格在 `innerText`；1688 卡片是 `a.search-offer-item`，offerId 在 href 或内部 `offerIds` 参数 |
| [Carmenliukang/1688_crawler-image_search_products](https://github.com/Carmenliukang/1688_crawler-image_search_products) | 203 / 2026-06 | 1688 `h5api.m.1688.com` / mtop JSONP 形态（`mtopjsonp1({...})`）与 `_m_h5_tk` token；用于确认 JSONP 去包装和接口兜底 |
| [0xAllenChen/spider_reverse](https://github.com/0xAllenChen/spider_reverse) | 938 / 2026-10 | 1688 h5api/签名逆向案例；只参考接口识别思路 |

以上均**未复制代码**，只按“当前 DOM 双结构 + 内嵌 JSON + JSONP/XHR”思路重写本项目解析。

### 实现内容

- `crawler/core/jsonutil.py`
  - `parse_json_text()` 支持 JSONP（`mtopjsonp1({...})`、`jsonpCBK({...})`）和
    反 XSSI 前缀（`)]}'`、`for(;;);`），不再只认严格 JSON。
  - 新增 `extract_embedded_json()`：从 `<script type="application/json">`、
    `__NEXT_DATA__` 以及 `window.__INIT_DATA__` / `__INITIAL_STATE__` /
    `__SEARCH_DATA__` / `pageData` / `offerList` 等已知全局变量提取首屏 JSON。
  - `extract_records()` 的商品启发式扩展到 `raw_title` / `view_price` /
    `wareName` / `offerTitle` / `priceRange` 等真实字段，便于在无 `product_id`
    时也能统计 drop 并触发 debug 快照。
- `crawler/core/browser.py`
  - `fetch_page()` 在 Scrapling 的 `page_wait_ms` 等待结束后再 `collect()` 一次，
    捕获 `page_action` 返回后才到达的懒加载/翻页 XHR，避免列表接口漏采。
- `crawler/platforms/base.py`
  - `payloads()` = XHR/JSONP 响应 + 页面内嵌 JSON。
  - 新增 `save_debug_snapshot()`：同一页只落一次
    `logs/debug/<platform>_<YYYYmmdd_HHMMSS_微秒>.html/.png`，写 warning：
    `列表解析 0 条 ... debug_html=... debug_png=...`；淘宝 `product_id` 缺失
    也落同一套快照。
  - 搜索流程在解析 0 条或 `product_id` 缺失时自动落盘；各平台 `parse_search`
    内部也落一次，直接调用解析器单测同样有效。
- `crawler/platforms/selectors.py`
  - 京东新增 `[data-sku]`、hash class 卡片、`goodsName`/`skuName`/`priceNum`
    等 2026 结构选择器。
  - 1688 新增 `a.search-offer-item`、`#sm-offer-list > div`、
    `offer-card`、`mojar-element-*`、`offerTitle` 等新旧结构。
- `crawler/platforms/jd.py`
  - 多策略：DOM 卡片（含无 `<a href>` 的 `[data-sku]`）→ 从 `item.jd.com`
    链接反推卡片 → 卡片文本兜底标题/价格/评论 → 页面内嵌 JSON → XHR/JSONP；
  - 按 product_id 合并去重，价格接口 `p.3.cn`/`c0.3.cn`/`api.m.jd.com` 补价。
- `crawler/platforms/ali1688.py`
  - 多策略：`a.search-offer-item` / `#sm-offer-list > div` / `mojar-*` 新旧
    DOM → offer 链接反推卡片 → 从 href、`data-offer-id`、`offerIds` 提取 ID →
    内嵌 JSON → h5api/mtop JSONP；
  - 过滤 `#sm-offer-list > div` 带出的标题/价格字段 div，避免误 drop；
  - XHR 监听收窄为 `offer_search` / `offerList` / `h5api` / `mtop` /
    `s.1688.com/selloffer`，避免页面 JS/CSS 占满响应配额导致列表接口漏采。
- `crawler/platforms/taobao.py`
  - DOM 无 `product_id` 时立即落 HTML+截图快照并写 warning；JSON 路径缺 ID 由
    `base.search()` 的 drop 差值兜底。
- `config/windows.yaml` **未改动**，`chrome_executable` 仍为原来的 Edge 路径。

### 测试

新增 fixture：

- `crawler/tests/fixtures/jd_search_2026.html`：`[data-sku]` hash 卡片、
  无 `<a>` 卡片、纯文本标题/价格卡片、`window.__INIT_DATA__` 内嵌商品；
- `crawler/tests/fixtures/ali1688_search_2026.html`：`a.search-offer-item`、
  `#sm-offer-list > div` / `mojar-*`、`offerIds` 内链三种结构。

新增用例覆盖：

- 京东 2026 DOM + 内嵌 JSON 混合解析（4 条，含无价格 class 的文本兜底）；
- 1688 2026 三种卡片结构解析（3 条）；
- 京东 JSONP XHR、1688 `mtopjsonp1` XHR 解析；
- 搜索页 0 条自动生成 `logs/debug/jd_*.html/.png` 并写 warning；
- 淘宝 DOM 缺 `product_id`、淘宝 JSON 缺 `product_id` 均生成
  `logs/debug/taobao_*.html/.png`；
- `parse_json_text` JSONP/反 XSSI、`extract_embedded_json` 全局变量。

全量结果：`.venv/bin/python -m pytest -q` → **204 passed**，覆盖率 **84%**
（全部离线 fixture，未访问真实电商站点）。

### 下次 Windows 实测

如果仍出现 0 条，直接查看 `crawler/logs/debug/jd_*.html/.png` 或
`1688_*.html/.png`；warning 中已带 keyword / page / 最终 URL / 文件路径。
注意第 2 页正常结束也会留下 0 条快照，这是排查需求，不是错误。

---

**CRAWLER_DONE**

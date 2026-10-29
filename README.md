# 中信证券自动化跟单

驱动 macOS 上的中信证券网上交易客户端（通达信内核）完成下单与查询，
支持 HTTP 信号跟单、仓位管理、交易界面自动解锁。

## 为什么是这个架构

中信客户端是**纯自绘界面**：二进制里没有任何 `AXUIElement` 导出符号，
窗口子控件全部无标题，表格逐像素绘制、没有 `AXTable`。

所以读界面只能靠「截图 + Vision OCR + 坐标重排」，操作只能靠「合成鼠标键盘」。
这条链路又慢又脆，因此：

- **核心常驻**（Rust），避免每次调用都重新定位窗口、重新走 OCR 初始化
- **按需裁切截图** —— 实测整窗 OCR 0.67 秒，只截下单区 0.25 秒
- **界面与核心分离**：核心是独立进程，经本地 Unix socket 收发 JSON

界面用 Python + Tkinter，只负责配置与展示，不做任何自动化动作。
这样核心可以单独跑、单独测，将来换界面时核心一行都不用改。

```
┌──────────────┐   Unix socket    ┌──────────────────┐
│  gui/app.py  │ ◄──────────────► │  core (Rust)     │
│  Tkinter     │  JSON，一行一条  │ 截图/OCR/点击/解锁 │
└──────────────┘                  └──────────────────┘
                                           │
                                           ▼
                                  中信证券网上交易客户端
```

## 目录结构

```
core/                Rust 自动化核心
  src/capture.rs     窗口截图（CoreGraphics + ImageIO 直写 PNG）
  src/ocr.rs         OCR（调用 tools/ocr），支持按区域裁切
  src/input.rs       鼠标键盘合成（CGEvent）+ 辅助功能读写
  src/panel.rs       面板坐标表、表格读取、窗口定位
  src/unlock.rs      锁检测、自动解锁、失败熔断
  src/config.rs      配置读写
  src/ipc.rs         IPC 服务与请求分发
gui/app.py           界面
tools/ocr.swift      Vision OCR（需编译）
tools/build_ocr.sh   编译脚本
```

## 配置与数据

**全部放在用户目录，不进仓库**（里面有交易密码）：

```
~/.lingtradebridge-citics/
├── config.json         配置（权限 600）
├── unlock_state.txt    解锁失败计数与熔断标志
└── logs/               日志
```

| 配置键 | 说明 |
|---|---|
| `app_path` | 客户端 App 路径 |
| `trade_password` | 交易密码，空表示未配置 |
| `auto_unlock` | 是否在检测到锁定时自动解锁 |
| `unlock_interval` | 检查间隔（秒） |
| `unlock_max_fail` | 连错几次后永久停手 |
| `settle` | 每次点击后的等待（秒） |

## 使用

首次需要编译 OCR 工具和核心：

```bash
./tools/build_ocr.sh
cd core && CARGO_HOME=.cargo cargo build --release
```

启动界面（二选一）：

```bash
# 日常使用：双击 启动中信跟单.app —— 无终端窗口，Dock 里显示本应用的名字和图标
open 启动中信跟单.app

# 调试：保留终端，能实时看到输出和报错
python3 gui/app.py
```

`.app` 必须和 `gui/`、`core/` 放在同一级目录里（它靠相对路径找项目根）。
单独把它拖到 `/Applications` 会导致 `gui/app.py` 找不到。
启动器的输出写在 `~/.lingtradebridge-citics/logs/启动器_日期.log`。

在界面上填交易密码 → 保存 → 启动核心，可用「立即解锁一次」验证密码。
核心必须重启才能读到新保存的配置。

环境自检（屏幕录制、辅助功能、OCR、客户端进程）：

```bash
cd core && CARGO_HOME=.cargo ./target/release/citics-core --check
```

权限：需要**屏幕录制**和**辅助功能**两项授权（系统设置 → 隐私与安全性），
否则截图返回空、事件发不出去。

## IPC 协议

本地 Unix socket（`/tmp/citics-core.sock`，权限 600），一行一个 JSON。
**不是 TCP 端口** —— 只有本机、只有本用户能连，不需要另做鉴权。

```jsonc
{"id":1,"op":"ping"}                      // 内核存活探测，返回 core_pid
{"id":2,"op":"status"}                    // 客户端与锁状态
{"id":3,"op":"unlock"}                    // 用配置里的密码解锁
{"id":4,"op":"unlock","password":"..."}   // 指定密码
{"id":5,"op":"reset_unlock"}              // 手动重置熔断
{"id":6,"op":"shutdown"}                  // 让内核自行退出（GUI 停止外部内核用）
```

响应带同一个 `id`，`ok` 表示成败，`error` 说明原因。
密码经本机 socket 传输，不落盘、不回显、不写日志。

### 交易接口按账户分命名空间

普通账户和信用账户是**两套平行的界面**：顶部菜单不同（买入/卖出 vs 担保品买入/
担保品卖出）、持仓委托各自独立、信用区还多出一批普通区没有的动作（融资买入、
融券卖出、还款还券）。所以交易类接口不是一个 `account` 参数，而是两套命名空间：

| 普通账户 | 信用账户 | 说明 |
|---|---|---|
| `normal.query` | `credit.query` | `kind`: positions / orders / deals / funds |
| `normal.buy` | `credit.buy` | 下单买入 |
| `normal.sell` | `credit.sell` | 下单卖出 |
| `normal.cancel` | `credit.cancel` | 撤销指定委托 |
| `normal.cancel_list` | `credit.cancel_list` | 读可撤列表 |

```jsonc
{"id":8,"op":"credit.query","kind":"positions"}
{"id":9,"op":"credit.buy","code":"600000","price":"10.00","amount":100}
```

方向编进动作名（`buy` / `sell`），不再单独传 `direction` —— 少一个能传错的字段。
响应里的 `account` 字段回显本次实际用的账户，**要核对它而不是只看 `ok`**。

信用账户尚未实现的动作会明确报「尚未实现」而不是「未知操作」，说明这些名字是
预留的：`credit.margin_buy`（融资买入）、`credit.short_sell`（融券卖出）、
`credit.repay_cash`（现金还款）、`credit.repay_stock`（卖券还款）、
`credit.buy_to_repay`（买券还券）、`credit.return_stock`（现券还券）。

旧写法 `{"op":"query","kind":"positions","account":"credit"}` 仍然可用
（HTTP 跟单链路内部就是这么调的），新代码请用命名空间。

## HTTP 接口

给外部策略/信号源用。默认 `127.0.0.1:8899`，除 `/health` 和 `/` 外都要
`Authorization: Bearer <http_api_token>`（token 为空时**服务拒绝启动**）。

和 IPC 一样按账户分两套命名空间 —— 信用账户的持仓、委托与普通账户完全独立，
信用区还多出一批普通区没有的动作：

| 普通账户 | 信用账户 |
|---|---|
| `POST /normal/signal` | `POST /credit/signal` |
| `GET /normal/positions` | `GET /credit/positions` |
| `GET /normal/orders` | `GET /credit/orders` |
| `GET /normal/deals` | `GET /credit/deals` |
| `GET /normal/account` | `GET /credit/account` |

```bash
TOKEN=$(配置里的 http_api_token)

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8899/credit/positions

curl -X POST -H "Authorization: Bearer $TOKEN" \
     -d '{"action":"buy","security":"600000","amount":100,"price":10.5}' \
     http://127.0.0.1:8899/credit/signal
```

**路径里的账户优先于配置**。`/credit/signal` 明确要求在信用账户下单，
就不去看配置里的 `follow_account` —— 调用方写明了用哪套账户，再去读配置
是跟调用方对着干。响应里回显 `account` 字段，核对它而不是只看 `ok`。

信用账户尚未实现、但名字已预留的接口返回 **501**（不是 404，免得看起来
像路径拼错）：

```
POST /credit/margin_buy     融资买入
POST /credit/short_sell     融券卖出
POST /credit/repay_cash     现金还款
POST /credit/repay_stock    卖券还款
POST /credit/buy_to_repay   买券还券
POST /credit/return_stock   现券还券
```

老路径 `/signal`、`/positions` 等继续可用（同花顺版的策略推送代码不用改）：
账户取配置里的 `follow_account`，查询默认普通账户。

## 内核生命周期

**内核可以完全脱离界面独立运行**（命令行、`launchd`、别的工具拉起来），
界面是可选的运维入口，不是必需组件。因此：

- `ping` / `status` 都返回 `core_pid`，界面据此显示内核 pid 和来源（本界面 / 外部）。
- 界面启动时先用 `ping` 探活：没内核就自动拉一个，已有内核就直接接管，
  **绝不重复启动**。
- 内核启动前也会反向探测 socket：能连上说明已有内核在跑，直接拒绝启动
  （否则 `bind` 会顶掉别人的 socket 文件，旧进程变成收不到请求的僵尸）；
  连不上说明只是上次异常退出的残骸，清掉再用。
- 「停止核心」对非本界面启动的内核会二次确认，然后走 `shutdown` op
  让内核自己退 —— 比按 pid 发信号干净，内核会顺手删掉 socket 文件。

## 自动解锁的安全设计

实测发现：**密码输错时中信不提示，直接关掉解锁对话框并退回行情界面**。
程序无法从界面得知这次成没成功，只能靠「解锁后是否还在锁定态」推断。

而券商对连续输错有账户锁定策略。所以：

- 解锁入口只有 `unlock_guarded` 一个，熔断逻辑绕不过去
- 连续失败达 `unlock_max_fail`（默认 3）后**永久停手**
- 熔断状态写盘，**重启程序不会自动解除**，必须人工重置

另一处实测修正：锁屏判据不能只看辅助功能特征。
行情界面右上角的搜索框与锁屏密码框位置尺寸完全一致
（`889.5, 425.6, 198, 32`），只看 AX 会把行情界面误判成锁定，
进而把交易密码敲进搜索框**并明文显示在屏幕上**。
现在 AX 只作快速预筛，必须 OCR 实读到「交易界面已锁定」才认定锁定。

## 已知限制

- **坐标绑定窗口尺寸**。坐标是在 1920×956 全窗口下实测的，换分辨率需重新标定。
  填单价格/数量按 OCR 标签锚定，自适应性强一些；导航仍是绝对坐标。
- **OCR 是唯一信息源**。列密时可能合并或错位，靠「共N条」交叉校验兜底，
  失败会明确报错而不是猜。
- **提交与撤单尚未实盘验证**。填单链路确认过，但点击提交后的二次确认弹窗
  与成功/失败判定仍是推测，首次实盘必须盯着。
- 暂不支持 Redis 信号通道，仅 HTTP。

## 当前进度

已完成：核心骨架、截图、OCR、锁状态识别、自动解锁与熔断、配置管理、GUI。

进行中：面板导航、填单、四类查询（持仓/委托/成交/资金）。

未开始：仓位管理、信用账户独有委托（融资买入/融券卖出/还款还券）。

## 与其他子项目的关系

| 项目 | 关系 |
|---|---|
| `LingStrategies` | 提供策略代码和研究资料；本项目不负责策略研究。 |
| `LingRun` | 提供本地策略运行能力；本项目是独立的信号接收与跟单路径。 |
| `LingJQcli` | 提供聚宽平台工具；策略信号可以来自聚宽相关运行环境。 |
| `LingData` | 提供金融数据基础设施；本项目不负责数据存储和查询。 |
| `webconsole` | 提供统一控制台；本项目当前通过自己的启动入口运行。 |

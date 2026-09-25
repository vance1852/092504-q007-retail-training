# 零售赛训运营协作基础服务

本项目提供零售训练机构、模拟门店、操作人员与运营资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在此之上，项目内置**零售赛训库存与承诺协调服务**：多组选手订单共用一套模拟库存，服务管理商品批次、货架与后仓数量、保质窗口、顾客承诺与补货任务，按已发布的服务规则为候选承诺生成可解释的分配方案；确认时核对库存版本并一次性落账，失败不留部分扣减。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、零售协调服务、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance          # 基础登记链路
PYTHONPATH=src python3 -m skills_workspace.retail_acceptance   # 零售库存与承诺协调
```

零售验收在临时 SQLite 数据库中跑通完整场景：高优先级承诺打烊后到达并按营业日结转、按已发布规则分配、过期批次被跳过、过期方案确认整体冲突且不留部分扣减、取消只释放未履约数量、主管干预记录理由。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## 零售协调服务概念

- **商品批次**：按 `batch_id` 登记，记录货架数量、后仓数量与保质截止日 `expires_on`。批次在截止日当天及之前可分配，过期批次永远不会被分配，只会在方案中以 `expired_batch_skipped` 标注。
- **保质窗口**：承诺可声明 `min_remaining_days`，剩余保质天数不足的批次对该承诺不可分配（`window_too_short`）。
- **库存版本**：每个 `(场所, SKU)` 一个版本号。收货、承诺创建/取消/履约、锁定、调优先级、补货完成、方案确认都会递增版本。分配方案记录生成时的版本，确认时必须一致，否则整体冲突，需重新生成方案。
- **顾客承诺**：登记时按门店所在地营业日程结转——打烊或休息日到达的请求，生效时间顺延到下一营业时段开始，营业日取生效时间的当地日期；结转只影响排序用的生效时间，承诺登记后即进入候选集合。
- **服务规则**：按场所发布（`POST /retail/rules`，仅 admin），新版本发布后旧版本自动失效。规则字段：
  - `candidate_order`：候选排序键，可选 `locked_desc`、`priority_desc`、`effective_at_asc`、`promise_id_asc`；
  - `batch_order`：批次排序键，可选 `expires_on_asc`（FEFO）、`batch_id_asc`；
  - `location_order`：`shelf`/`backroom` 的取用顺序；
  - `allow_partial`：是否允许部分分配，为 `false` 时不足量的承诺整体不分配（`partial_not_allowed`）；
  - `min_remaining_days_default`：承诺未声明时的默认保质窗口。
  未发布规则时使用内置默认规则（`rule_set_id=default`）。
- **分配方案**：`POST /retail/plans` 生成 draft 方案，包含批次快照、候选排序、每条承诺的分配明细与原因码；同 SKU 旧 draft 自动作废。`POST /retail/plans/confirm` 核对库存版本与营业日后在单个事务内落账：扣减批次、写入分配台账、更新承诺、递增版本，任何一步失败整体回滚。
- **取消与履约**：履约只消耗已分配数量；取消先冲销未分配部分、再释放已分配库存回补批次，已履约数量不受影响。
- **主管干预**：`supervisor` 或 `admin` 角色可锁定紧急承诺（排序最前）或调整优先级，`reason` 必填并写入哈希链审计。
- **补货任务**：`POST /retail/replenishments` 开立后仓到货架的移仓任务，完成时按 FEFO 顺序移仓，后仓不足则整体失败。

### 方案原因码

- `locked_first`：锁定承诺优先；
- `batch_order:<键>` / `location_order:<位置>`：分配明细引用的规则键；
- `window_too_short:<批次>`：批次剩余保质天数不满足承诺窗口；
- `expired_batch_skipped:<批次>`：过期批次被跳过；
- `insufficient_usable_stock`：可用库存不足；
- `partial_not_allowed`：规则禁止部分分配。

### 承诺解释视图

`GET /retail/promises/explain?promise_id=...` 返回一笔承诺获得（`gained`）、部分获得（`partial`）、延迟（`delayed`）或失去库存的完整计算与审计依据：当前状态与数量台账、相关每个方案的候选排序/批次快照/分配明细与原因码、已落账的分配记录，以及该承诺与相关方案的哈希链审计事件。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持机构、操作者、场所和领域资料登记，以及审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。

### 零售接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /retail/schedules` | 设置场所营业日程（营业时段、休息 weekday） |
| `POST /retail/rules` / `GET /retail/rules?site_id=` | 发布 / 查询服务规则 |
| `POST /retail/batches` / `GET /retail/stock?site_id=&sku=` | 批次收货 / 库存查询 |
| `POST /retail/promises` / `GET /retail/promises?site_id=` | 登记承诺 / 列表 |
| `GET /retail/promises/detail?promise_id=` | 承诺当前状态 |
| `GET /retail/promises/explain?promise_id=` | 承诺完整计算与审计依据 |
| `POST /retail/promises/cancel` / `fulfill` | 取消（只释放未履约）/ 履约 |
| `POST /retail/promises/lock` / `priority` | 主管锁定 / 调优先级（必填理由） |
| `POST /retail/plans` / `GET /retail/plans?plan_id=` | 生成方案 / 查询方案 |
| `POST /retail/plans/confirm` | 核对版本并一次性落账 |
| `POST /retail/replenishments` / `complete` / `cancel` | 补货任务开立 / 完成 / 取消 |

所有写接口均要求 `request_id`：相同消息安全重放并返回原回执（HTTP 200），相同 `request_id` 携带不同载荷返回 `409 conflict`。

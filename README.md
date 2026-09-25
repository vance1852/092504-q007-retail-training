# 零售赛训运营协作基础服务

本项目提供零售训练机构、模拟门店、操作人员与运营资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在此之上，项目内置**零售赛训库存与承诺协调服务**：管理商品批次、货架与后仓数量、保质窗口、顾客预留及补货任务，按已发布的服务规则（`retail-allocation-v1`，规则 R1–R8）为候选请求生成可解释的分配方案。核心语义：

- **版本核对落账**：方案生成时不扣库存，确认时核对库存版本并在同一事务内一次性扣减，版本冲突或库存不足整体回滚，不留部分扣减；
- **优先级排序**：候选承诺按主管锁定、优先级、承诺时间排序重算，先到的离线回执不会挤占后到但优先级更高的承诺；
- **营业日结转**：门店关闭期间到达的请求按所在地营业时间结转到下一营业日；
- **保质窗口**：过期批次不参与分配，截止日当天仍可分配；
- **取消与履约**：取消只释放尚未履约的数量，已履约部分保持扣减；
- **主管干预**：主管可锁定紧急承诺或调整优先级，必须记录理由并进入审计链；
- **幂等重放**：相同消息安全重放返回原回执，载荷变化复用编号返回冲突；
- **可解释查询**：承诺解释接口展示获得、延迟或失去库存的完整计算过程、规则轨迹与审计依据。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、库存承诺协调、HTTP 路由和离线验收；
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
PYTHONPATH=src python3 -m skills_workspace.acceptance
PYTHONPATH=src python3 -m skills_workspace.inventory_acceptance
```

第一条命令验收基础登记链；第二条在临时 SQLite 数据库中跑通批次入库、优先级重算、版本核对落账、部分履约后取消、主管锁定、幂等重放与解释查询，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，所有写入携带 `request_id` 保证幂等。服务重启后，SQLite 中的业务状态和审计链继续保留。

### 基础登记接口

- `POST /organizations`、`POST /actors`、`POST /sites`、`POST /domain-records`
- `GET /domain-records?site_id=&category=`、`GET /audit-events?after_sequence=`

### 库存与承诺接口

- `POST /store-calendars`：设置门店营业时间（`open_time`、`close_time`，HH:MM）
- `POST /inventory/batches`：商品批次入库（`batch_id`、`sku`、`expires_on`、货架/后仓数量）
- `POST /commitments`：创建顾客预留或订单承诺并生成分配方案（`kind`、`quantity`、`priority`、`promised_at`）
- `POST /inventory/replans`：按锁定、优先级、承诺时间重算某商品全部待确认承诺
- `POST /plan-confirmations`：核对 `expected_version` 并一次性落账
- `POST /commitment-fulfillments`：按分配行履约
- `POST /commitment-cancellations`：取消承诺，只释放未履约数量
- `POST /commitment-overrides`：主管动作（`action` 为 `lock`、`unlock`、`set_priority`，必须给出 `reason`）
- `POST /inventory/replenishment-tasks`、`POST /inventory/replenishment-executions`：补货任务创建与执行
- `GET /inventory?site_id=&sku=`：批次、货架/后仓数量、保质状态与库存版本
- `GET /commitments?site_id=&status=`、`GET /replenishment-tasks?site_id=&status=`
- `GET /commitment-explanation?commitment_id=`：承诺获得、延迟或失去库存的完整计算与审计依据
- `GET /plans?plan_id=`、`GET /allocation-rules`

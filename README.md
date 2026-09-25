# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 投用观察

变更投产（`commission`）必须提交`observation_deadline`（观察截止时间，ISO-8601，须晚于当前时间），投产后进入观察期：

- `submit_reading`（engineer/admin）：按采样点登记`sample_point`、`min_limit`/`max_limit`（至少一个）、`value`、`read_at`。超出限值的读数生成顺序异常编号并计入累计异常；同一采样点可重复上报，历次读数全部保留，最新读数用于判断采样点当前状态。
- `dispose_anomaly`（engineer/admin）：按`anomaly_id`登记`disposition`处置说明，处置后该异常不再阻塞关闭；重复处置返回冲突错误。
- `close`：`commissioned`状态下只有过了观察截止时间、且无未处置异常时才能关闭，关闭结果写入`data.observation_result`（截止时间、读数总数、异常数、各采样点汇总）。观察期未结束或存在未处置异常时会被拒绝。
- `rollback`：存在未处置观察异常时仅安全员（safety）可批准回退；无异常时engineer/admin仍可回退。回退后按原流程关闭，不再套用观察关闭限制。

观察数据保存在变更的`data.observation`中（`readings`为历次读数，`summary`为异常汇总），`GET /api/entities/<id>`即可查看。演示页面（`/`）支持一键引导至投产、提交读数、查看异常汇总、登记处置、批准回退和查看关闭结果。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。

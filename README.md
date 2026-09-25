# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/overview`：按繁育周期汇总配对建议与占用，首页看板使用此接口。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 配对档期规则

- 配对建议创建时必须带`cycle`（繁育周期）与`venue`（场馆）；`sire_id`/`dam_id`可在批准前补齐。
- 批准（`approve`）时重新核对双方：同一`cycle`内只要任一方已被其他`approved`/`completed`配对占用即拒绝（409），错误信息包含冲突个体名称、ID与占用配对编号；亲本处于`quarantined`（隔离）或`deceased`（死亡）时同样拒绝（400）。被拦截的建议保持`proposed`。
- `rejected`配对不占用档期；已批准但未完成的建议也可以`reject`，驳回后双方档期立即释放，其他待审批建议可以重新批准。
- 批准的「读取占用→规则校验→写入→审计」在单个`BEGIN IMMEDIATE`事务内完成，两个协调员同时批准同一批动物时只有一条成功，另一条收到409且不会重复占用。
- 首页（`/`）按周期展示场馆、已占用个体、每条建议的状态与当前冲突提示。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。

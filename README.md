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

- `animal`：个体谱系（含 `sire_id`/`dam_id` 父母链，可沿多代祖先回溯）；`pairing`：配对建议；`transfer`：机构和运输记录。

## 谱系校正与繁育审批

- 录入员（`registrar`）可通过 `POST /api/entities/<id>/actions` 提交 `{"action":"correct_pedigree","data":{"sire_id":...,"dam_id":...},"expected_version":数字}` 更正父母或祖父母。更正会校验血缘存在、性别、不自交、不成环，并触发重算。
- 近交系数按 Wright 亲缘系数沿多代祖先递归计算：`F = 0.5 * r(sire, dam)`，`r` 为父母间的亲缘系数，自动计入近交祖先的 `(1 + F_A)`，可正确处理自交、回交、全/半同胞、叔侄、表亲等。
- 血统更正后，系统重算所有 `proposed`（待审）和 `approved`（已批准）配对建议：超阈值（`INBREEDING_THRESHOLD = 0.125`）的已批准建议退回 `proposed` 待审；`completed`（已完成）或动物已运输（`in_transit`/`completed`）的保留原判定。
- 审批时按提交那一刻的血统版本再核：若配对记录的父母或近交系数快照与当前血统对不上，返回 `409` 退回重新确认；超阈值的不予批准。
- 两人同时提交同一只动物的校正时，以后到者的 `expected_version` 做乐观锁校验，版本不符返回 `409`；重算始终读取最新已提交血统。
- 服务启动时自动执行 `backfill_coefficients()`，为缺少系数的旧配对数据按现存血统补齐近交系数。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数为标准 Wright 亲缘模型，仍不替代专业谱系软件、遗传咨询或法定动物运输许可。

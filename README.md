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

- `animal`：个体谱系（`sire_id`/`dam_id` 指向父母，`inbreeding_coefficient` 为个体近交系数）；
- `pairing`：配对建议（含父方/母方、近交系数与提交时的血统版本快照）；
- `transfer`：机构和运输记录。

## 谱系校正与繁育审批

- **多代近交系数**：沿完整祖先图按 Wright 共祖系数（kinship）递归计算，不再只看父母一代；阈值为 `0.125`（严格大于才判超阈）。
- **谱系校正**：录入员（`registrar`）或管理员对动物提交 `correct_pedigree`，可更正 `sire_id`/`dam_id`（传 `null` 表示清除）。系统校验父母存在、性别一致（父不能为雌、母不能为雄）、且不会形成祖先环。校正后沿多代祖先重算所有个体的近交系数。
- **配对重算**：校正后，所有 `proposed`/`approved` 的配对按现存血统重算：
  - 系数超阈值的已批准配对**退回待审**（`proposed`，置 `needs_reconfirm`，写入 `return_for_review` 审计）；
  - 阈值内的已批准配对保留原判定；
  - `completed`/`rejected` 的配对以及已关联**在途或已完成运输**的配对保留原判定，不重算。
- **审批版本核对**：配对在创建/重新确认时固化父母双方全部祖先的版本快照（`pedigree_snapshot`）。审批（`approve`）时先按当前血统再核：祖先版本与提交时不一致会返回 409 `PedigreeVersionConflict`，配对留在待审并置 `needs_reconfirm`，需先执行 `reconfirm` 动作按当前血统重新确认，再行批准；版本一致但超阈值则按校验失败退回。
- **并发校正**：所有更新走乐观锁（`expected_version`）。两人同时校正同一只动物时，后到且基于旧版本的提交返回 409，需要读取最新版本后重新提交；重算始终基于提交成功后的最新血统。
- **旧数据升级**：服务首次启动时自动回填旧记录缺失的 `inbreeding_coefficient` 与血统版本快照（仅补缺，不改状态、不升版本），完成后通过 `meta` 表标记，重复启动幂等。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
  - 动物：`correct_pedigree`（data 为 `sire_id`/`dam_id`，可部分提供、可传 `null` 清除）；
  - 配对：`approve`、`reject`、`reconfirm`（版本对不上后按当前血统重新确认）、`complete`；
  - 运输：`authorize`、`ship`、`arrive`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。

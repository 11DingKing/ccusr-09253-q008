# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 跨校联合实训互认

学生在牵头院校与合作院校分别完成实训时，外校事件经签名校验后按互认规则映射到本校培养方案：

- **机构身份**：`POST /api/exchange/institutions` 登记院校代码、时区与共享校验密钥。
- **互认规则**：`POST /api/exchange/rules` 追加不可变规则版本（事件类型映射、可互认活动类型白名单），`POST /api/exchange/rules/{code}/versions/{v}/publish` 生效；批次在接收时绑定规则版本，规则升级只影响新批次。
- **批次接收**：`POST /api/exchange/batches` 校验 HMAC-SHA256 签名后落库；每个 (本校方案, 合作院校) 通道按序号连续入账，乱序批次挂起等待缺口，`POST /api/exchange/recover` 在重启后重新驱动未入账批次（逐笔幂等）。
- **重复与争议**：同一来源事件重复投递自动跳过；同一活动同一时段的外校记录被抑制不重复计时；部分重叠或不在白名单的活动进入争议，裁决前保持待定，`POST /api/exchange/arbitrations` 裁决后重放即生效。
- **对账与解释**：`GET /api/exchange/plans/{plan}/reconcile` 输出序号缺口、挂起批次与开放争议；`GET /api/exchange/sources/{institution}/events/{event_id}` 给出从外校原始事件到本校计时的完整来源链路。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及跨校交换的乱序批次、跨时区映射、重复来源、争议裁决与重启恢复；运行过程中不需要单独的数据库或网络服务。

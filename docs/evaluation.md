# 评估指标文档（Phase 1.7）

每轮 assistant turn 结束后，由 `eval_hooks.TURN_END` 触发五个指标函数，写入 `EvaluationMetric` 表。开关：`ENABLE_EVALUATION`（默认关）。

## 指标定义

| 指标 | metric_name | 计算 | 目标值 |
|---|---|---|---|
| 任务完成率 | `task_completion` | 本轮 outcome ∈ {done, awaiting_user} 记 1，否则 0 | ≥ 0.9 |
| 用户满意度 | `user_satisfaction` | APPLIED / 已决议提案（APPLIED+DISCARDED+STALE）；无决议跳过 | ≥ 0.7 |
| 成本 | `cost` | 本轮 `TaskRecord.cost`（美元） | 单轮 ≤ $0.05 |
| 延迟 | `latency` | `completed_at - started_at` 秒数 | P95 ≤ 30s |
| 工具成功率 | `tool_success_rate` | 1 − is_error 工具结果占比；无工具跳过 | ≥ 0.85 |

## 数据模型

`EvaluationMetric{session, turn_id=TaskRecord.pk, metric_name, metric_value, metadata, created_at}`

- `turn_id` 关联到 `TaskRecord.pk`（不是 seq）— 便于联查成本/trace
- `metadata` 保存细分：`{"outcome": ..., "total": n, "errors": m, "applied": a, "decided": d, "latency_s": x}`

## API

```
GET /api/assistant/evaluation/?play={play_id}&period=7d
→ {"period_days": 7, "since": "...", "metrics": {"task_completion": {"avg": .92, "count": 50}, ...}, "trend": {...}}
```

`period` 支持 `Nd`（如 `30d`）；`play` 限定到某部短剧。

## 周期报告

Celery 任务 `shortplay.tasks.generate_evaluation_report(days=7, play_id=None)`：
聚合 `EvaluationMetric` → 写 `media/evaluation_reports/report_YYYYMMDD_HHMMSS.json`。返回 `{"path", "metrics"}`。PDF 渲染由前端/外部工具消费该 JSON。

## 安全评估

`evaluation.evaluate_injection_defense(handler)`：对 `INJECTION_PROBES` 样本跑会话，判定注入文本是否污染长期记忆。目标防御成功率 ≥ 99%。当前为离线评估函数（需注入真实 runner 做端到端验证）。

## 设计说明

- 单指标函数异常被 `eval_hooks.run_evals` 吞掉（ok=false），不阻断主流程 — 指标记录永远不拖垮轮次
- 指标按 turn 维度落库，聚合在读取时计算（无需预聚合表）
- `register_defaults()` 在 `apps.creation.apps.ready()` 注册，幂等（重复注册会追加，Django 重载时会重复——测试环境注意）

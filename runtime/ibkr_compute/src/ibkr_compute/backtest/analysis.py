from __future__ import annotations

from .runtime_support import *


class BacktestAnalysisMixin:
    def _escape_pb_filter_value(self, raw_value: Any) -> str:
        return str(raw_value or "").replace("\\", "\\\\").replace('"', '\\"')

    def _normalize_symbol_tokens(self, raw_value: Any) -> list[str]:
        items = []
        if isinstance(raw_value, list):
            source = raw_value
        else:
            source = str(raw_value or "").replace("\n", ",").split(",")
        for item in source:
            symbol = str(item or "").strip().upper()
            if not symbol or symbol in items:
                continue
            items.append(symbol)
        return items

    def _compute_symbol_overlap(self, left_symbols: list[str], right_symbols: list[str]) -> dict:
        left_set = {str(item or "").strip().upper() for item in left_symbols if str(item or "").strip()}
        right_set = {str(item or "").strip().upper() for item in right_symbols if str(item or "").strip()}
        if not left_set and not right_set:
            return {"exact_match": True, "overlap_ratio": 1.0, "shared_count": 0}
        if not left_set or not right_set:
            return {"exact_match": False, "overlap_ratio": 0.0, "shared_count": 0}
        shared = left_set & right_set
        union = left_set | right_set
        overlap_ratio = (len(shared) / len(union)) if union else 0.0
        return {
            "exact_match": left_set == right_set,
            "overlap_ratio": round(overlap_ratio, 4),
            "shared_count": len(shared),
        }

    def _coerce_run_for_analysis(self, run: dict) -> dict:
        metrics = self._parse_object(run.get("metrics"))
        extra = self._parse_object(run.get("extra"))
        params = self._parse_object(run.get("params"))
        return {
            "run_id": str(run.get("id") or ""),
            "name": str(run.get("name") or ""),
            "status": str(run.get("status") or ""),
            "symbols": self._normalize_symbol_tokens(
                run.get("symbols") or extra.get("resolved_symbols") or extra.get("requested_symbols") or []
            ),
            "date_from": str(run.get("date_from") or ""),
            "date_to": str(run.get("date_to") or ""),
            "source_environment": str(run.get("source_environment") or ""),
            "symbol_source": str(run.get("symbol_source") or ""),
            "session_mode": str(run.get("session_mode") or ""),
            "trade_count": int(run.get("trade_count", metrics.get("trade_count", 0)) or 0),
            "net_pnl": float(run.get("net_pnl", metrics.get("net_pnl", 0)) or 0),
            "total_return_pct": float(run.get("total_return_pct", metrics.get("total_return_pct", 0)) or 0),
            "sharpe": float(run.get("sharpe", metrics.get("sharpe", 0)) or 0),
            "max_drawdown_pct": float(run.get("max_drawdown_pct", metrics.get("max_drawdown_pct", 0)) or 0),
            "win_rate": float(run.get("win_rate", metrics.get("win_rate", 0)) or 0),
            "signal_fill_rate": float(metrics.get("signal_fill_rate", 0) or 0),
            "strategy_tag": str(extra.get("strategy_tag") or ""),
            "strategy_params": deepcopy(params.get("strategy_params") or {}),
            "batch_id": str(extra.get("batch_id") or ""),
            "created": str(run.get("created") or ""),
            "metrics": metrics,
            "extra": extra,
        }

    def _load_comparable_completed_runs(
        self,
        request: dict,
        exclude_run_ids: set[str] | None = None,
        exclude_batch_ids: set[str] | None = None,
        max_pages: int = 12,
    ) -> list[dict]:
        if not self.pb:
            return []
        exclude_ids = {str(item or "").strip() for item in (exclude_run_ids or set()) if str(item or "").strip()}
        exclude_batches = {str(item or "").strip() for item in (exclude_batch_ids or set()) if str(item or "").strip()}
        filter_text = (
            f'status = "completed" && '
            f'source_environment = "{self._escape_pb_filter_value(request.get("source_environment"))}" && '
            f'symbol_source = "{self._escape_pb_filter_value(request.get("symbol_source"))}" && '
            f'date_from = "{self._escape_pb_filter_value(request.get("date_from"))}" && '
            f'date_to = "{self._escape_pb_filter_value(request.get("date_to"))}" && '
            f'session_mode = "{self._escape_pb_filter_value(request.get("session_mode"))}"'
        )
        rows = self.pb.get_all_records(
            RUN_COLLECTION,
            filter=filter_text,
            sort="-created",
            max_pages=max_pages,
        )
        items = []
        for row in rows:
            candidate = self._coerce_run_for_analysis(row)
            candidate_run_id = candidate["run_id"]
            if not candidate_run_id or candidate_run_id in exclude_ids:
                continue
            if candidate["batch_id"] and candidate["batch_id"] in exclude_batches:
                continue
            items.append(candidate)
        return items

    def _pick_best_comparable_run(
        self,
        current_symbols: list[str],
        current_strategy_tag: str,
        candidates: list[dict],
    ) -> dict | None:
        if not candidates:
            return None
        scored = []
        safe_strategy_tag = str(current_strategy_tag or "").strip()
        for item in candidates:
            overlap = self._compute_symbol_overlap(current_symbols, item.get("symbols") or [])
            similarity_score = 0
            if overlap["exact_match"]:
                similarity_score += 3
            elif overlap["overlap_ratio"] >= 0.6:
                similarity_score += 2
            elif overlap["overlap_ratio"] > 0:
                similarity_score += 1
            if safe_strategy_tag and safe_strategy_tag == str(item.get("strategy_tag") or "").strip():
                similarity_score += 1
            scored.append(
                (
                    similarity_score,
                    float(overlap["overlap_ratio"]),
                    float(item.get("total_return_pct", 0) or 0),
                    float(item.get("sharpe", 0) or 0),
                    -float(item.get("max_drawdown_pct", 0) or 0),
                    item,
                    overlap,
                )
            )
        scored.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]), reverse=True)
        if not scored:
            return None
        best = dict(scored[0][5])
        best["scope_similarity"] = {
            "score": scored[0][0],
            **scored[0][6],
        }
        return best

    def _build_strategy_param_delta(self, current_params: dict, reference_params: dict, limit: int = 4) -> list[dict]:
        diffs = []
        current = current_params or {}
        reference = reference_params or {}
        for key in sorted(set(current.keys()) | set(reference.keys())):
            current_value = current.get(key)
            reference_value = reference.get(key)
            if current_value == reference_value:
                continue
            diffs.append(
                {
                    "key": key,
                    "current": current_value,
                    "reference": reference_value,
                }
            )
            if len(diffs) >= max(1, int(limit or 0)):
                break
        return diffs

    def _build_backtest_recommendations(self, request: dict, metrics: dict, tv_summary: dict, comparison: dict | None = None) -> dict:
        strengths = []
        risks = []
        suggestions = []

        total_return_pct = float(metrics.get("total_return_pct", 0) or 0)
        sharpe = float(metrics.get("sharpe", 0) or 0)
        win_rate = float(metrics.get("win_rate", 0) or 0)
        signal_fill_rate = float(metrics.get("signal_fill_rate", 0) or 0)
        max_drawdown_pct = float(metrics.get("max_drawdown_pct", 0) or 0)
        profit_factor = float(metrics.get("profit_factor", 0) or 0)
        win_loss_ratio = float(metrics.get("win_loss_ratio", 0) or 0)
        reverse_count = int(metrics.get("backtest_reverse_signal_count", 0) or 0)
        trade_count = int(metrics.get("trade_count", 0) or 0)
        target_count = int(metrics.get("backtest_target_count", 0) or 0)
        tv_status = str((tv_summary or {}).get("status") or "disabled")
        historical_targeting = metrics.get("historical_targeting") or {}
        reverse_actions = metrics.get("backtest_reverse_action_breakdown") or {}
        reverse_cancel = int(reverse_actions.get("cancel", 0) or 0)
        reverse_adjust = int(reverse_actions.get("adjust_sl", 0) or 0) + int(reverse_actions.get("adjust_tp", 0) or 0)

        score = 0
        if total_return_pct > 0:
            score += 2
            strengths.append("收益为正，当前参数组合具备继续迭代价值。")
        else:
            risks.append("总收益为负，本期参数/筛股组合没有跑出有效优势。")
        if sharpe >= 1:
            score += 2
            strengths.append("Sharpe 大于等于 1，风险调整后的回报相对稳定。")
        elif sharpe > 0:
            score += 1
        else:
            risks.append("Sharpe 偏弱，说明收益波动质量还不够稳。")
        if win_rate >= 55:
            score += 1
            strengths.append("胜率超过 55%，信号方向判断整体不差。")
        if signal_fill_rate >= 50:
            score += 1
            strengths.append("信号成交率较高，入场链路摩擦可控。")
        elif trade_count > 0:
            risks.append("信号成交率偏低，很多信号没有转成实际成交。")
        if max_drawdown_pct <= 2:
            score += 1
        elif max_drawdown_pct >= max(3.0, abs(total_return_pct) * 1.5):
            risks.append("回撤相对收益偏大，说明风控与筛股还需要继续收紧。")
        if tv_status in {"warn", "fail", "error"}:
            risks.append(f"TV 对齐状态为 {tv_status}，分析结论仍需先建立在 bars 正确性之上。")
            score -= 1
        if request.get("symbol_source") == "daily_scan_replay" and target_count > 0:
            strengths.append(f"历史盘前选股已成功回放，共覆盖 {historical_targeting.get('target_date_count', 0)} 个交易日。")
        if reverse_count > max(0, trade_count):
            risks.append("反转动作次数多于成交笔数，说明冲突阈值可能偏敏感。")

        if signal_fill_rate < 35:
            suggestions.append("优先提高信号成交率：复查入场条件、滑点假设、盘前 cutoff 和 session_mode。")
        daily_scan_match = metrics.get("daily_scan_match_diagnostics") or {}
        if request.get("symbol_source") == "daily_scan_replay" and int(daily_scan_match.get("generated_signal_count", 0) or 0) > 0:
            selected_day_signal_rate = float(daily_scan_match.get("selected_day_signal_rate_pct", 0) or 0)
            if selected_day_signal_rate < 25:
                risks.append("9:20 日筛入选标的与后续日内信号重合度偏低，开仓数量会被 selection plan 明显压缩。")
                suggestions.append("优先调 daily_scan_replay 命中率：扩大 max_symbols，或放宽盘前成交量/涨跌幅/ATR%门槛后做对照回测。")
        if total_return_pct <= 0 or max_drawdown_pct >= max(3.0, abs(total_return_pct) * 1.5):
            suggestions.append("优先收紧风险：减少弱分数标的、降低 max_symbols，或提高止损纪律。")
        if tv_status in {"warn", "fail", "error"}:
            suggestions.append("先消除 TV 对齐漂移，再解读策略优劣，避免把数据问题当作策略问题。")
        if reverse_cancel >= max(2, reverse_adjust + 1):
            suggestions.append("反转动作以 cancel 为主，建议复查 indicator_conflict 的弱冲突阈值，避免过早放弃仓位。")
        if request.get("symbol_source") == "daily_scan_replay" and target_count <= 1:
            suggestions.append("历史盘前标的过少，建议扩大回放底池，或放宽 premarket cutoff 前的准备窗口。")
        if win_rate >= 55 and win_loss_ratio < 1:
            suggestions.append("胜率尚可但盈亏比不足，建议复查 rr_ratio / 止盈参数，避免过早止盈。")
        if win_rate < 45 and profit_factor <= 1:
            suggestions.append("胜率和 profit factor 同时偏弱，建议优先缩减入场频率而不是放大仓位。")

        if comparison and comparison.get("improved_vs_baseline"):
            strengths.append("相对同周期历史基准已有明显提升，可作为当前优先候选配置。")
            param_delta = comparison.get("strategy_param_delta") or []
            if param_delta:
                changed_keys = ", ".join(str(item.get("key") or "") for item in param_delta if str(item.get("key") or ""))
                if changed_keys:
                    suggestions.append(f"可优先复核这些参数变化是否稳定有效：{changed_keys}。")
        elif comparison and comparison.get("baseline_exists"):
            risks.append("与同周期历史基准相比尚未形成显著优势，暂不建议直接替换当前候选方案。")

        verdict = "strong" if score >= 5 else ("mixed" if score >= 2 else "weak")
        if verdict == "strong":
            headline = "本期回测表现偏强，收益、风险和成交结构整体可接受。"
        elif verdict == "mixed":
            headline = "本期回测有可用信号，但还存在明确的优化空间。"
        else:
            headline = "本期回测暂未形成稳定优势，优先排查筛股、风险和对齐质量。"

        deduped_suggestions = []
        seen_suggestions = set()
        for item in suggestions:
            if item in seen_suggestions:
                continue
            seen_suggestions.add(item)
            deduped_suggestions.append(item)

        return {
            "verdict": verdict,
            "headline": headline,
            "strengths": strengths[:6],
            "risks": risks[:6],
            "suggestions": deduped_suggestions[:6],
        }

    def _build_run_analysis_report(
        self,
        run_id: str,
        request: dict,
        symbols: list[str],
        metrics: dict,
        tv_parity: dict,
        historical_targeting: dict | None = None,
    ) -> dict:
        current_strategy_tag = str(request.get("strategy_tag") or "").strip()
        current_params = deepcopy((request.get("params") or {}).get("strategy_params") or {})
        batch_id = str(request.get("batch_id") or "").strip()
        candidates = self._load_comparable_completed_runs(
            request,
            exclude_run_ids={run_id},
            exclude_batch_ids={batch_id} if batch_id else set(),
        )
        baseline = self._pick_best_comparable_run(symbols, current_strategy_tag, candidates)
        comparison = {
            "baseline_exists": bool(baseline),
            "improved_vs_baseline": False,
        }
        if baseline:
            delta_return = round(float(metrics.get("total_return_pct", 0) or 0) - float(baseline.get("total_return_pct", 0) or 0), 4)
            delta_sharpe = round(float(metrics.get("sharpe", 0) or 0) - float(baseline.get("sharpe", 0) or 0), 4)
            delta_win_rate = round(float(metrics.get("win_rate", 0) or 0) - float(baseline.get("win_rate", 0) or 0), 4)
            improved = (
                delta_return >= BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD
                or (
                    delta_return >= 0
                    and delta_sharpe >= BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD
                )
            )
            comparison.update(
                {
                    "baseline_run_id": baseline.get("run_id") or "",
                    "baseline_name": baseline.get("name") or "",
                    "baseline_total_return_pct": float(baseline.get("total_return_pct", 0) or 0),
                    "baseline_sharpe": float(baseline.get("sharpe", 0) or 0),
                    "baseline_win_rate": float(baseline.get("win_rate", 0) or 0),
                    "delta_return_pct": delta_return,
                    "delta_sharpe": delta_sharpe,
                    "delta_win_rate": delta_win_rate,
                    "scope_similarity": baseline.get("scope_similarity") or {},
                    "strategy_param_delta": self._build_strategy_param_delta(
                        current_params,
                        deepcopy(baseline.get("strategy_params") or {}),
                    ),
                    "improved_vs_baseline": bool(improved),
                }
            )
        recommendation = self._build_backtest_recommendations(
            request,
            metrics,
            (tv_parity or {}).get("summary") or {},
            comparison=comparison,
        )
        report = {
            "run_id": run_id,
            "baseline_comparison": comparison,
            "recommendation": recommendation,
            "historical_targeting": historical_targeting or {},
            "notification": {
                "should_notify": bool(comparison.get("improved_vs_baseline")),
                "reason": "better_same_period" if comparison.get("improved_vs_baseline") else "",
            },
        }
        report["summary"] = {
            "verdict": recommendation["verdict"],
            "headline": recommendation["headline"],
            "improved_vs_baseline": bool(comparison.get("improved_vs_baseline")),
            "baseline_run_id": comparison.get("baseline_run_id") or "",
            "delta_return_pct": float(comparison.get("delta_return_pct", 0) or 0),
            "delta_sharpe": float(comparison.get("delta_sharpe", 0) or 0),
        }
        return report

    def _notify_backtest_improvement(self, title: str, detail: dict) -> dict:
        if not self.pb:
            return {"ok": False, "notified": False, "error": "pb_client_unavailable"}
        try:
            result = self.pb.notify_ibkr_event(
                title=title,
                detail=detail,
                notify_type="status",
                environment=BACKTEST_ENVIRONMENT,
            )
            return {
                "ok": True,
                "notified": bool(result.get("ok", True)),
            }
        except Exception as exc:
            traceback.print_exc()
            return {
                "ok": False,
                "notified": False,
                "error": str(exc)[:300],
            }

    def _build_batch_experiment_analysis(
        self,
        batch_id: str,
        request: dict,
        summaries: list[dict],
    ) -> dict:
        completed = [item for item in summaries if item.get("status") == "completed"]
        sorted_completed = sorted(
            completed,
            key=lambda item: (
                -float(item.get("total_return_pct", 0) or 0),
                -float(item.get("sharpe", 0) or 0),
                float(item.get("max_drawdown_pct", 0) or 0),
            ),
        )
        best = sorted_completed[0] if sorted_completed else {}
        second = sorted_completed[1] if len(sorted_completed) > 1 else {}
        exclude_run_ids = {str(item.get("run_id") or "") for item in summaries if str(item.get("run_id") or "")}
        historical_candidates = self._load_comparable_completed_runs(
            request,
            exclude_run_ids=exclude_run_ids,
            exclude_batch_ids={batch_id} if batch_id else set(),
        )
        historical_best = self._pick_best_comparable_run(
            self._normalize_symbol_tokens(request.get("symbols") or []),
            str(best.get("strategy_tag") or request.get("strategy_tag") or ""),
            historical_candidates,
        )

        comparison = {
            "best_run_id": best.get("run_id") or "",
            "best_variant_label": best.get("variant_label") or "",
            "variant_count": len(completed),
            "historical_baseline_run_id": historical_best.get("run_id") if historical_best else "",
            "improved_vs_historical": False,
        }
        if best and second:
            comparison["delta_vs_second_best_return_pct"] = round(
                float(best.get("total_return_pct", 0) or 0) - float(second.get("total_return_pct", 0) or 0),
                4,
            )
            comparison["delta_vs_second_best_sharpe"] = round(
                float(best.get("sharpe", 0) or 0) - float(second.get("sharpe", 0) or 0),
                4,
            )
            comparison["strategy_param_delta_vs_second_best"] = self._build_strategy_param_delta(
                deepcopy(best.get("strategy_params") or {}),
                deepcopy(second.get("strategy_params") or {}),
            )
        if best and historical_best:
            delta_return = round(
                float(best.get("total_return_pct", 0) or 0) - float(historical_best.get("total_return_pct", 0) or 0),
                4,
            )
            delta_sharpe = round(
                float(best.get("sharpe", 0) or 0) - float(historical_best.get("sharpe", 0) or 0),
                4,
            )
            improved = (
                delta_return >= BACKTEST_IMPROVEMENT_NOTIFY_THRESHOLD
                or (delta_return >= 0 and delta_sharpe >= BACKTEST_IMPROVEMENT_SHARPE_THRESHOLD)
            )
            comparison.update(
                {
                    "historical_baseline_name": historical_best.get("name") or "",
                    "historical_baseline_return_pct": float(historical_best.get("total_return_pct", 0) or 0),
                    "historical_baseline_sharpe": float(historical_best.get("sharpe", 0) or 0),
                    "delta_vs_historical_return_pct": delta_return,
                    "delta_vs_historical_sharpe": delta_sharpe,
                    "improved_vs_historical": bool(improved),
                    "strategy_param_delta_vs_historical": self._build_strategy_param_delta(
                        deepcopy(best.get("strategy_params") or {}),
                        deepcopy(historical_best.get("strategy_params") or {}),
                    ),
                    "scope_similarity": historical_best.get("scope_similarity") or {},
                }
            )

        suggestions = []
        if comparison.get("strategy_param_delta_vs_second_best"):
            keys = ", ".join(
                str(item.get("key") or "")
                for item in comparison["strategy_param_delta_vs_second_best"]
                if str(item.get("key") or "")
            )
            if keys:
                suggestions.append(f"优先复核最佳变体相对次优变体的参数差异：{keys}。")
        if comparison.get("improved_vs_historical"):
            suggestions.append("最佳变体已优于同周期历史基准，可考虑作为当前候选默认参数继续前推。")
        elif best:
            suggestions.append("本次 experiment 形成了内部最优参数，但相对历史同周期基准还未拉开明显优势。")
        if not best:
            suggestions.append("本次 experiment 没有完成的变体结果，先检查参数输入或数据范围。")

        headline = "本次参数扫描已形成最佳变体。" if best else "本次参数扫描尚未形成可用结果。"
        if comparison.get("improved_vs_historical"):
            headline = "本次参数扫描找到优于同周期历史基准的最佳变体。"

        return {
            "summary": {
                "headline": headline,
                "best_run_id": best.get("run_id") or "",
                "best_variant_label": best.get("variant_label") or "",
                "improved_vs_historical": bool(comparison.get("improved_vs_historical")),
                "delta_vs_historical_return_pct": float(comparison.get("delta_vs_historical_return_pct", 0) or 0),
                "delta_vs_second_best_return_pct": float(comparison.get("delta_vs_second_best_return_pct", 0) or 0),
            },
            "comparison": comparison,
            "suggestions": suggestions[:6],
            "notification": {
                "should_notify": bool(comparison.get("improved_vs_historical")),
                "reason": "better_same_period" if comparison.get("improved_vs_historical") else "",
            },
        }

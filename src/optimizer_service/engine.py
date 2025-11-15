"""Core optimization search logic for optimizer service."""

from __future__ import annotations

import hashlib
import itertools
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from .schemas import OptimizationAlgorithm, OptimizationRequest, OptimizationResultItem


@dataclass
class ExecutionOutcome:
    success: bool
    parameters: Dict[str, Any]
    result: OptimizationResultItem | None = None
    fail_reason: str | None = None


class OptimizationEngine:
    """Executes optimization according to requested algorithm.

    The real trading backtest is not yet integrated; we use deterministic pseudo metrics
    so that routing and reporting layers can be exercised during development.
    """

    def __init__(self, request: OptimizationRequest) -> None:
        self.request = request
        self._rng = random.Random(self._seed_from_request(request))

    def run(self) -> Iterator[ExecutionOutcome]:
        if self.request.algorithm == OptimizationAlgorithm.GRID:
            yield from self._run_grid()
        elif self.request.algorithm == OptimizationAlgorithm.RANDOM:
            yield from self._run_random()
        else:
            yield from self._run_bayesian()

    def estimated_total(self) -> int:
        if self.request.algorithm == OptimizationAlgorithm.GRID:
            total = 1
            for values in self.request.parameters.values():
                total *= len(values)
            return total
        iterations = self.request.iterations or 10
        return max(iterations, 1)

    # Internal helpers -------------------------------------------------
    def _run_grid(self) -> Iterator[ExecutionOutcome]:
        param_names = list(self.request.parameters.keys())
        value_lists = [self.request.parameters[name] for name in param_names]
        for combo_values in itertools.product(*value_lists):
            combo = {name: value for name, value in zip(param_names, combo_values)}
            yield self._evaluate_combo(combo)

    def _run_random(self) -> Iterator[ExecutionOutcome]:
        iterations = self.request.iterations or 10
        param_names = list(self.request.parameters.keys())
        value_lists = [self.request.parameters[name] for name in param_names]
        for _ in range(iterations):
            combo = {name: self._rng.choice(values) for name, values in zip(param_names, value_lists)}
            yield self._evaluate_combo(combo)

    def _run_bayesian(self) -> Iterator[ExecutionOutcome]:
        # Placeholder: start with random exploration then exploit best so far by tweaking one parameter
        iterations = self.request.iterations or 10
        param_names = list(self.request.parameters.keys())
        best_combo: Dict[str, Any] | None = None
        best_score = -math.inf
        for idx in range(iterations):
            if idx < max(2, len(param_names)) or best_combo is None:
                combo = {name: self._rng.choice(values) for name, values in self.request.parameters.items()}
            else:
                combo = best_combo.copy()
                target_param = self._rng.choice(param_names)
                options = self.request.parameters[target_param]
                combo[target_param] = self._rng.choice(options)
            outcome = self._evaluate_combo(combo)
            if outcome.success and outcome.result:
                score = outcome.result.metrics.get("sharpe", 0.0)
                if score > best_score:
                    best_score = score
                    best_combo = combo
            yield outcome

    def _evaluate_combo(self, combo: Dict[str, Any]) -> ExecutionOutcome:
        try:
            metrics = self._simulate_backtest(combo)
            result = OptimizationResultItem(parameters=combo, metrics=metrics)
            return ExecutionOutcome(success=True, parameters=combo, result=result)
        except RuntimeError as exc:
            return ExecutionOutcome(success=False, parameters=combo, fail_reason=str(exc))

    def _simulate_backtest(self, combo: Dict[str, Any]) -> Dict[str, float]:
        seed = self._seed_from_combo(combo)
        rng = random.Random(seed)
        failure_probability = 0.08
        if rng.random() < failure_probability:
            raise RuntimeError("信号数量不足")
        annual_return = round(0.05 + rng.random() * 0.25, 4)
        sharpe = round(0.8 + rng.random() * 1.5, 3)
        max_drawdown = round(0.05 + rng.random() * 0.2, 4)
        win_rate = round(0.45 + rng.random() * 0.35, 3)
        avg_pnl = round(-2 + rng.random() * 12, 3)
        return {
            "annual_return": annual_return,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
        }

    def _seed_from_combo(self, combo: Dict[str, Any]) -> int:
        serial = f"{self.request.strategy}|" + "|".join(f"{k}={combo[k]}" for k in sorted(combo))
        digest = hashlib.sha256(serial.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)

    def _seed_from_request(self, request: OptimizationRequest) -> int:
        base = request.strategy + request.start_date.isoformat() + request.end_date.isoformat()
        digest = hashlib.sha256(base.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)

import os
os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

from datetime import date
from typing import Optional

from src.optimizer_service.engine import OptimizationEngine
from src.optimizer_service.schemas import OptimizationAlgorithm, OptimizationRequest


def make_request(algorithm: OptimizationAlgorithm, iterations: Optional[int] = None) -> OptimizationRequest:
    return OptimizationRequest(
        strategy="engine_test",
        start_date=date(2023, 1, 1),
        end_date=date(2023, 1, 10),
        parameters={"window": [5, 10], "threshold": [0.1, 0.2, 0.3]},
        algorithm=algorithm,
        iterations=iterations,
    )


def test_grid_engine_visits_full_cartesian():
    request = make_request(OptimizationAlgorithm.GRID)
    engine = OptimizationEngine(request)
    outcomes = list(engine.run())
    expected_total = len(request.parameters["window"]) * len(request.parameters["threshold"])
    assert len(outcomes) == expected_total


def test_random_engine_uses_iteration_count():
    iterations = 7
    request = make_request(OptimizationAlgorithm.RANDOM, iterations=iterations)
    engine = OptimizationEngine(request)
    outcomes = list(engine.run())
    assert len(outcomes) == iterations


def test_bayesian_engine_respects_iteration_count():
    iterations = 5
    request = make_request(OptimizationAlgorithm.BAYESIAN, iterations=iterations)
    engine = OptimizationEngine(request)
    outcomes = list(engine.run())
    assert len(outcomes) == iterations

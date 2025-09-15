import pytest

from src.dom_service import DOMService
from src.dom_processor import DOMProcessor


def test_get_metrics_only_trend_score():
    service = DOMService()
    service.processor = DOMProcessor()
    # Manually set metrics and signals to ensure only metrics are exposed
    service.processor.last_metrics = {"trend_score": 0.42, "obi5": 0.5}
    service.processor.last_signals = {"long": True, "short": False}
    metrics = service.get_metrics()
    assert "trend_score" in metrics
    assert "long" not in metrics and "short" not in metrics

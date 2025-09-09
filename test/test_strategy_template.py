import importlib.util
from src.strategy_manager import StrategyManager


def test_local_strategy_template_generation(tmp_path):
    manager = StrategyManager(data_dir=tmp_path / "data", strategy_dir=tmp_path / "strategies")
    manager._create_strategy_template("demo")
    strategy_file = manager.strategy_dir / "demo.py"
    assert strategy_file.exists()
    # Ensure only one __init__ definition
    content = strategy_file.read_text()
    assert content.count("def __init__(self)") == 1
    # Try importing the generated strategy to verify syntax
    spec = importlib.util.spec_from_file_location("demo_strategy", strategy_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "strategy")
    assert module.strategy.name == "demo"
    assert module.strategy.description

from src.data_layer import (
    DataLayerMode,
    get_data_layer_mode,
    is_dual_mode,
    is_legacy_mode,
    is_unified_mode,
)


def test_get_data_layer_mode_defaults_to_legacy():
    mode = get_data_layer_mode(env={})
    assert mode is DataLayerMode.LEGACY


def test_get_data_layer_mode_respects_env_case_insensitive():
    mode = get_data_layer_mode(env={"DATA_LAYER_MODE": "UnIfIeD"})
    assert mode is DataLayerMode.UNIFIED


def test_mode_helpers():
    assert is_legacy_mode(DataLayerMode.LEGACY)
    assert is_unified_mode(DataLayerMode.UNIFIED)
    assert is_dual_mode(DataLayerMode.DUAL)


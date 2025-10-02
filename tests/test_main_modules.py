import asyncio

import main


class _DummyLogger:
    def __init__(self) -> None:
        self.records = []

    def info(self, *args, **kwargs):
        self.records.append(("info", args, kwargs))

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args, kwargs))


def test_data_subscriptions_skips_dom_autostart_when_flag_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_DOM_AUTO_START", "0")
    monkeypatch.setattr(main, "get_data_source_manager", lambda: None)

    dom_calls = []

    def _fail_dom_resolution():
        dom_calls.append(True)
        raise AssertionError("DOM service should not be resolved when auto-start is disabled")

    monkeypatch.setattr(main.core, "get_dom_service", _fail_dom_resolution, raising=False)

    context = main.ModuleRuntimeContext(
        app=None,
        module_name="data_subscriptions",
        logger=_DummyLogger(),
        root_logger=_DummyLogger(),
    )

    handle = asyncio.run(main._module_data_subscriptions(context))

    assert isinstance(handle, main.ModuleHandle)
    assert not dom_calls

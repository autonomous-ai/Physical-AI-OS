"""Local intent delivery must not repeat a relative adjustment after disconnect."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import requests
from hal.drivers.voice._internal import sensing_sender as module

@pytest.mark.parametrize("kind", ["voice", "voice_command", "voice_followup"])
def test_voice_budget_and_uncertain_delivery(monkeypatch, kind):
    post = Mock(side_effect=requests.ConnectionError("lost response"))
    monkeypatch.setattr(module.requests, "post", post)
    result = module.SensingSender().send("dim the light", event_type=kind, skip_echo=True)
    assert not result
    post.assert_called_once()
    assert post.call_args.kwargs["timeout"] == 30

@pytest.mark.parametrize("harness,image,want", [(True,None,5),(False,None,30),(False,"image",90)])
def test_route_budgets(monkeypatch,harness,image,want):
    post=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:{"data":{"handledLocally":"true"}}))
    monkeypatch.setattr(module.requests,"post",post)
    result=module.SensingSender().send("dim the light",event_type="voice_command",skip_echo=True,
        image_b64=image,harness_voice={"enabled":True,"generation":2} if harness else None)
    assert result.handled_locally
    assert post.call_args.kwargs["timeout"]==want

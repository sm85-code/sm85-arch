import json
import pytest
import httpx
from tenants.marketplace_erp.modules.marketplace_erp.application.assistant import openai_provider


@pytest.mark.asyncio
async def test_responses_contract_reasoning_usage_and_tool_roundtrip(monkeypatch):
    reasoning = {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "test-only"}
    data = {"status": "completed", "usage": {"input_tokens": 25, "output_tokens": 90, "output_tokens_details": {"reasoning_tokens": 80}}, "output": [reasoning, {"type": "function_call", "call_id": "c1", "name": "daftar_toko", "arguments": "{}"}]}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(openai_provider.httpx, 'AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    response = await openai_provider.call('gpt-6.1-sol', [{'role': 'user', 'content': 'Baca toko'}], [], 'Sistem')
    assert response.usage.output_tokens == 90  # Includes reasoning cost.
    content = [b.model_dump() for b in response.content]
    inputs = openai_provider.response_input([{'role': 'assistant', 'content': content}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1', 'content': '[]'}]}])
    assert inputs == [reasoning, {'type': 'function_call', 'call_id': 'c1', 'name': 'daftar_toko', 'arguments': '{}'}, {'type': 'function_call_output', 'call_id': 'c1', 'output': '[]'}]
    payload = json.loads(requests[0].content)
    assert payload['store'] is False and payload['reasoning']['effort'] == 'low'
    assert requests[0].url == 'https://api.openai.com/v1/responses'
    data['status'] = 'incomplete'
    assert openai_provider.normalize(data).stop_reason == 'max_tokens'
    data['output'][1]['arguments'] = '{bad json'
    with pytest.raises(ValueError):
        openai_provider.normalize(data)

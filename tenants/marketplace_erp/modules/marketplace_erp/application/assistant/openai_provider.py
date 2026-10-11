"""Responses API adapter; stateless conversation and no automatic retries."""
import json
import os
from types import SimpleNamespace
import httpx
from . import config


class Block:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **kwargs):
        return self.value


def response_input(messages):
    items = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            items.append({"role": message["role"], "content": content})
            continue
        for block in content:
            kind = block["type"]
            if kind == "text":
                items.append({"role": message["role"], "content": block["text"]})
            elif kind == "tool_use":
                items.append({"type": "function_call", "call_id": block["id"], "name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False)})
            elif kind == "tool_result":
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": block["content"]})
            elif kind == "openai_reasoning":
                # Replay encrypted reasoning only within this transient tool loop.
                items.append(block["item"])
    return items


def normalize(data):
    usage = data["usage"]
    for key in ("input_tokens", "output_tokens"):
        if type(usage[key]) is not int or usage[key] < 0:
            raise ValueError("Invalid provider usage")
    content = []
    for item in data.get("output", []):
        if item["type"] == "message":
            for part in item.get("content", []):
                if part["type"] in ("output_text", "refusal"):
                    content.append(Block({"type": "text", "text": part.get("text", part.get("refusal", ""))}))
        elif item["type"] == "function_call":
            # Invalid JSON stops the turn before any proposed action is processed.
            content.append(Block({"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": json.loads(item["arguments"])}))
        elif item["type"] == "reasoning":
            content.append(Block({"type": "openai_reasoning", "item": item}))
    status = data.get("status")
    if status not in ("completed", "incomplete") or data.get("error"):
        raise ValueError("Responses API did not complete")
    return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]), stop_reason="max_tokens" if status == "incomplete" else "end_turn")


async def call(model, messages, definitions, system):
    payload = {"model": model, "instructions": system, "input": response_input(messages),
               "tools": [{"type": "function", "name": d["name"], "description": d["description"], "parameters": d["input_schema"], "strict": False} for d in definitions],
               "max_output_tokens": config.MAX_OUTPUT, "reasoning": {"effort": "low"},
               "store": False, "include": ["reasoning.encrypted_content"], "parallel_tool_calls": False}
    async with httpx.AsyncClient(timeout=40) as client:
        response = await client.post("https://api.openai.com/v1/responses", headers={"Authorization": "Bearer " + os.getenv("OPENAI_API_KEY", "")}, json=payload)
        # Never include request headers or provider error bodies in ERP replies/logs.
        if response.status_code != 200:
            raise RuntimeError(f"OpenAI request rejected (HTTP {response.status_code})")
        return normalize(response.json())

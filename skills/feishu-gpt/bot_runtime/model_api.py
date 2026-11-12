"""Model API routing and compatibility with the agent's Chat message loop."""

from types import SimpleNamespace


def _uses_responses(model: str) -> bool:
    return any(model == name or model.startswith(name + "-") for name in ("gpt-6.1-sol", "gpt-6-astra"))


def _responses_input(messages: list[dict]) -> list[dict]:
    items = []
    for message in messages:
        if "responses_output" in message:
            # Replay all output items, including encrypted reasoning, before tool results.
            items.extend(message["responses_output"])
            continue
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            parts = []
            for part in content:
                if part["type"] == "text":
                    parts.append({"type": "input_text", "text": part["text"]})
                elif part["type"] == "image_url":
                    image = part["image_url"]
                    parts.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
                else:
                    raise ValueError(f"Unsupported message content type: {part['type']}")
            content = parts
        if content:
            items.append({"role": message["role"], "content": content})
        for call in message.get("tool_calls", []):
            items.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    return items


def create_model_completion(client, model: str, messages: list[dict], tools: list | None, *, effort: str | None = None):
    if not _uses_responses(model):
        kwargs = {"model": model, "messages": messages}
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        return client.chat.completions.create(**kwargs)

    kwargs = {
        "model": model,
        "input": _responses_input(messages),
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if effort is not None:
        kwargs["reasoning"] = {"effort": effort}
    if tools:
        kwargs["tools"] = [{"type": "function", **tool["function"], "strict": tool["function"].get("strict", False)} for tool in tools]
        kwargs["tool_choice"] = "auto"
    response = client.responses.create(**kwargs)
    if response.status != "completed":
        raise RuntimeError(f"Responses API status: {response.status}; error: {response.error}; details: {response.incomplete_details}")

    output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
    tool_calls = []
    text_parts = []
    for item in output:
        if item["type"] == "function_call":
            tool_calls.append(SimpleNamespace(id=item["call_id"], function=SimpleNamespace(name=item["name"], arguments=item["arguments"])))
        elif item["type"] == "message":
            for part in item["content"]:
                if part["type"] == "output_text":
                    text_parts.append(part["text"])
                elif part["type"] == "refusal":
                    text_parts.append(part["refusal"])
    message = SimpleNamespace(content="\n".join(text_parts), tool_calls=tool_calls, responses_output=output)
    return SimpleNamespace(model=response.model, choices=[SimpleNamespace(message=message)],
                           reasoning=getattr(response, "reasoning", None),
                           service_tier=getattr(response, "service_tier", None))

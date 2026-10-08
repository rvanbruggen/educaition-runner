"""LLM provider abstraction — the same idea as Positron's admin/lib/llm.ts.

Each pipeline step ("classify", "write", "digest") has its own setting of the
form "<provider>:<model>", e.g. "openai:gpt-6-luna" or "anthropic:claude-sonnet-5".
Settings live in the database (editable on /settings) with environment defaults,
and are read at call time, so a switch applies to the next call without a restart.

The model only ever answers with a JSON object; the pipeline renders files itself.
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import db

log = logging.getLogger("llm")

STEPS = {
    "classify": "Relevantie + tags per kandidaat (veel calls, klein)",
    "write": "Titel + samenvatting per artikel",
    "digest": "Wekelijkse digests en duiding (fase 2-3)",
}
DEFAULT_SPEC = "openai:gpt-6-luna"
PROVIDERS = ("openai", "anthropic")

# USD per 1M tokens (input, output). Used for the cost column only — check the
# providers' pricing pages; unknown models are logged at $0 with their token counts.
PRICES = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-astra": (10.00, 50.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}


@dataclass
class LLMResult:
    data: dict
    model: str
    tokens_in: int
    tokens_out: int
    cost: float


def get_spec(step: str) -> tuple[str, str]:
    env_default = os.environ.get(f"LLM_{step.upper()}", DEFAULT_SPEC)
    spec = db.get_setting(f"llm_{step}", env_default) or env_default
    provider, _, model = spec.partition(":")
    if provider not in PROVIDERS or not model:
        log.warning("Ongeldige LLM-instelling %r voor %s; terugval op %s", spec, step, DEFAULT_SPEC)
        provider, _, model = DEFAULT_SPEC.partition(":")
    return provider, model


def _price(model: str, tin: int, tout: int) -> float:
    for prefix, (pin, pout) in PRICES.items():
        if model.startswith(prefix):
            return tin / 1e6 * pin + tout / 1e6 * pout
    return 0.0


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _retry(fn, label: str, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            # 4xx other than rate limits will not get better by retrying.
            if status and 400 <= status < 500 and status != 429:
                raise
            if i == attempts - 1:
                raise
            wait = 2 ** i * 2
            log.warning("%s mislukt (%s); nieuwe poging over %ss", label, exc, wait)
            time.sleep(wait)


# ---------------------------------------------------------------------------
def _openai(model: str, system: str, user: str, schema: dict, max_tokens: int):
    from openai import BadRequestError, OpenAI
    client = OpenAI()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    # gpt-5 and later (and o-series) are reasoning models: they take
    # max_completion_tokens and reject a non-default temperature. Reasoning
    # tokens come out of the same budget, hence the headroom.
    major = re.match(r"gpt-(\d+)", model)
    reasoning = bool(re.match(r"o[1-9]", model)) or (major is not None and int(major.group(1)) >= 5)
    kwargs: dict = {"model": model, "messages": messages}
    if reasoning:
        kwargs["max_completion_tokens"] = max_tokens + 4000
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0.2

    def call(response_format):
        return client.chat.completions.create(response_format=response_format, **kwargs)

    strict = {"type": "json_schema",
              "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    try:
        resp = _retry(lambda: call(strict), f"OpenAI {model}")
    except BadRequestError as exc:
        # Older or unusual models: fall back to plain JSON mode; the pipeline
        # validates every field anyway.
        log.warning("Strict JSON schema geweigerd door %s (%s); terugval op json_object", model, exc)
        resp = _retry(lambda: call({"type": "json_object"}), f"OpenAI {model}")
    text = resp.choices[0].message.content or ""
    usage = resp.usage
    return _parse_json(text), usage.prompt_tokens, usage.completion_tokens


def _anthropic(model: str, system: str, user: str, schema: dict, max_tokens: int):
    import anthropic
    client = anthropic.Anthropic()
    user = (user + "\n\nAntwoord met uitsluitend één JSON-object dat aan dit schema voldoet "
            "(geen tekst ervoor of erna):\n" + json.dumps(schema, ensure_ascii=False))
    resp = _retry(lambda: client.messages.create(
        model=model, max_tokens=max_tokens + 2000, system=system,
        messages=[{"role": "user", "content": user}]), f"Anthropic {model}")
    # Thinking models put a thinking block first; collect only text blocks.
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _parse_json(text), resp.usage.input_tokens, resp.usage.output_tokens


def complete_json(step: str, system: str, user: str, schema: dict,
                  max_tokens: int = 1500) -> LLMResult:
    provider, model = get_spec(step)
    fn = _openai if provider == "openai" else _anthropic
    data, tin, tout = fn(model, system, user, schema, max_tokens)
    return LLMResult(data=data, model=f"{provider}:{model}", tokens_in=tin, tokens_out=tout,
                     cost=_price(model, tin, tout))

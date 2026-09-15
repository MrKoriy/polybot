import asyncio
import json
import re
from dataclasses import dataclass

import structlog

import analysis.prompts as _prompts_module
from analysis.prompts import format_news_items
from config.constants import LLMProvider
from core.exceptions import LLMError
from data.base import NewsItem

log = structlog.get_logger()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_MAX_RETRIES = 2
_RETRY_BACKOFF_BASE = 1.5  # seconds


@dataclass
class ProbabilityEstimate:
    probability: float
    confidence: float
    reasoning: str
    key_factors: list[str]
    counter_arguments: list[str]
    model_used: str


class LLMClient:
    def __init__(
        self,
        anthropic_key: str = "",
        openai_key: str = "",
        openrouter_key: str = "",
        primary_model: str = "google/gemini-3.1-flash-lite-preview",
        secondary_model: str = "google/gemini-3.1-flash-lite-preview",
        primary_provider: str = "openrouter",
        secondary_provider: str = "openrouter",
    ) -> None:
        self._anthropic_key = anthropic_key
        self._openai_key = openai_key
        self._openrouter_key = openrouter_key
        self._primary_model = primary_model
        self._secondary_model = secondary_model
        self._primary_provider = primary_provider
        self._secondary_provider = secondary_provider
        self._anthropic = None
        self._openai = None
        self._openrouter = None

    async def estimate_probability(
        self,
        question: str,
        market_price: float,
        end_date: str,
        news_items: list[NewsItem],
        provider: LLMProvider = LLMProvider.OPENROUTER,
        model: str = "",
        lookback_hours: int = 2,
    ) -> ProbabilityEstimate:
        formatted_news = format_news_items(news_items)

        user_prompt = _prompts_module.PROBABILITY_ESTIMATION_USER.format(
            question=question,
            market_price=market_price,
            market_pct=market_price * 100,
            end_date=end_date,
            news_count=len(news_items),
            lookback_hours=lookback_hours,
            formatted_news=formatted_news,
        )

        if provider == LLMProvider.OPENROUTER:
            model_id = model or self._primary_model
            return await self._call_openrouter(question, user_prompt, model_id)
        elif provider == LLMProvider.CLAUDE:
            return await self._call_claude(question, user_prompt)
        elif provider == LLMProvider.OPENAI:
            return await self._call_openai(question, user_prompt)
        else:
            raise LLMError(f"Unknown provider: {provider}")

    async def call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 512,
        timeout_s: float = 10.0,
    ) -> dict | None:
        """Generic JSON-returning LLM call using primary provider.

        Used by lightweight consumers (e.g. weather anomaly filter) that have
        their own prompts and don't need the full ProbabilityEstimate schema.

        Returns parsed dict on success, None on any error (fail-open).
        """
        if self._primary_provider == "openrouter" and self._openrouter_key:
            call = self._json_openrouter
        elif self._primary_provider == "claude" and self._anthropic_key:
            call = self._json_claude
        elif self._primary_provider == "openai" and self._openai_key:
            call = self._json_openai
        else:
            return None

        try:
            text = await asyncio.wait_for(
                call(system_prompt, user_prompt, max_tokens),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("llm_call_json_timeout", timeout=timeout_s)
            return None
        except Exception as e:
            log.warning("llm_call_json_error", err=str(e)[:120])
            return None

        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            extracted = self._extract_json(text)
            return extracted

    async def _json_openrouter(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self._openrouter is None:
            import openai
            self._openrouter = openai.AsyncOpenAI(
                api_key=self._openrouter_key,
                base_url=OPENROUTER_BASE_URL,
            )
        response = await self._openrouter.chat.completions.create(
            model=self._primary_model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            extra_headers={
                "HTTP-Referer": "https://github.com/polymarket-bot",
                "X-Title": "Polymarket Trading Bot",
            },
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    async def _json_claude(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.AsyncAnthropic(api_key=self._anthropic_key)
        response = await self._anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        if not response.content:
            return ""
        return response.content[0].text or ""

    async def _json_openai(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self._openai is None:
            import openai
            self._openai = openai.AsyncOpenAI(api_key=self._openai_key)
        response = await self._openai.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    async def _call_with_retry(self, func, *args, **kwargs) -> str:
        """Call an LLM function with exponential backoff retry."""
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except LLMError:
                raise  # Don't retry our own parse errors
            except Exception as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF_BASE ** attempt
                    log.warning("llm_retry", attempt=attempt + 1, wait=wait, error=str(e)[:100])
                    await asyncio.sleep(wait)
        raise LLMError(f"LLM call failed after {_MAX_RETRIES + 1} attempts: {last_error}") from last_error

    async def _call_openrouter(
        self, question: str, user_prompt: str, model: str
    ) -> ProbabilityEstimate:
        if not self._openrouter_key:
            raise LLMError("OpenRouter API key not set")

        async def _do_call():
            if self._openrouter is None:
                import openai

                self._openrouter = openai.AsyncOpenAI(
                    api_key=self._openrouter_key,
                    base_url=OPENROUTER_BASE_URL,
                )

            response = await self._openrouter.chat.completions.create(
                model=model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": _prompts_module.PROBABILITY_ESTIMATION_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                extra_headers={
                    "HTTP-Referer": "https://github.com/polymarket-bot",
                    "X-Title": "Polymarket Trading Bot",
                },
            )
            if not response.choices:
                raise LLMError("OpenRouter returned empty choices")
            return response.choices[0].message.content

        text = await self._call_with_retry(_do_call)
        return self._parse_response(text, model)

    async def _call_claude(self, question: str, user_prompt: str) -> ProbabilityEstimate:
        if not self._anthropic_key:
            raise LLMError("Anthropic API key not set")

        async def _do_call():
            if self._anthropic is None:
                import anthropic

                self._anthropic = anthropic.AsyncAnthropic(api_key=self._anthropic_key)

            response = await self._anthropic.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=_prompts_module.PROBABILITY_ESTIMATION_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
            if not response.content:
                raise LLMError("Claude returned empty content")
            return response.content[0].text

        text = await self._call_with_retry(_do_call)
        return self._parse_response(text, "claude-haiku-4-5")

    async def _call_openai(self, question: str, user_prompt: str) -> ProbabilityEstimate:
        if not self._openai_key:
            raise LLMError("OpenAI API key not set")

        async def _do_call():
            if self._openai is None:
                import openai

                self._openai = openai.AsyncOpenAI(api_key=self._openai_key)

            response = await self._openai.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": _prompts_module.PROBABILITY_ESTIMATION_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            if not response.choices:
                raise LLMError("OpenAI returned empty choices")
            return response.choices[0].message.content

        text = await self._call_with_retry(_do_call)
        return self._parse_response(text, "gpt-4o-mini")

    def _parse_response(self, text: str, model: str) -> ProbabilityEstimate:
        # Strip markdown code blocks
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract the outermost balanced JSON object
            data = self._extract_json(text)
            if data is None:
                raise LLMError(f"No valid JSON found in LLM response: {text[:200]}")

        probability = float(data.get("probability") or 0.5)
        confidence = float(data.get("confidence") or 0.5)

        # Log original values before clamping
        raw_prob, raw_conf = probability, confidence

        # Clamp values
        probability = max(0.01, min(0.99, probability))
        confidence = max(0.0, min(1.0, confidence))

        if raw_prob != probability or raw_conf != confidence:
            log.debug(
                "llm_values_clamped",
                model=model,
                raw_prob=raw_prob,
                raw_conf=raw_conf,
                clamped_prob=probability,
                clamped_conf=confidence,
            )

        return ProbabilityEstimate(
            probability=probability,
            confidence=confidence,
            reasoning=data.get("reasoning", ""),
            key_factors=data.get("key_factors", []),
            counter_arguments=data.get("counter_arguments", []),
            model_used=model,
        )

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract a JSON object from text with proper brace balancing."""
        start = text.find("{")
        if start < 0:
            return None

        # Walk through and find the matching closing brace
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        # Try to find next JSON object
                        next_start = text.find("{", start + 1)
                        if next_start >= 0:
                            remaining = text[next_start:]
                            match = re.search(r'\{[^{}]*\}', remaining)
                            if match:
                                try:
                                    return json.loads(match.group())
                                except json.JSONDecodeError:
                                    pass
                        return None

        return None

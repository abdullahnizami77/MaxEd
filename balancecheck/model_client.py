"""The thin model client: OpenAI-compatible live mode plus deterministic stub.

Design points that carry weight:

- Every attempt (including failures) emits exactly one trace line, from a
  finally block, so invariant I6 holds in live mode too, not only in the
  stub run the test can see.
- Structured calls use JSON-Schema constrained decoding when the endpoint
  supports it (vLLM does), which makes malformed judge/verifier JSON
  structurally impossible at the decode level; parsed output is still
  validated here, retried on failure, and the malformed rate is reported as
  a first-class number rather than hidden.
- Stub mode is keyed by an explicit stub_key the call site provides, read
  from a canned-responses file; it needs zero credentials and no network,
  and the whole test suite runs against it.
- Thinking-mode output is disabled via chat_template_kwargs and defensively
  stripped, because a reasoning preamble in draft text would poison claim
  extraction downstream.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from balancecheck.config import Config
from balancecheck.spine.trace import trace

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class StubKeyMissing(KeyError):
    pass


@dataclass
class ModelResponse:
    text: str
    ok: bool
    parsed: Any = None            # dict when a schema was requested and validated
    error: str = ""
    attempts: int = 1
    parse_failures: int = 0       # malformed structured outputs before success


@dataclass
class ModelClient:
    cfg: Config
    run_id: str = ""
    _stub_responses: dict | None = field(default=None, repr=False)
    call_count: int = 0           # attempts made; the I6 test compares this to trace lines

    # ------------------------------------------------------------------
    def complete(
        self,
        task: str,
        prompt: str,
        *,
        system: str = "",
        schema: dict | None = None,
        stub_key: str = "",
        meta: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1800,
    ) -> ModelResponse:
        """One logical completion; retries transport and parse failures.

        schema: when given, the endpoint is asked for schema-constrained
        decoding and the output is json-parsed and jsonschema-validated;
        `parsed` carries the result.
        """
        meta = dict(meta or {})
        parse_failures = 0
        last_error = ""
        current_prompt = prompt
        for attempt in range(1, self.cfg.max_attempts + 1):
            output_text = ""
            ok = False
            error = ""
            try:
                if self.cfg.mode == "stub":
                    output_text = self._stub_lookup(task, stub_key)
                else:
                    output_text = self._live_call(
                        current_prompt, system, schema, temperature, max_tokens
                    )
                output_text = strip_thinking(output_text)
                if schema is not None:
                    parsed = _parse_structured(output_text, schema)
                    ok = True
                    return ModelResponse(
                        text=output_text,
                        ok=True,
                        parsed=parsed,
                        attempts=attempt,
                        parse_failures=parse_failures,
                    )
                ok = True
                return ModelResponse(
                    text=output_text,
                    ok=True,
                    attempts=attempt,
                    parse_failures=parse_failures,
                )
            except StructuredParseError as e:
                parse_failures += 1
                error = f"structured parse failure: {e}"
                last_error = error
                current_prompt = (
                    prompt
                    + "\n\nYour previous output was not valid against the required JSON schema: "
                    + str(e)
                    + "\nReturn only a valid JSON object."
                )
            except StubKeyMissing:
                raise
            except Exception as e:  # transport, HTTP, timeout
                error = f"{type(e).__name__}: {e}"
                last_error = error
            finally:
                self.call_count += 1
                trace(
                    task,
                    current_prompt,
                    output_text,
                    {
                        **meta,
                        "model_name": self.cfg.model or "stub",
                        "attempt": attempt,
                        "ok": ok,
                        "error": error,
                        "run_id": self.run_id,
                        "stub": self.cfg.mode == "stub",
                        "stub_key": stub_key,
                    },
                    log_path=self.cfg.log_path,
                )
        return ModelResponse(
            text="",
            ok=False,
            error=last_error,
            attempts=self.cfg.max_attempts,
            parse_failures=parse_failures,
        )

    # ------------------------------------------------------------------
    def _stub_lookup(self, task: str, stub_key: str) -> str:
        if not stub_key:
            raise StubKeyMissing(f"stub mode requires a stub_key for task {task!r}")
        if self._stub_responses is None:
            if not self.cfg.stub_file.exists():
                raise StubKeyMissing(f"stub file not found: {self.cfg.stub_file}")
            self._stub_responses = json.loads(self.cfg.stub_file.read_text())
        try:
            entry = self._stub_responses[stub_key]
        except KeyError:
            raise StubKeyMissing(
                f"no stub response for key {stub_key!r} in {self.cfg.stub_file}"
            ) from None
        return entry["text"] if isinstance(entry, dict) else str(entry)

    def _live_call(
        self, prompt: str, system: str, schema: dict | None, temperature: float, max_tokens: int
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_output", "schema": schema},
            }
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        with httpx.Client(timeout=self.cfg.timeout_s) as client:
            resp = client.post(
                self.cfg.base_url.rstrip("/") + "/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("completion truncated at max_tokens")
        return choice["message"].get("content") or ""


class StructuredParseError(ValueError):
    pass


def _parse_structured(text: str, schema: dict) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise StructuredParseError(f"not JSON: {e}") from None
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as e:
        raise StructuredParseError(e.message) from None
    return parsed


def strip_thinking(text: str) -> str:
    """Remove <think> blocks and bare reasoning preambles defensively."""
    text = _THINK_BLOCK_RE.sub("", text)
    return text.strip()

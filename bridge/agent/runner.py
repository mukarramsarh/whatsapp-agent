"""
ReAct agent runner.
Reason → Act (tool calls) → Observe → Repeat until final answer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from agent import confidence as conf_module
from agent.language import detect as detect_lang, instruction as lang_instruction
from agent.tools.base import Tool

logger = logging.getLogger("bridge.agent")

_MASTER_PROMPT_FALLBACK = (
    "You are a highly intelligent AI assistant. "
    "Help users accurately and professionally. "
    "If you need more information, ask the user. "
    "Use your available tools when appropriate."
)


@dataclass
class AgentResult:
    content: str
    language: str = "en"
    tool_calls_made: list[str] = field(default_factory=list)
    confidence: float = 1.0
    iterations: int = 0


class AgentRunner:
    def __init__(
        self,
        settings: dict,
        tools: list[Tool],
    ):
        self.settings = settings
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.client = AsyncOpenAI(
            base_url=settings.get("ai_base_url", "http://localhost:8000/v1"),
            api_key=settings.get("ai_api_key", "local-key"),
        )
        self.model = settings.get("ai_model", "gpt-4o-mini")
        self.max_iterations = int(settings.get("agent_max_iterations", "10"))
        self.confidence_enabled = settings.get("confidence_enabled", "false").lower() == "true"
        self.confidence_threshold = float(settings.get("confidence_threshold", "0.7"))
        self.confidence_max_retries = int(settings.get("confidence_max_retries", "2"))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        context_messages: list[dict],
        role_prompt: str,
        language: str,
        attachment_meta: dict | None = None,
    ) -> AgentResult:
        system_prompt = self._build_system(role_prompt, language)
        user_content = self._build_user_content(user_message, attachment_meta)
        tools_schema = [t.to_openai_function() for t in self.tools.values()]

        messages: list[dict] = (
            [{"role": "system", "content": system_prompt}]
            + context_messages
            + [{"role": "user", "content": user_content}]
        )

        tool_calls_made: list[str] = []
        final_content = ""
        final_confidence = 1.0

        for attempt in range(self.confidence_max_retries + 1):
            attempt_messages = list(messages)
            result_text, calls, iterations = await self._react_loop(
                attempt_messages, tools_schema
            )
            tool_calls_made.extend(calls)

            if not self.confidence_enabled:
                final_content = result_text
                break

            sc, reason = await conf_module.score(self.client, self.model, user_message, result_text)
            final_confidence = sc
            if sc >= self.confidence_threshold:
                final_content = result_text
                break

            logger.info(
                "Confidence %.2f < threshold %.2f (attempt %d/%d): %s",
                sc, self.confidence_threshold, attempt + 1, self.confidence_max_retries + 1, reason,
            )
            if attempt < self.confidence_max_retries:
                # Feed the low-confidence result back and retry
                messages.append({"role": "assistant", "content": result_text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"[System note: your previous answer had low confidence "
                        f"(score {sc:.2f}): {reason}. "
                        f"Please reconsider and provide a more accurate, complete answer.]"
                    ),
                })
            else:
                final_content = result_text

        return AgentResult(
            content=final_content or "I'm sorry, I couldn't generate a response.",
            language=language,
            tool_calls_made=tool_calls_made,
            confidence=final_confidence,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _react_loop(
        self,
        messages: list[dict],
        tools_schema: list[dict],
    ) -> tuple[str, list[str], int]:
        """Run the Reason-Act loop. Returns (final_text, tool_names_used, iterations)."""
        tool_calls_made: list[str] = []

        for iteration in range(self.max_iterations):
            kwargs: dict = {
                "model": self.model,
                "messages": messages,
            }
            if tools_schema:
                kwargs["tools"] = tools_schema
                kwargs["tool_choice"] = "auto"

            try:
                response = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                logger.error("LLM call failed: %s", exc)
                return f"I encountered an error processing your request: {exc}", tool_calls_made, iteration

            choice = response.choices[0]
            finish = choice.finish_reason

            # Tool call path
            if finish == "tool_calls" and choice.message.tool_calls:
                messages.append(choice.message)  # assistant message with tool_calls
                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    tool_calls_made.append(fn_name)
                    tool_result = await self._execute_tool(fn_name, tc.function.arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(tool_result),
                    })
                    logger.info("Tool %s executed → %d chars output", fn_name, len(str(tool_result)))
                continue

            # Final answer
            content = (choice.message.content or "").strip()
            return content, tool_calls_made, iteration + 1

        # Exhausted iterations — return whatever the last assistant message was
        last_assistant = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "assistant"),
            "I'm sorry, I couldn't complete the task within the allowed steps.",
        )
        return last_assistant, tool_calls_made, self.max_iterations

    async def _execute_tool(self, name: str, arguments_json: str) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"Error: tool '{name}' not found."
        try:
            args = json.loads(arguments_json) if arguments_json else {}
            result = await tool.run(**args)
            return str(result)
        except Exception as exc:
            logger.error("Tool %s raised: %s", name, exc)
            return f"Tool error: {exc}"

    def _build_system(self, role_prompt: str, language: str) -> str:
        master = self.settings.get("master_prompt", _MASTER_PROMPT_FALLBACK).strip()
        parts = [master]
        if role_prompt:
            parts.append(f"\n--- User Role Context ---\n{role_prompt}")
        parts.append(f"\n--- Language ---\n{lang_instruction(language)}")
        return "\n".join(parts)

    @staticmethod
    def _build_user_content(text: str, attachment_meta: dict | None) -> str:
        if not attachment_meta:
            return text or "(no message)"
        mime = attachment_meta.get("mime", "")
        name = attachment_meta.get("name", "file")
        path = attachment_meta.get("path", "")
        caption = attachment_meta.get("caption", "")
        file_desc = f"[Attached file: {name} | type: {mime} | path: {path}]"
        if caption:
            file_desc += f" | Caption: {caption}"
        return f"{text}\n{file_desc}".strip() if text else file_desc

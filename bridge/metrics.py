"""
Central Prometheus metric registry for the bridge.

Rule: label values are closed-set enums only (direction, tool name, status,
error type, language) — never phone numbers, user IDs, or message content.
"""
from prometheus_client import Counter, Histogram

messages_total = Counter(
    "bridge_messages_total", "WhatsApp messages processed", ["direction"]
)
messages_ignored_total = Counter(
    "bridge_messages_ignored_total", "Inbound messages ignored without processing", ["reason"]
)
agent_pipeline_duration_seconds = Histogram(
    "bridge_agent_pipeline_duration_seconds", "End-to-end agent pipeline duration"
)
llm_call_duration_seconds = Histogram(
    "bridge_llm_call_duration_seconds", "LLM chat completion call duration", ["model"]
)
llm_call_errors_total = Counter(
    "bridge_llm_call_errors_total", "LLM call failures", ["error_type"]
)
tool_calls_total = Counter(
    "bridge_tool_calls_total", "Tool invocations", ["tool_name", "status"]
)
tool_call_duration_seconds = Histogram(
    "bridge_tool_call_duration_seconds", "Tool execution duration", ["tool_name"]
)
confidence_score = Histogram(
    "bridge_confidence_score", "Self-rated confidence score distribution",
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
confidence_retries_total = Counter(
    "bridge_confidence_retries_total", "Low-confidence retries triggered"
)
embedding_requests_total = Counter(
    "bridge_embedding_requests_total", "Embedding API calls", ["status"]
)
embedding_duration_seconds = Histogram(
    "bridge_embedding_duration_seconds", "Embedding API call duration"
)
embedding_retries_total = Counter(
    "bridge_embedding_retries_total", "Embedding API retry attempts (429/5xx backoff)"
)
stt_requests_total = Counter(
    "bridge_stt_requests_total", "Whisper transcription attempts", ["status"]
)
stt_duration_seconds = Histogram(
    "bridge_stt_duration_seconds", "Whisper transcription duration"
)
tts_requests_total = Counter(
    "bridge_tts_requests_total", "edge-tts synthesis attempts", ["status", "language"]
)
tts_duration_seconds = Histogram(
    "bridge_tts_duration_seconds", "edge-tts synthesis duration"
)

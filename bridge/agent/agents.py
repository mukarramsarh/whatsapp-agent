"""
Multi-agent layer.

One orchestrator agent talks to the user and delegates to specialist agents.
Each specialist is a full ReAct agent (agent/runner.py) with its own system
prompt and its own subset of the registered tools, so a specialist only ever
sees the tools that belong to its job:

    orchestrator ── delegate_to_agent(agent, task) ──┬─ knowledge  e-Library, knowledge base
                                                     ├─ documents  OCR, classification, vision
                                                     ├─ tender     BD/RFP analysis, tender generation
                                                     ├─ media      image-to-image, text-to-image, vision
                                                     └─ ops        DB / SSH / HTTP (admin role only)

A specialist whose tools are all disabled in /tools simply does not exist for
that request. Files a specialist produces (an edited photo, a PDF) travel back
through the delegate tool's ToolResult and are delivered by the bridge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from agent.tools.base import Tool, ToolResult, ToolSession

logger = logging.getLogger("bridge.agents")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    title: str
    summary: str            # one line the orchestrator sees when choosing
    tool_names: tuple[str, ...]
    prompt: str             # the specialist's own instructions
    admin_only: bool = False


AGENT_SPECS: list[AgentSpec] = [
    AgentSpec(
        name="knowledge",
        title="Knowledge & e-Library agent",
        summary=(
            "Answers questions from the company's document library (technical, consulting, "
            "contracts & tenders): methodology, past proposals, contract terms, tender history."
        ),
        tool_names=("search_cortex_elibrary", "search_knowledge_base"),
        prompt=(
            "You are the Knowledge specialist. Answer ONLY from what the search tools return; "
            "never invent facts. Pick the right pathway (technical, consulting, contracts_tenders) "
            "and search again with a different pathway or wording if the first result is empty. "
            "Always name the source documents. If nothing relevant is found, say so plainly."
        ),
    ),
    AgentSpec(
        name="documents",
        title="Documents agent",
        summary=(
            "Works on files the user sent: extracts text from scans/PDFs/photos (OCR), classifies "
            "procurement documents, and reads or explains a picture."
        ),
        tool_names=("cortex_ocr_extract_text", "ocr_extract_text", "classify_document", "describe_image"),
        prompt=(
            "You are the Documents specialist. Use the file path given in the task or in the "
            "[Attached file ...] marker; never invent a path. Use OCR to extract text, classify_document "
            "to identify the type of a procurement document, describe_image to understand a photo. "
            "Return the actual extracted text or result, trimmed to what the user asked for. "
            "If no file is available, say that a file is needed."
        ),
    ),
    AgentSpec(
        name="tender",
        title="Tender & BD agent",
        summary=(
            "Analyses a tender/RFP file for business development (4 tracks) and generates new "
            "tender / RFP / SOW documents (simple or Saudi-government format)."
        ),
        tool_names=("analyze_rfp", "generate_tender"),
        prompt=(
            "You are the Tender & BD specialist. For analyze_rfp you need a file and a track "
            "(technical, consulting, secondment, managed_services); for generate_tender first "
            "establish which mode (sow, rfp or gov_ksa) and collect that mode's required fields. "
            "If something required is missing, reply with ONE specific question and do not call the "
            "tool with guessed values. These jobs run for minutes in the background: when a tool says "
            "it has started, tell the user the result will follow in this chat."
        ),
    ),
    AgentSpec(
        name="media",
        title="Image studio agent",
        summary=(
            "Image-to-image editing of photos the user sent (background, style, colours, retouch), "
            "creating new images from a description, and describing pictures."
        ),
        tool_names=("edit_image", "generate_image", "describe_image"),
        prompt=(
            "You are the Image Studio specialist. To change a photo the user sent, call edit_image "
            "with its file path and a precise instruction in English (translate from Arabic). To make "
            "a brand-new picture call generate_image with a rich English prompt. To explain what is "
            "in a picture call describe_image. The finished image is delivered to the user "
            "automatically, so afterwards just confirm briefly what you did. If the request is "
            "ambiguous about WHAT to change, ask one short question instead of guessing."
        ),
    ),
    AgentSpec(
        name="ops",
        title="Operations agent",
        summary="Read-only database queries, SSH commands and external HTTP APIs (administrators only).",
        tool_names=("query_database", "execute_ssh_command", "call_external_api"),
        prompt=(
            "You are the Operations specialist for administrators. Prefer read-only actions, state "
            "exactly what you ran, and refuse anything destructive that the user did not explicitly ask for."
        ),
        admin_only=True,
    ),
]

_CONTEXT_TAIL = 6  # recent user/assistant messages a specialist also sees


def _orchestrator_prompt(specs: list[AgentSpec]) -> str:
    team = "\n".join(f"- {s.name}: {s.summary}" for s in specs)
    return (
        "--- You are the lead assistant of a team ---\n"
        "You talk to the user. You do NOT have document, image or tender tools yourself; your team does. "
        "Call delegate_to_agent to hand work to the right specialist:\n"
        f"{team}\n\n"
        "Rules:\n"
        "1. Greetings, thanks and small talk: answer yourself, no delegation.\n"
        "2. Any question about company documents, tenders, contracts, methodology or procurement facts: "
        "delegate to knowledge - do not answer those from memory.\n"
        "3. Whenever a file or photo is attached (or listed as a recent attachment), the task must carry "
        "its path exactly as shown in the [Attached file ...] marker.\n"
        "4. Write each task as a complete instruction: include everything the user said earlier in this "
        "conversation that matters (chosen options, project details, the exact edit requested). The "
        "specialist cannot read the whole chat.\n"
        "5. A photo with no instruction: delegate to media to describe it, then tell the user briefly what "
        "you can do with it (edit it, read its text, describe it).\n"
        "6. If a specialist asks the user a question or needs a missing detail, pass that question on in "
        "your own words and wait for the answer. When the answer arrives, delegate again with the full "
        "picture.\n"
        "7. Several steps may need several delegations (e.g. extract text, then classify). Do them in order.\n"
        "8. When a specialist names source documents, you MUST end your answer with a short line "
        "listing them, e.g. 'Source: Methodology_v3.pdf'.\n"
        "9. Never mention agents, tools or delegation to the user. Speak as one assistant. Never claim an "
        "action happened unless a specialist confirmed it."
    )


class DelegateTool(Tool):
    """The orchestrator's only tool: run a specialist agent on a task."""

    name = "delegate_to_agent"
    display_name = "Delegate to specialist agent"
    description = "Hand a task to a specialist agent and receive its answer (and any files it produced)."
    parameters_schema: dict = {}

    def __init__(
        self,
        specialists: dict,
        context_messages: list[dict],
        attachment_meta: dict | None,
        language: str,
        is_voice: bool,
    ):
        super().__init__({})
        self.specialists = specialists          # name -> (AgentSpec, AgentRunner)
        self.context_tail = [
            m for m in context_messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ][-_CONTEXT_TAIL:]
        self.attachment_meta = attachment_meta
        self.language = language
        self.is_voice = is_voice
        self.description = (
            "Hand a task to a specialist agent and receive its answer (and any files it produced). "
            "Available agents: " + ", ".join(self.specialists)
        )
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": list(self.specialists),
                    "description": "Which specialist should do the work.",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A complete, self-contained instruction including all needed details and any "
                        "file path from an [Attached file ...] marker."
                    ),
                },
            },
            "required": ["agent", "task"],
        }

    async def run(self, agent: str, task: str, session: ToolSession | None = None, **_) -> ToolResult:
        entry = self.specialists.get(agent)
        if not entry:
            return ToolResult(
                success=False, output="",
                error=f"unknown agent '{agent}'. Choose one of: {', '.join(self.specialists)}",
            )
        spec, runner = entry
        meta = self.attachment_meta
        # Make sure the specialist can see the file even if the orchestrator forgot the path.
        if meta and meta.get("path") and meta["path"] not in task:
            task = f"{task}\n[Attached file: {meta.get('name', 'file')} | type: {meta.get('mime', '')} | path: {meta['path']}]"
        logger.info("delegate -> %s: %s", spec.name, task[:200].replace("\n", " "))
        try:
            result = await runner.run(
                user_message=task,
                context_messages=self.context_tail,
                role_prompt=spec.prompt,
                language=self.language,
                attachment_meta=None,       # already embedded in `task`
                session=session,
                is_voice=False,             # specialists write plain results; the orchestrator speaks
            )
        except Exception as exc:
            logger.error("specialist %s crashed: %s", spec.name, exc)
            return ToolResult(success=False, output="", error=f"the {spec.name} agent failed: {exc}")
        logger.info(
            "specialist %s done (tools=%s, files=%d)",
            spec.name, result.tool_calls_made or "none", len(result.attachments),
        )
        return ToolResult(success=True, output=result.content, files=list(result.attachments))


def build_team(
    settings: dict,
    tools: list[Tool],
    role: str,
    context_messages: list[dict],
    attachment_meta: dict | None,
    language: str,
    is_voice: bool,
):
    """Return an orchestrator AgentRunner wired to its specialists, or None when
    no specialist has any enabled tool (the caller then falls back to a single
    flat agent)."""
    from agent.runner import AgentRunner

    by_name = {t.name: t for t in tools}
    # Specialists never re-score their own answers or get nudged; the
    # orchestrator already decides when to use them.
    spec_settings = dict(settings, confidence_enabled="false", agent_require_tool_check="false")

    specialists: dict = {}
    used: list[AgentSpec] = []
    for spec in AGENT_SPECS:
        if spec.admin_only and role != "admin":
            continue
        subset = [by_name[n] for n in spec.tool_names if n in by_name]
        if not subset:
            continue
        specialists[spec.name] = (spec, AgentRunner(settings=spec_settings, tools=subset))
        used.append(spec)

    if not specialists:
        return None

    delegate = DelegateTool(specialists, context_messages, attachment_meta, language, is_voice)
    orch_settings = dict(settings, agent_require_tool_check="false")
    orchestrator = AgentRunner(settings=orch_settings, tools=[delegate])
    orchestrator.system_extra = _orchestrator_prompt(used)
    return orchestrator

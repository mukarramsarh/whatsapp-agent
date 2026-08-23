# OCR Project — Migrate to Shared vLLM Models

**Goal:** Switch the OCR project from the old shared **Ollama** models (gpt + qwen)
to the **vLLM**-served models running on the DGX Spark, so it uses the same model
stack as the WhatsApp AI agent.

Hand this file to the OCR developer, or paste it to an AI assistant — it contains
everything needed to make the change.

---

## 1. What changes

| Purpose | OLD (Ollama) | NEW (vLLM) |
|---|---|---|
| Read images / documents (OCR) | qwen vision via Ollama `/api/generate` | **Qwen3-VL 8b** via OpenAI-compatible `/v1/chat/completions` |
| Text processing / cleanup | gpt via Ollama `/api/chat` | **gpt-oss 20b** via OpenAI-compatible `/v1/chat/completions` |

**The API style changes**: Ollama-native (`/api/generate`, `/api/chat`, body
`{"model","prompt"}`) → **OpenAI-compatible** (`/v1/chat/completions`, OpenAI
message format). Use the `openai` Python SDK or any OpenAI-compatible client.

---

## 2. Endpoints

Both models are served by vLLM on the DGX host and speak the OpenAI API.

| Model | Base URL | Purpose | API key |
|---|---|---|---|
| Qwen3-VL 8b (vision) | `http://<HOST>:8003/v1` | image → text (OCR) | `none` |
| gpt-oss 20b (text) | `http://<HOST>:8002/v1` | text tasks | `none` |

**`<HOST>`** = `localhost` if the OCR service runs **directly on the DGX host**,
otherwise the DGX LAN IP `192.168.100.62`. See the networking note in §6 first —
the vLLM servers bind to `127.0.0.1`, which affects containers.

**Confirm the exact model IDs** before coding — query each server:

```bash
curl -s http://localhost:8003/v1/models   # Qwen3-VL — copy the "id"
curl -s http://localhost:8002/v1/models   # gpt-oss  — "gpt-oss:20b" or "openai/gpt-oss-20b"
```

Use the exact `id` string the server returns as the `model` parameter.

---

## 3. OCR request — Qwen3-VL (vision)

Send the image as a base64 `data:` URL inside an OpenAI `image_url` content block.

**Python (OpenAI SDK):**

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8003/v1", api_key="none", max_retries=4, timeout=120)

def ocr_image(image_path: str, mime: str = "image/jpeg") -> str:
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    resp = client.chat.completions.create(
        model="<QWEN3_VL_ID_FROM_/v1/models>",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Extract ALL text from this image exactly as it appears. "
                         "Preserve line breaks and reading order. Return only the text, no commentary."},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        temperature=0.0,
    )
    return resp.choices[0].message.content
```

**Notes**
- PDFs: render each page to an image (e.g. `pdf2image`) and OCR page by page.
- `temperature=0.0` for faithful, deterministic extraction.
- For Arabic/mixed content, add "The text may be in Arabic or English." to the prompt.

---

## 4. Text request — gpt-oss (cleanup / structuring)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8002/v1", api_key="none", max_retries=4, timeout=120)

def process_text(raw_text: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-oss:20b",   # confirm via /v1/models
        messages=[
            {"role": "system", "content": "You clean up and structure OCR output."},
            {"role": "user", "content": raw_text},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content
```

---

## 5. Config / env changes to make in the OCR project

Replace the old Ollama settings with:

```
# Vision model (OCR) — Qwen3-VL 8b on vLLM
OCR_VISION_BASE_URL=http://localhost:8003/v1
OCR_VISION_MODEL=<qwen3-vl id from /v1/models>
OCR_VISION_API_KEY=none

# Text model — gpt-oss 20b on vLLM
OCR_TEXT_BASE_URL=http://localhost:8002/v1
OCR_TEXT_MODEL=gpt-oss:20b
OCR_TEXT_API_KEY=none
```

Remove any Ollama base URLs (e.g. `http://...:11434`) and any Ollama-native
calling code (`/api/generate`, `/api/chat`, `requests.post(... "prompt" ...)`).

---

## 6. Networking (IMPORTANT)

The vLLM servers are bound to **`127.0.0.1`** (loopback) on the DGX host and are
owned by another team — they cannot be rebound.

- **If the OCR service runs directly on the DGX host** (bare process): use
  `http://localhost:8003/v1` and `http://localhost:8002/v1` directly. Done.
- **If the OCR service runs in a Docker container**: it cannot reach the host's
  `127.0.0.1`. Add a socat forwarder on the host that exposes the vision port on
  all interfaces, then point the container at the host IP + forward port:

  ```bash
  # on the DGX host — expose Qwen3-VL 127.0.0.1:8003 -> 0.0.0.0:18003
  docker run -d --name fwd-qwen --network host --restart unless-stopped \
    alpine/socat TCP-LISTEN:18003,fork,reuseaddr TCP:127.0.0.1:8003
  ```
  Then set `OCR_VISION_BASE_URL=http://192.168.100.62:18003/v1`.
  (gpt-oss already has such a forwarder on port `18002`, so text can use
  `http://192.168.100.62:18002/v1`.)

---

## 7. Resilience

vLLM returns **HTTP 429** when the shared GPU memory budget is saturated. The
OpenAI SDK already retries 429/5xx with exponential backoff when you pass
`max_retries` (shown above) — keep it at 4. Do not hammer with large batches.

---

## 8. Verification

1. `curl -s http://<HOST>:8003/v1/models` returns JSON with the Qwen3-VL id.
2. Run `ocr_image()` on a known sample image → returns the correct text.
3. Confirm behaviour on an Arabic sample and a PDF page.
4. Confirm no remaining references to the old Ollama endpoints in the codebase.

---

*For: OCR project team · From: WhatsApp AI Agent team*

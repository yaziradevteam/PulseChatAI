# PulseChat AI - Telegram AI Chatbot

An AI-powered Telegram chatbot that engages users in flirty conversation and drives them to your content page (Fanvue, OnlyFans, or any link). Uses a custom DPO-trained LLM (via RunPod Serverless) for natural chat, with intent detection, lead scoring, photo escalation, and smart CTA timing.

Join our Telegram community for support, updates, and discussion: **[AI Hustlers GC](https://t.me/aihustlersgc)**

---

## Features

- **AI Conversation**: Uses a DPO-trained model (via RunPod Serverless + vLLM) for natural, in-character chat
- **Multi-Intent Detection**: Classifies user intents (compliment, buy intent, objection, etc.) using Mistral Small 24B via DeepInfra
- **Sentiment Analysis**: Keyword + emoji scoring combined with intent-based sentiment hints (no external API call)
- **Lead Temperature Scoring**: Cold/warm/hot lead classification based on engagement signals
- **Dynamic CTA Timing**: Smart timing for when to promote Fanvue based on lead temperature
- **Photo Escalation System**: Progressive photo unlocking (casual > flirty > spicy) based on engagement
- **Re-engagement System**: Auto-messages inactive users based on their lead temperature
- **RAG Memory**: Vector-based conversation memory using ChromaDB + OpenAI embeddings
- **Conversion Tracking**: Detects when users subscribe and logs conversions
- **Voice & Image Support**: Transcribes voice notes (Whisper) and describes images (GPT-4o)

---

## Prerequisites

- **Python 3.10+**
- **Telegram Account** (the bot runs as a Telethon userbot on your personal account)
- **RunPod Account** with a deployed vLLM serverless endpoint (see below)
- **OpenAI API Key** (for Whisper voice transcription, GPT-4o image description, and RAG embeddings)
- **DeepInfra API Key** (for Mistral Small 24B intent detection)

---

## Setting Up Your LLM Endpoint on RunPod

The bot uses a custom DPO-trained model for generating responses. You'll need to deploy it as a serverless endpoint on RunPod using vLLM.

### The Model

The fine-tuned model is available on HuggingFace:

**[jessicarizzler/amelia-32b-dpo-merged](https://huggingface.co/jessicarizzler/amelia-32b-dpo-merged)**

This is a 32B parameter DPO-merged model optimized for flirty, in-character conversation.

### Deploying on RunPod Serverless

1. **Create a RunPod account** at [runpod.io](https://www.runpod.io) and add credits
2. **Get your API key** from [Settings > API Keys](https://www.runpod.io/console/user/settings) — you'll need this for config
3. **Go to [Serverless](https://www.runpod.io/console/serverless)** in your RunPod dashboard
4. **Under Quick Deploy, find the vLLM preset** and click **Start** — this auto-configures everything using the [runpod-workers/worker-vllm](https://github.com/runpod-workers/worker-vllm) template
5. **Enter the HuggingFace model ID**: `jessicarizzler/amelia-32b-dpo-merged`
6. **Set environment variables** (click Advanced):
   - `MAX_MODEL_LEN`: `4096` (context window — keep reasonable for speed)
   - `DTYPE`: `float16` (or `bfloat16` if your GPU supports it)
   - `GPU_MEMORY_UTILIZATION`: `0.95`
7. **Select GPU**: For a 32B model you'll need a GPU with at least 48GB VRAM (e.g., A100 80GB, A6000 48GB, or 2x A100 40GB)
8. **Set Min/Max Workers**: Start with 0 min (scales to zero when idle to save cost) and 1 max
9. **Click Create Endpoint**

Once deployed, note your **Endpoint ID** — your API URLs will be:
- **Run endpoint**: `https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/run`
- **Status endpoint**: `https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/status`

For more details see the [RunPod Serverless vLLM docs](https://docs.runpod.io/serverless/workers/vllm/get-started).

---

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API Keys

Open `config.py` and fill in the placeholder values:

```python
# API Keys - REQUIRED
RUNPOD_API_KEY = "YOUR_RUNPOD_API_KEY_HERE"
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY_HERE"
DEEPINFRA_API_KEY = "YOUR_DEEPINFRA_API_KEY_HERE"

# RunPod Endpoint - REQUIRED (from the deployment step above)
RUNPOD_ENDPOINT = "https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/run"
RUNPOD_STATUS_ENDPOINT = "https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/status"

# Telegram Credentials - REQUIRED
TELEGRAM_API_ID = 0          # Get from https://my.telegram.org
TELEGRAM_API_HASH = "YOUR_API_HASH_HERE"
TELEGRAM_PHONE_NUMBER = "+1234567890"
```

### 3. Get Your Telegram API Credentials

1. Go to https://my.telegram.org
2. Log in with your phone number
3. Go to "API Development Tools"
4. Create a new application
5. Copy the `api_id` and `api_hash` into `config.py`

### 4. Configure Your Model Profile

In `config.py`, customize the model's identity:

```python
MODEL_NAME = "YourModelName"       # Display name
MODEL_LOCATION = "London"          # Where the model "lives"
MODEL_BIO = "Your bio here..."     # Character description
FANVUE_LINK = "https://www.fanvue.com/YOUR_USERNAME"
FANVUE_IS_FREE = True              # True if free subscription, False if paid
FANVUE_PRICE = "$5/month"          # Price (only used if FANVUE_IS_FREE = False)
MODEL_TIMEZONE = "Europe/London"   # For morning/night photo timing
```

### 5. Set Up Photo Folders

Create the following folder structure:

```
fanvue-link-bot/
  photos/
    casual/       # Level 1 - casual selfies
    flirty/       # Level 2 - flirty poses
    morning/      # Level 2 - morning selfies
    night/        # Level 3 - night/going out
    spicy/        # Level 4 - more revealing (redirects to Fanvue after)
```

Place your photos (.jpg, .jpeg, .png, .gif) in each folder.

### 6. (Optional) Knowledge Base

Place `.txt` or `.md` files in a `knowledge/` folder for the RAG system to use as context. This is useful for storing facts about the model persona that should stay consistent across conversations.

---

## Running the Bot

```bash
python bot_telethon.py
```

On first run, you'll be prompted to enter your **Telegram verification code** (Telethon login). The session is saved locally so you only need to do this once.

The bot will then:
1. Validate your configuration
2. Restore state from any existing conversations
3. Load knowledge documents into RAG
4. Start listening for incoming messages

---

## How It Works

1. User sends a message on Telegram
2. Bot batches messages (30-60s delay for realism)
3. Intent is classified (Mistral Small 24B via DeepInfra)
4. Sentiment is scored (keyword + emoji analysis with intent hint)
5. Lead temperature is calculated from engagement signals
6. RAG retrieves relevant context from past conversations and knowledge base
7. System prompt is built with all context
8. DPO model generates a response (RunPod vLLM)
9. Response is split into natural short text messages
10. Photos are sent based on triggers and escalation level
11. CTA (Fanvue link) is included when timing is right
12. Conversion is detected and tracked

---

## File Structure

| File | Description |
|------|-------------|
| `bot_telethon.py` | Main bot logic - message handling, AI pipeline, photo sending |
| `config.py` | All configuration - API keys, prompts, timing, photo settings |
| `conversation_manager.py` | User profiles, conversation history, lead scoring |
| `rag_manager.py` | Vector store (ChromaDB) for conversation memory + knowledge base |
| `requirements.txt` | Python dependencies |

---

## Configuration Reference

### Key Settings in `config.py`

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_MESSAGES_BEFORE_CTA` | 5 | Messages before promoting Fanvue |
| `MAX_PHOTOS_PER_CHAT` | 3 | Free photos before redirecting to Fanvue |
| `PHOTO_ESCALATION_ENABLED` | True | Progressive photo unlocking |
| `RE_ENGAGEMENT_ENABLED` | True | Auto-message inactive users |
| `RE_ENGAGEMENT_INACTIVE_HOURS` | 24 | Hours before re-engagement |
| `RAG_ENABLED` | True | Vector-based conversation memory |
| `MIN_TYPING_DELAY` | 1.5s | Min delay before responding |
| `MAX_TYPING_DELAY` | 4.0s | Max delay before responding |
| `SCAN_UNREPLIED_ON_START` | True | Reply to unreplied messages on startup |

### CTA Timing by Lead Temperature

| Temperature | Min Messages | Approach |
|-------------|-------------|----------|
| Cold | 8 | Subtle |
| Warm | 5 | Natural |
| Hot | 3 | Direct |

---

## Troubleshooting

- **"CONFIGURATION ERRORS"**: The bot validates config on startup — check that all API keys and endpoints are filled in
- **"Session file not found"**: Normal on first run — you'll be prompted to log in
- **"FloodWaitError"**: Telegram rate limit — the bot will wait and retry with exponential backoff
- **"RunPod job FAILED"**: Check your RunPod endpoint is deployed and has GPU available (cold starts can take a few minutes)
- **Photos not sending**: Check folder paths exist and contain supported image files (.jpg, .jpeg, .png, .gif)

---

## Cost Breakdown

| Service | Used For | Approximate Cost |
|---------|----------|-----------------|
| **RunPod** | Chat responses (DPO model) | ~$0.001-0.01 per response (serverless, pay per second) |
| **DeepInfra** | Intent detection (Mistral Small 24B) | ~$0.0002 per message |
| **OpenAI** | Voice transcription (Whisper) | ~$0.006 per minute of audio |
| **OpenAI** | Image description (GPT-4o) | ~$0.01-0.03 per image |
| **OpenAI** | RAG embeddings (text-embedding-3-small) | ~$0.00002 per embedding |

Sentiment detection is done locally with keyword analysis — no API cost.

---

## Important Notes

- The bot runs as a **userbot** using your personal Telegram account via Telethon
- First-time login requires your phone verification code
- The session is saved locally so you only need to log in once
- All conversations are stored in the `conversations/` folder as JSON files
- User profiles are stored in `conversations/user_profiles/`
- The vector store for RAG is persisted in `vector_store/`

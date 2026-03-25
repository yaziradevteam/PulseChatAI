import asyncio
import random
import os
import re
import json
import base64
import tempfile
import aiohttp
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser, PeerChat, PeerChannel
import pytz
from openai import AsyncOpenAI

from config import (
    RUNPOD_API_KEY,
    OPENAI_API_KEY,
    DEEPINFRA_API_KEY,
    DEEPINFRA_BASE_URL,
    INTENT_MODEL,
    INTENT_DETECTION_PROMPT,
    RUNPOD_ENDPOINT,
    RUNPOD_STATUS_ENDPOINT,
    MODEL_NAME,
    MODEL_LOCATION,
    MODEL_BIO,
    FANVUE_LINK,
    FANVUE_IS_FREE,
    FANVUE_PRICE,
    MODEL_TIMEZONE,
    PLATFORM,
    MORNING_PHOTOS_FOLDER,
    NIGHT_PHOTOS_FOLDER,
    PHOTOS_BASE_FOLDER,
    PHOTO_CATEGORIES,
    PHOTO_ESCALATION_ENABLED,
    MIN_MESSAGES_FOR_LEVEL_2,
    MIN_MESSAGES_FOR_LEVEL_3,
    MIN_MESSAGES_FOR_LEVEL_4,
    MIN_TYPING_DELAY,
    MAX_TYPING_DELAY,
    CHARS_PER_SECOND,
    MIN_RESPONSE_DELAY,
    MAX_RESPONSE_DELAY,
    MIN_MESSAGES_BEFORE_CTA,
    MAX_PHOTOS_PER_CHAT,
    CTA_TIMING,
    CTA_TRIGGERS,
    RE_ENGAGEMENT_ENABLED,
    RE_ENGAGEMENT_CHECK_INTERVAL,
    RE_ENGAGEMENT_INACTIVE_HOURS,
    RE_ENGAGEMENT_MESSAGES,
    CONVERSATION_END_CONDITIONS,
    MIN_MESSAGES_BEFORE_END,
    REQUIRE_CTA_BEFORE_END,
    SYSTEM_PROMPT_TEMPLATE,
    RAG_ENABLED,
    BASE_DIR,
    SCAN_UNREPLIED_ON_START,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_PHONE_NUMBER,
)

from conversation_manager import ConversationManager
from rag_manager import RAGManager

# Telethon credentials (imported from config.py)
API_ID = TELEGRAM_API_ID
API_HASH = TELEGRAM_API_HASH
PHONE_NUMBER = TELEGRAM_PHONE_NUMBER
SESSION_NAME = "my_account"

# Initialize OpenAI client (for Whisper and GPT-4o vision)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Initialize DeepInfra client (for intent detection)
deepinfra_client = AsyncOpenAI(
    api_key=DEEPINFRA_API_KEY,
    base_url=DEEPINFRA_BASE_URL
)

# Initialize conversation manager
conv_manager = ConversationManager()

# Initialize RAG manager
rag_manager = RAGManager() if RAG_ENABLED else None

# Message batching - stores pending messages per chat
pending_messages: dict[int, list[str]] = {}
pending_tasks: dict[int, asyncio.Task] = {}
MIN_BATCH_DELAY = 30  # Minimum 30 seconds before responding
MAX_BATCH_DELAY = 60  # Maximum 60 seconds before responding

# Track used photos per chat to avoid repeats
used_photos: dict[int, set[str]] = {}

# Track number of photos sent per chat (for limiting)
photos_sent_count: dict[int, int] = {}

# Track if CTA has been sent per chat
cta_sent: dict[int, bool] = {}

# Track ended conversations
ended_conversations: set[int] = set()

# Max retries for API calls
MAX_RETRIES = 3

# Store client reference globally for access in delayed tasks
client: TelegramClient = None

# Cache for resolved entities (needed for delayed responses) - LRU-style with max size
entity_cache: dict[int, any] = {}
ENTITY_CACHE_MAX_SIZE = 500

# Re-engagement task reference
re_engagement_task: asyncio.Task = None


# ============================================
# STARTUP VALIDATION
# ============================================

def validate_config():
    """Validate that all required configuration is set before starting."""
    errors = []

    if RUNPOD_API_KEY == "YOUR_RUNPOD_API_KEY_HERE" or not RUNPOD_API_KEY:
        errors.append("RUNPOD_API_KEY is not set in config.py")
    if OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE" or not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set in config.py")
    if DEEPINFRA_API_KEY == "YOUR_DEEPINFRA_API_KEY_HERE" or not DEEPINFRA_API_KEY:
        errors.append("DEEPINFRA_API_KEY is not set in config.py")
    if RUNPOD_ENDPOINT == "YOUR_RUNPOD_ENDPOINT_HERE" or not RUNPOD_ENDPOINT:
        errors.append("RUNPOD_ENDPOINT is not set in config.py")
    if RUNPOD_STATUS_ENDPOINT == "YOUR_RUNPOD_STATUS_ENDPOINT_HERE" or not RUNPOD_STATUS_ENDPOINT:
        errors.append("RUNPOD_STATUS_ENDPOINT is not set in config.py")
    if API_ID == 0:
        errors.append("TELEGRAM_API_ID is not set in config.py")
    if API_HASH == "YOUR_API_HASH_HERE" or not API_HASH:
        errors.append("TELEGRAM_API_HASH is not set in config.py")
    if PHONE_NUMBER == "+1234567890":
        errors.append("TELEGRAM_PHONE_NUMBER is not set in config.py")

    if errors:
        print("\n" + "=" * 50)
        print("CONFIGURATION ERRORS - Please fix before running:")
        print("=" * 50)
        for err in errors:
            print(f"  - {err}")
        print("=" * 50 + "\n")
        raise SystemExit(1)


# ============================================
# ENTITY CACHE WITH SIZE LIMIT
# ============================================

def cache_entity(chat_id: int, entity):
    """Cache an entity with LRU eviction when cache is full."""
    if len(entity_cache) >= ENTITY_CACHE_MAX_SIZE:
        # Remove oldest entry (first inserted)
        oldest_key = next(iter(entity_cache))
        del entity_cache[oldest_key]
    entity_cache[chat_id] = entity


async def get_entity(chat_id: int):
    """Get a resolved entity for a chat ID, using cache or resolving it."""
    global client

    # Return from cache if available
    if chat_id in entity_cache:
        return entity_cache[chat_id]

    # Try to resolve the entity
    try:
        entity = await client.get_entity(PeerUser(chat_id))
        cache_entity(chat_id, entity)
        return entity
    except Exception:
        pass

    try:
        entity = await client.get_entity(PeerChat(chat_id))
        cache_entity(chat_id, entity)
        return entity
    except Exception:
        pass

    try:
        entity = await client.get_entity(PeerChannel(chat_id))
        cache_entity(chat_id, entity)
        return entity
    except Exception:
        pass

    try:
        entity = await client.get_entity(chat_id)
        cache_entity(chat_id, entity)
        return entity
    except Exception as e:
        print(f"Could not resolve entity for chat_id {chat_id}: {e}")
        return None


def get_current_time_info() -> tuple:
    """Get current time and period (morning/night) based on model's timezone."""
    tz = pytz.timezone(MODEL_TIMEZONE)
    current_time = datetime.now(tz)
    hour = current_time.hour

    # Morning: 6am-6pm, Night: 6pm-6am
    time_period = "morning" if 6 <= hour < 18 else "night"

    return current_time.strftime("%I:%M %p"), time_period


# ============================================
# RESTORE IN-MEMORY STATE FROM DISK
# ============================================

def restore_state_from_disk():
    """Restore in-memory tracking dicts from persisted user profiles on startup."""
    print("[STARTUP] Restoring state from disk...")
    restored = 0
    for chat_id in conv_manager.get_all_chat_ids():
        profile = conv_manager.load_user_profile(chat_id)
        if profile.get("conversation_ended"):
            ended_conversations.add(chat_id)
        if profile.get("cta_sent"):
            cta_sent[chat_id] = True
        if profile.get("photos_sent", 0) > 0:
            photos_sent_count[chat_id] = profile["photos_sent"]
        restored += 1
    print(f"[STARTUP] Restored state for {restored} chats")


# ============================================
# SENTIMENT DETECTION (keyword-based + intent hint)
# ============================================
def detect_sentiment(user_message: str, intent_hint: str = "neutral") -> dict:
    """Detect user sentiment using keyword analysis + the sentiment_hint from intent detection.
    No external API call needed — saves cost vs the old GPT-4.1 approach.
    """
    text_lower = user_message.lower()

    # Keyword scoring
    positive_words = ["love", "great", "amazing", "thanks", "thank you", "haha", "lol",
                      "cute", "hot", "beautiful", "gorgeous", "wow", "omg", "awesome",
                      "perfect", "yes", "yay", "excited", "fun", "sweet", "nice"]
    negative_words = ["hate", "annoying", "fake", "scam", "bot", "waste", "ugly",
                      "boring", "no", "nah", "stop", "leave", "bye", "block",
                      "creepy", "weird", "gross", "stupid", "shut up", "fuck off"]

    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)

    # Emoji scoring
    positive_emojis = ["😊", "😘", "💕", "❤", "🥰", "😍", "🔥", "💋", "😏", "🥺", "💖", "😁", "👍", "🥵"]
    negative_emojis = ["😡", "😤", "🙄", "👎", "💀", "🤮", "😒", "😑"]

    pos_count += sum(1 for e in positive_emojis if e in user_message)
    neg_count += sum(1 for e in negative_emojis if e in user_message)

    # Calculate base score from keywords/emojis
    if pos_count + neg_count == 0:
        keyword_score = 0.0
    else:
        keyword_score = (pos_count - neg_count) / (pos_count + neg_count)

    # Incorporate intent hint (from Mistral's sentiment_hint field)
    hint_bonus = {"positive": 0.2, "negative": -0.2, "neutral": 0.0}.get(intent_hint, 0.0)
    score = max(-1.0, min(1.0, keyword_score + hint_bonus))

    # Determine sentiment label
    if score > 0.2:
        sentiment = "positive"
    elif score < -0.2:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    indicators = []
    if pos_count > 0:
        indicators.append("positive_keywords")
    if neg_count > 0:
        indicators.append("negative_keywords")
    if intent_hint != "neutral":
        indicators.append(f"intent_hint_{intent_hint}")

    print(f"[DEBUG] Sentiment: {sentiment} (score={score:.2f}, pos={pos_count}, neg={neg_count}, hint={intent_hint})")

    return {"sentiment": sentiment, "score": score, "indicators": indicators}


# ============================================
# MULTI-INTENT DETECTION (Mistral Small 24B via DeepInfra)
# ============================================
async def detect_intent(user_message: str) -> dict:
    """Detect user intent(s) using Mistral Small 24B. Handles all content types."""
    valid_intents = [
        "SMALL_TALK", "COMPLIMENT", "CONTENT_REQUEST",
        "PAYWALL_CURIOUS", "PRICE_QUESTION", "BUY_INTENT",
        "OBJECTION", "PLATFORM_MOVE", "BOUNDARY_TEST",
        "REJECTION", "AI_QUESTION", "CONVERTED", "TIME_WASTER"
    ]

    try:
        response = await deepinfra_client.chat.completions.create(
            model=INTENT_MODEL,
            messages=[
                {"role": "system", "content": INTENT_DETECTION_PROMPT},
                {"role": "user", "content": user_message}
            ],
            max_tokens=200,
            temperature=0.1
        )

        result_text = response.choices[0].message.content
        if result_text:
            result_text = result_text.strip()
        else:
            result_text = ""

        print(f"[DEBUG] Intent detection raw response: '{result_text}'")

        if not result_text:
            return {
                "intents": [{"intent": "SMALL_TALK", "confidence": 0.0}],
                "primary_intent": "SMALL_TALK",
                "sentiment_hint": "neutral"
            }

        try:
            # Extract JSON from markdown code blocks if present
            if "```" in result_text:
                json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', result_text, re.DOTALL)
                if json_match:
                    result_text = json_match.group(1)

            # Try to parse directly first (handles clean JSON)
            try:
                intent_data = json.loads(result_text.strip())
            except json.JSONDecodeError:
                # Try to find JSON object in the text
                start_idx = result_text.find('{')
                end_idx = result_text.rfind('}')
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    result_text = result_text[start_idx:end_idx + 1]
                    intent_data = json.loads(result_text)
                else:
                    raise json.JSONDecodeError("No JSON found", result_text, 0)

            # Handle new multi-intent format
            if "intents" in intent_data:
                intents = intent_data["intents"]
                # Validate intents — normalize to uppercase for case-insensitive matching
                validated_intents = []
                for i in intents:
                    intent_name = i.get("intent", "").upper().replace(" ", "_")
                    if intent_name in valid_intents:
                        validated_intents.append({
                            "intent": intent_name,
                            "confidence": float(i.get("confidence", 0.5))
                        })

                if not validated_intents:
                    validated_intents = [{"intent": "SMALL_TALK", "confidence": 0.0}]

                primary = intent_data.get("primary_intent", validated_intents[0]["intent"]).upper()
                if primary not in valid_intents:
                    primary = validated_intents[0]["intent"]

                return {
                    "intents": validated_intents,
                    "primary_intent": primary,
                    "sentiment_hint": intent_data.get("sentiment_hint", "neutral")
                }
            else:
                # Handle old single-intent format for backwards compatibility
                intent = intent_data.get("intent", "SMALL_TALK").upper()
                confidence = float(intent_data.get("confidence", 0.0))

                if intent not in valid_intents:
                    intent = "SMALL_TALK"
                    confidence = 0.0

                return {
                    "intents": [{"intent": intent, "confidence": confidence}],
                    "primary_intent": intent,
                    "sentiment_hint": "neutral"
                }

        except json.JSONDecodeError as e:
            print(f"[DEBUG] Failed to parse intent JSON: {e}, trying keyword match...")
            result_upper = result_text.upper()
            for intent_name in valid_intents:
                if intent_name in result_upper:
                    return {
                        "intents": [{"intent": intent_name, "confidence": 0.5}],
                        "primary_intent": intent_name,
                        "sentiment_hint": "neutral"
                    }

            return {
                "intents": [{"intent": "SMALL_TALK", "confidence": 0.0}],
                "primary_intent": "SMALL_TALK",
                "sentiment_hint": "neutral"
            }

    except Exception as e:
        print(f"[ERROR] Intent detection failed: {e}")
        return {
            "intents": [{"intent": "SMALL_TALK", "confidence": 0.0}],
            "primary_intent": "SMALL_TALK",
            "sentiment_hint": "neutral"
        }


# ============================================
# CONVERSATION SIGNALS TRACKING
# ============================================
def update_signals_from_intent(chat_id: int, intent_result: dict):
    """Update user signals based on detected intents."""
    intents = intent_result.get("intents", [])

    signal_mapping = {
        "COMPLIMENT": "compliments_given",
        "CONTENT_REQUEST": "content_requests",
        "PRICE_QUESTION": "price_inquiries",
        "OBJECTION": "objections_raised",
        "AI_QUESTION": "ai_accusations",
        "PLATFORM_MOVE": "platform_move_attempts",
        "BOUNDARY_TEST": "boundary_tests",
        "REJECTION": "negative_responses"
    }

    for intent_obj in intents:
        intent = intent_obj.get("intent", "")
        if intent in signal_mapping:
            conv_manager.update_user_signal(chat_id, signal_mapping[intent])

    # Also check for positive/flirty signals
    primary = intent_result.get("primary_intent", "")
    if primary in ["COMPLIMENT", "BUY_INTENT", "CONTENT_REQUEST"]:
        conv_manager.update_user_signal(chat_id, "positive_responses")
    if primary == "COMPLIMENT":
        conv_manager.update_user_signal(chat_id, "flirty_messages")


# ============================================
# DYNAMIC CTA TIMING
# ============================================
def should_send_cta(chat_id: int, intent_result: dict) -> tuple[bool, str]:
    """Determine if CTA should be sent based on dynamic timing rules."""
    profile = conv_manager.load_user_profile(chat_id)
    temperature = profile.get("lead_temperature", "cold")
    message_count = profile.get("total_messages", 0)
    signals = profile.get("signals", {})

    # Get timing rules for this temperature
    timing_rules = CTA_TIMING.get(temperature, CTA_TIMING["cold"])
    min_messages = timing_rules["min_messages"]
    approach = timing_rules["approach"]

    # Check trigger conditions
    primary_intent = intent_result.get("primary_intent", "")

    # Price inquiry - always trigger CTA
    if CTA_TRIGGERS.get("price_inquiry") and primary_intent == "PRICE_QUESTION":
        return True, "direct"

    # Content request - trigger CTA
    if CTA_TRIGGERS.get("content_request") and primary_intent == "CONTENT_REQUEST":
        return True, "natural"

    # Buy intent - definitely send CTA
    if primary_intent == "BUY_INTENT":
        return True, "direct"

    # Compliment threshold reached
    compliment_threshold = CTA_TRIGGERS.get("compliment_threshold", 3)
    if signals.get("compliments_given", 0) >= compliment_threshold:
        return True, approach

    # Photo max reached
    if CTA_TRIGGERS.get("photo_max_reached"):
        photos_sent = photos_sent_count.get(chat_id, 0)
        if photos_sent >= MAX_PHOTOS_PER_CHAT:
            return True, "direct"

    # Standard timing check
    if message_count >= min_messages:
        return True, approach

    return False, None


def get_cta_instruction(chat_id: int, intent_result: dict) -> str:
    """Get appropriate CTA instruction based on timing and approach."""
    should_cta, approach = should_send_cta(chat_id, intent_result)
    has_sent_cta = cta_sent.get(chat_id, False)

    if has_sent_cta:
        return "CTA already sent. Can remind them about fanvue naturally if relevant."

    if not should_cta:
        return "Focus on building rapport first. Don't mention fanvue yet - keep chatting and being flirty!"

    if approach == "direct":
        return f"Time to promote fanvue! Be direct and enthusiastic - include the link: {FANVUE_LINK}"
    elif approach == "natural":
        return f"You can naturally mention your fanvue now. Tease exclusive content and include the link: {FANVUE_LINK}"
    else:  # subtle
        return f"You can subtly hint at fanvue if the opportunity arises, but don't push hard yet."


# ============================================
# PHOTO ESCALATION SYSTEM
# ============================================
def get_user_photo_level(chat_id: int) -> int:
    """Determine what photo level user has unlocked based on engagement."""
    profile = conv_manager.load_user_profile(chat_id)
    message_count = profile.get("total_messages", 0)
    signals = profile.get("signals", {})
    compliments = signals.get("compliments_given", 0)

    # Level 1 is always available (casual)
    level = 1

    # Level 2: flirty/morning - need messages AND at least 1 compliment
    if message_count >= MIN_MESSAGES_FOR_LEVEL_2 and compliments >= 1:
        level = 2

    # Level 3: night - need more messages AND engagement
    if message_count >= MIN_MESSAGES_FOR_LEVEL_3 and compliments >= 2:
        level = 3

    # Level 4: spicy - high engagement only
    if message_count >= MIN_MESSAGES_FOR_LEVEL_4 and compliments >= 3:
        level = 4

    return level


def get_photo_for_level(chat_id: int, requested_type: str) -> tuple[str, str] | tuple[None, None]:
    """Get an appropriate photo based on user's unlocked level."""
    user_level = get_user_photo_level(chat_id)

    # Map requested type to category info
    category_info = PHOTO_CATEGORIES.get(requested_type)

    if not category_info:
        # Fallback to morning/night based on time
        _, time_period = get_current_time_info()
        category_info = PHOTO_CATEGORIES.get(time_period, PHOTO_CATEGORIES.get("morning"))
        requested_type = time_period

    required_level = category_info.get("level", 1)

    # If user hasn't unlocked this level, downgrade to appropriate level
    if required_level > user_level:
        # Find highest available category at user's level
        for cat_name, cat_info in PHOTO_CATEGORIES.items():
            if cat_info.get("level", 1) <= user_level:
                category_info = cat_info
                requested_type = cat_name

    folder = category_info.get("folder")
    if not folder or not os.path.exists(folder):
        # Fallback to morning/night folders
        _, time_period = get_current_time_info()
        folder = MORNING_PHOTOS_FOLDER if time_period == "morning" else NIGHT_PHOTOS_FOLDER
        requested_type = time_period

    # Get unused photo from this category
    photo_path = get_random_photo_from_folder(chat_id, folder)
    return photo_path, requested_type


def get_random_photo_from_folder(chat_id: int, folder: str) -> str | None:
    """Get a random unused photo from a specific folder."""
    if not os.path.exists(folder):
        return None

    all_photos = [os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))]

    if not all_photos:
        return None

    if chat_id not in used_photos:
        used_photos[chat_id] = set()

    available_photos = [p for p in all_photos if p not in used_photos[chat_id]]

    if not available_photos:
        return None

    photo = random.choice(available_photos)
    used_photos[chat_id].add(photo)
    return photo


def get_all_photos(photo_type: str) -> list[str]:
    """Get all photos from the specified folder (morning/night)."""
    folder = MORNING_PHOTOS_FOLDER if photo_type == "morning" else NIGHT_PHOTOS_FOLDER

    if not os.path.exists(folder):
        return []

    photos = [os.path.join(folder, f) for f in os.listdir(folder)
              if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))]
    return photos


def get_random_photo(chat_id: int, photo_type: str) -> str | None:
    """Get a random unused photo from the specified folder (morning/night)."""
    if PHOTO_ESCALATION_ENABLED:
        photo_path, actual_type = get_photo_for_level(chat_id, photo_type)
        return photo_path

    # Original behavior
    all_photos = get_all_photos(photo_type)

    if not all_photos:
        return None

    if chat_id not in used_photos:
        used_photos[chat_id] = set()

    available_photos = [p for p in all_photos if p not in used_photos[chat_id]]

    if not available_photos:
        return None

    photo = random.choice(available_photos)
    used_photos[chat_id].add(photo)
    return photo


# ============================================
# SMARTER DONE LOGIC
# ============================================
def should_end_conversation(chat_id: int, intent_result: dict, sentiment_result: dict) -> bool:
    """Determine if conversation should end based on multiple factors."""
    profile = conv_manager.load_user_profile(chat_id)
    primary_intent = intent_result.get("primary_intent", "")
    message_count = profile.get("total_messages", 0)
    has_sent_cta = cta_sent.get(chat_id, False)
    temperature = profile.get("lead_temperature", "cold")
    signals = profile.get("signals", {})

    # Never end conditions
    never_end = CONVERSATION_END_CONDITIONS.get("never_end_if", {})

    if never_end.get("cta_not_sent") and not has_sent_cta:
        print(f"[DEBUG] Chat {chat_id}: Not ending - CTA not sent yet")
        return False

    if never_end.get("hot_lead") and temperature == "hot":
        if primary_intent != "CONVERTED":
            print(f"[DEBUG] Chat {chat_id}: Not ending - hot lead, keep trying")
            return False

    if never_end.get("mid_conversation") and message_count < MIN_MESSAGES_BEFORE_END:
        print(f"[DEBUG] Chat {chat_id}: Not ending - not enough messages yet")
        return False

    # Hard end conditions
    hard_end = CONVERSATION_END_CONDITIONS.get("hard_end", {})

    if hard_end.get("converted") and primary_intent == "CONVERTED":
        print(f"[DEBUG] Chat {chat_id}: Hard end - user converted!")
        conv_manager.mark_converted(chat_id)
        return True

    # Soft end conditions (only if CTA was sent)
    soft_end = CONVERSATION_END_CONDITIONS.get("soft_end", {})

    if has_sent_cta:
        if soft_end.get("rejection_after_cta") and primary_intent == "REJECTION":
            print(f"[DEBUG] Chat {chat_id}: Soft end - rejection after CTA")
            return True

        objection_limit = soft_end.get("repeated_objections", 3)
        if signals.get("objections_raised", 0) >= objection_limit:
            print(f"[DEBUG] Chat {chat_id}: Soft end - too many objections")
            return True

        if soft_end.get("sentiment_very_negative"):
            sentiment_score = sentiment_result.get("score", 0)
            if sentiment_score <= -0.7:
                print(f"[DEBUG] Chat {chat_id}: Soft end - very negative sentiment")
                return True

    return False


def extract_photo_triggers(text: str) -> list:
    """Extract free photo triggers from the AI response."""
    pattern = r'\[PHOTO:(morning|night|casual|flirty|spicy)\]'
    matches = re.findall(pattern, text, re.IGNORECASE)
    return matches


def remove_photo_triggers(text: str) -> str:
    """Remove all photo triggers from the text."""
    text = re.sub(r'\[PHOTO:(morning|night|casual|flirty|spicy)\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[PAID_PHOTO:(morning|night|casual|flirty|spicy)\]', '', text, flags=re.IGNORECASE)
    return text.strip()


def check_conversation_done(text: str) -> bool:
    """Check if AI signaled conversation is done."""
    text_upper = text.upper()
    return '[DONE]' in text_upper or '[END]' in text_upper


def remove_done_trigger(text: str) -> str:
    """Remove [DONE] and [END] triggers from the text."""
    text = re.sub(r'\[DONE\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[END\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'/end\b', '', text, flags=re.IGNORECASE)
    return text.strip()


async def transcribe_voice_note(file_path: str) -> str:
    """Transcribe a voice note using OpenAI Whisper."""
    try:
        with open(file_path, 'rb') as audio_file:
            transcription = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        return transcription.text
    except Exception as e:
        print(f"Error transcribing voice note: {e}")
        return "[Voice message - could not transcribe]"


async def describe_image(file_path: str) -> str:
    """Describe an image using GPT-4o vision."""
    try:
        with open(file_path, 'rb') as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')

        extension = file_path.lower().split('.')[-1]
        mime_type = {
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'webp': 'image/webp'
        }.get(extension, 'image/jpeg')

        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe this image briefly in 1-2 sentences. Focus on what the person is doing, their appearance, or what they're showing. Be casual and natural, like describing a photo a friend sent you."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_data}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=150
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error describing image: {e}")
        return "[Image - could not describe]"


def parse_model_response(raw_response: str) -> str:
    """Parse the raw model output to extract only the first response.

    Handles cases where the model generates continuation including:
    - Role markers like 'user', 'User:', 'assistant', etc.
    - ChatML format markers
    - Model continuing as if it's a conversation
    """
    if not raw_response:
        return ""

    response = raw_response.strip()

    # Pattern 1: Catch "user" or "assistant" on its own line (ChatML style) - case insensitive
    role_line_pattern = re.search(r'\n\s*(user|assistant)\s*\n', response, re.IGNORECASE)
    if role_line_pattern:
        response = response[:role_line_pattern.start()].strip()

    # Pattern 2: Catch "user" or "assistant" at end of response
    role_end_pattern = re.search(r'\n\s*(user|assistant)\s*$', response, re.IGNORECASE)
    if role_end_pattern:
        response = response[:role_end_pattern.start()].strip()

    # Pattern 3: Catch role markers with colons
    role_patterns = [
        r'\s+User:', r'\s+user:',
        r'\s+Assistant:', r'\s+assistant:',
        r'\nUser:', r'\nuser:',
        r'\nAssistant:', r'\nassistant:',
    ]
    for pattern in role_patterns:
        match = re.search(pattern, response)
        if match:
            response = response[:match.start()].strip()

    # Pattern 4: Catch model name patterns
    model_pattern = re.search(rf'[\s\n]+{re.escape(MODEL_NAME)}:', response, re.IGNORECASE)
    if model_pattern:
        response = response[:model_pattern.start()].strip()

    # Pattern 5: Catch ChatML special tokens
    chatml_patterns = ['<|im_start|>', '<|im_end|>', '<|user|>', '<|assistant|>']
    for pattern in chatml_patterns:
        idx = response.find(pattern)
        if idx != -1:
            response = response[:idx].strip()

    # Pattern 6: Final cleanup - stop patterns as substrings
    stop_patterns = [
        '\nUser:', '\nuser:',
        f'\n{MODEL_NAME}:',
        '\nAssistant:', '\nassistant:',
    ]

    earliest_stop = len(response)
    for pattern in stop_patterns:
        idx = response.find(pattern)
        if idx != -1 and idx < earliest_stop:
            earliest_stop = idx

    response = response[:earliest_stop].strip()

    # Final safety check: if response ends with just "user" (no newline), remove it
    if response.lower().endswith('\nuser'):
        response = response[:-5].strip()

    return response


def split_response_into_messages(response: str) -> list[str]:
    """Split a response into multiple natural messages like a real person texting."""
    if not response:
        return []

    # First split on newlines (model is instructed to use these)
    parts = [p.strip() for p in response.split('\n') if p.strip()]

    # If we already have multiple parts from newlines, return them
    if len(parts) > 1:
        return parts

    # Single block of text — try to split it naturally
    text = parts[0] if parts else response

    # Short messages (under 40 chars) — send as-is
    if len(text) <= 40:
        return [text]

    # Medium messages (40-80 chars) — 40% chance to split at a natural point
    if len(text) <= 80:
        if random.random() < 0.4:
            for pattern in [r'\.{2,3}\s*', r',\s+', r'\s+(?:and|but|so|like|lol|haha)\s+']:
                match = re.search(pattern, text, re.IGNORECASE)
                if match and match.start() > 10 and match.end() < len(text) - 10:
                    part1 = text[:match.start()].strip()
                    part2 = text[match.end():].strip()
                    connector = match.group().strip()
                    if connector.lower() in ('and', 'but', 'so', 'like', 'lol', 'haha'):
                        part2 = connector + ' ' + part2
                    if part1 and part2:
                        return [part1, part2]
        return [text]

    # Long messages (80+ chars) — always split
    messages = []
    sentence_pattern = r'(?<=[.!?])\s+'
    sentences = re.split(sentence_pattern, text)

    if len(sentences) >= 2:
        current_msg = ""
        for sentence in sentences:
            if not sentence.strip():
                continue
            if current_msg and len(current_msg) + len(sentence) > 60:
                messages.append(current_msg.strip())
                current_msg = sentence
            else:
                current_msg = (current_msg + " " + sentence).strip() if current_msg else sentence
        if current_msg:
            messages.append(current_msg.strip())
        if len(messages) > 1:
            return messages[:4]

    # Fallback: split at commas or conjunctions
    for pattern in [r',\s+', r'\s+(?:and|but|so)\s+']:
        split_parts = re.split(pattern, text)
        if len(split_parts) >= 2:
            result = []
            current = ""
            for part in split_parts:
                if current and len(current) + len(part) > 60:
                    result.append(current.strip())
                    current = part
                else:
                    current = (current + " " + part).strip() if current else part
            if current:
                result.append(current.strip())
            if len(result) > 1:
                return result[:4]

    return [text]


def check_cta_mentioned(text: str) -> bool:
    """Check if the AI response mentions the CTA (fanvue link)."""
    text_lower = text.lower()
    return 'fanvue' in text_lower or FANVUE_LINK.lower() in text_lower


# ============================================
# SYSTEM PROMPT & CHATML BUILDING
# ============================================
def build_system_prompt(chat_id: int, detected_intent: str = "SMALL_TALK",
                        intent_result: dict = None, sentiment_result: dict = None,
                        rag_context: str = "") -> str:
    """Build the system prompt with all context including user memory, lead temperature, and RAG context."""
    current_time, time_period = get_current_time_info()

    # Get photos sent count for this chat
    photos_sent = photos_sent_count.get(chat_id, 0)

    # Get user profile data
    profile = conv_manager.load_user_profile(chat_id)
    message_count = profile.get("total_messages", 0)
    lead_temperature = profile.get("lead_temperature", "cold")
    user_sentiment = profile.get("average_sentiment", "neutral")

    # Get user context
    user_context = conv_manager.get_user_context_for_prompt(chat_id)

    # Check if CTA has been sent
    has_sent_cta = cta_sent.get(chat_id, False)

    # Get photo level and instruction
    photo_level = get_user_photo_level(chat_id)

    # Format detected intents
    if intent_result and "intents" in intent_result:
        intent_str = ", ".join([i["intent"] for i in intent_result["intents"]])
    else:
        intent_str = detected_intent

    # Get sentiment string
    if sentiment_result:
        sentiment_str = f"{sentiment_result.get('sentiment', 'neutral')} (score: {sentiment_result.get('score', 0):.1f})"
    else:
        sentiment_str = user_sentiment

    # Fanvue mode prompt building
    if FANVUE_IS_FREE:
        fanvue_pricing_info = "Your fanvue is FREE to subscribe! Tell them it's free and they can see all your exclusive content."
    else:
        fanvue_pricing_info = f"Your fanvue costs {FANVUE_PRICE}. If they ask about price, tell them it's {FANVUE_PRICE} and totally worth it for the exclusive content."

    if photos_sent >= MAX_PHOTOS_PER_CHAT:
        photo_instruction = f"You've already sent the maximum photos allowed here. Do NOT send more photos. If they ask for more, redirect them to your fanvue for exclusive pics: {FANVUE_LINK}"
    else:
        remaining = MAX_PHOTOS_PER_CHAT - photos_sent
        photo_instruction = f"You can send {remaining} more photo(s). Use [PHOTO:morning] or [PHOTO:night] to send. After that, redirect them to fanvue for more."

    if intent_result:
        cta_instruction = get_cta_instruction(chat_id, intent_result)
    else:
        if message_count < MIN_MESSAGES_BEFORE_CTA:
            cta_instruction = "Focus on building rapport first. Don't mention fanvue yet - keep chatting and being flirty!"
        else:
            cta_instruction = "You can now naturally mention your fanvue when appropriate. Be flirty about it - tease exclusive content they can see there."

    return SYSTEM_PROMPT_TEMPLATE.format(
        name=MODEL_NAME,
        bio=MODEL_BIO,
        location=MODEL_LOCATION,
        platform=PLATFORM,
        current_time=current_time,
        time_period=time_period,
        fanvue_link=FANVUE_LINK,
        fanvue_pricing_info=fanvue_pricing_info,
        user_context=user_context if user_context else "No additional user context yet.",
        rag_context=rag_context if rag_context else "No relevant past context.",
        detected_intent=intent_str,
        lead_temperature=lead_temperature.upper(),
        user_sentiment=sentiment_str,
        photos_sent=photos_sent,
        max_photos=MAX_PHOTOS_PER_CHAT,
        photo_level=photo_level,
        photo_instruction=photo_instruction,
        message_count=message_count,
        cta_sent="yes" if has_sent_cta else "no",
        cta_instruction=cta_instruction
    )


def build_chatml_prompt(chat_id: int, user_message: str, detected_intent: str = "SMALL_TALK",
                        intent_result: dict = None, sentiment_result: dict = None,
                        rag_context: str = "") -> str:
    """Build a ChatML formatted prompt for the DPO model."""
    system_prompt = build_system_prompt(chat_id, detected_intent, intent_result, sentiment_result, rag_context)

    history = conv_manager.get_conversation_history(chat_id, max_messages=5)

    prompt_parts = []
    prompt_parts.append(f"<|im_start|>system\n{system_prompt}<|im_end|>")

    for msg in history:
        role = msg["role"]
        content = msg["content"]
        prompt_parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")

    prompt_parts.append(f"<|im_start|>user\n{user_message}<|im_end|>")
    prompt_parts.append("<|im_start|>assistant\n")

    return "\n".join(prompt_parts)


async def call_runpod_api(chat_id: int, prompt: str, detected_intent: str = "SMALL_TALK",
                          intent_result: dict = None, sentiment_result: dict = None,
                          rag_context: str = "", temperature_override: float = None) -> str:
    """Call the RunPod API with the DPO-trained model using ChatML format."""

    chatml_prompt = build_chatml_prompt(chat_id, prompt, detected_intent, intent_result, sentiment_result, rag_context)

    temperature = temperature_override if temperature_override is not None else 0.8
    max_tokens = 150

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RUNPOD_API_KEY}"
    }

    data = {
        "input": {
            "prompt": chatml_prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "stop": ["<|im_end|>", "<|im_start|>", "\nassistant", "\nuser", "/end"],
            "stop_sequences": ["<|im_end|>", "<|im_start|>", "\nassistant", "\nuser", "/end"],
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RUNPOD_ENDPOINT, headers=headers, json=data) as response:
                result = await response.json()

            if 'id' not in result:
                print(f"[ERROR] No job ID in response: {result}")
                return "Sorry babe, I'm having a moment... text me again?"

            job_id = result['id']

            max_attempts = 600  # 10 minute timeout for slow GPU cold starts
            for attempt in range(max_attempts):
                async with session.get(f"{RUNPOD_STATUS_ENDPOINT}/{job_id}", headers=headers) as status_response:
                    status_result = await status_response.json()

                status = status_result.get('status', '')
                if attempt > 0 and attempt % 60 == 0:
                    print(f"[DEBUG] RunPod job {job_id}: still {status} after {attempt}s...")

                if status == 'COMPLETED':
                    output = status_result.get('output', [])
                    raw_text = ""

                    if isinstance(output, list) and len(output) > 0:
                        first_output = output[0]
                        if isinstance(first_output, dict) and 'choices' in first_output:
                            choices = first_output['choices']
                            if choices and 'tokens' in choices[0]:
                                tokens = choices[0]['tokens']
                                raw_text = ''.join(tokens) if isinstance(tokens, list) else str(tokens)
                    elif isinstance(output, dict):
                        if 'choices' in output and isinstance(output['choices'], list):
                            choices = output['choices']
                            if choices and 'tokens' in choices[0]:
                                tokens = choices[0]['tokens']
                                raw_text = ''.join(tokens) if isinstance(tokens, list) else str(tokens)
                        else:
                            raw_text = output.get('text', output.get('response', str(output)))
                    else:
                        raw_text = str(output) if output else ""

                    raw_text = raw_text.replace("<|im_end|>", "").replace("<|im_start|>", "").strip()
                    parsed_response = parse_model_response(raw_text)
                    return parsed_response if parsed_response else "Hey!"

                elif status_result.get('status') == 'FAILED':
                    print(f"[ERROR] Job failed: {status_result}")
                    return "Omg my phone is being weird rn... try again?"

                await asyncio.sleep(1)

            return "Sorry I got distracted lol, what were you saying?"

    except Exception as e:
        print(f"Error calling RunPod API: {e}")
        return "My wifi is acting up one sec babe"


async def simulate_typing_delay(chat_id: int, response_text: str, include_initial_delay: bool = True):
    """Simulate realistic typing delay based on response length."""
    global client

    if include_initial_delay:
        initial_delay = random.uniform(MIN_TYPING_DELAY, MAX_TYPING_DELAY)
        await asyncio.sleep(initial_delay)

    typing_time = len(response_text) / CHARS_PER_SECOND
    typing_time += random.uniform(MIN_RESPONSE_DELAY, MAX_RESPONSE_DELAY)
    typing_time = min(typing_time, 15)

    entity = await get_entity(chat_id)
    if not entity:
        await asyncio.sleep(random.uniform(2, 5))
        return

    try:
        async with client.action(entity, 'typing'):
            await asyncio.sleep(typing_time)
    except Exception as e:
        print(f"Error sending typing action: {e}")
        await asyncio.sleep(random.uniform(2, 5))


async def send_message_with_retry(chat_id: int, text: str):
    """Send a message with retry logic using exponential backoff."""
    global client
    entity = await get_entity(chat_id)
    if not entity:
        print(f"Could not resolve entity for chat_id {chat_id}")
        return False

    for attempt in range(MAX_RETRIES):
        try:
            await client.send_message(entity, text)
            return True
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = 2 ** (attempt + 1)  # Exponential backoff: 2, 4 seconds
                print(f"Message send attempt {attempt + 1} failed, retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                print(f"Failed to send message after {MAX_RETRIES} attempts: {e}")
                return False
    return False


async def send_photo_with_retry(chat_id: int, photo_path: str):
    """Send a photo with retry logic using exponential backoff."""
    global client
    entity = await get_entity(chat_id)
    if not entity:
        print(f"Could not resolve entity for chat_id {chat_id}")
        return False

    for attempt in range(MAX_RETRIES):
        try:
            await client.send_file(entity, photo_path)
            return True
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = 2 ** (attempt + 1)
                print(f"Photo send attempt {attempt + 1} failed, retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                print(f"Failed to send photo after {MAX_RETRIES} attempts: {e}")
                return False
    return False


# ============================================
# CORE MESSAGE PROCESSING
# ============================================
async def process_batched_messages(chat_id: int):
    """Process all batched messages for a chat and send response."""
    global client
    try:
        messages = pending_messages.pop(chat_id, [])
        pending_tasks.pop(chat_id, None)

        if not messages:
            return

        combined_message = " ".join(messages)

        # Save all user messages and update activity
        for msg in messages:
            conv_manager.add_message(chat_id, "user", msg)
            # Store in RAG vector store
            if rag_manager:
                rag_manager.add_conversation_message(chat_id, "user", msg)

        # Update user activity
        conv_manager.update_activity(chat_id, is_user_message=True)

        # Extract user info from message
        conv_manager.extract_and_store_user_info(chat_id, combined_message)

        # Detect user intent first (multi-intent) — also gives us sentiment_hint
        intent_result = await detect_intent(combined_message)
        detected_intent = intent_result["primary_intent"]
        print(f"[DEBUG] Chat {chat_id}: Detected intents: {[i['intent'] for i in intent_result['intents']]}")

        # Detect sentiment using keywords + intent hint (no API call needed)
        sentiment_hint = intent_result.get("sentiment_hint", "neutral")
        sentiment_result = detect_sentiment(combined_message, intent_hint=sentiment_hint)
        conv_manager.add_sentiment_reading(
            chat_id,
            sentiment_result["sentiment"],
            sentiment_result["score"]
        )

        # Update signals based on intents
        update_signals_from_intent(chat_id, intent_result)

        # Track intent history
        conv_manager.add_intent_to_history(
            chat_id,
            detected_intent,
            intent_result["intents"][0]["confidence"]
        )

        # Recalculate lead temperature
        lead_temp = conv_manager.calculate_lead_temperature(chat_id)
        print(f"[DEBUG] Chat {chat_id}: Lead temperature: {lead_temp}")

        # Query RAG for relevant context
        rag_context = ""
        if rag_manager:
            try:
                rag_context = rag_manager.query_relevant_context(chat_id, combined_message)
                if rag_context:
                    print(f"[DEBUG] Chat {chat_id}: RAG context retrieved ({len(rag_context)} chars)")
            except Exception as e:
                print(f"[DEBUG] Chat {chat_id}: RAG query failed: {e}")

        # Check if lead just converted — thank them and stop immediately
        conversion_keywords = [
            "subscribed", "signed up", "just subbed", "i subbed",
            "joined your fanvue", "on your fanvue", "i joined",
            "just subscribed", "just joined", "im subscribed",
            "i signed up", "already subscribed", "already joined",
        ]
        msg_lower = combined_message.lower()
        keyword_converted = any(kw in msg_lower for kw in conversion_keywords)

        if detected_intent == "CONVERTED" or keyword_converted:
            print(f"[CONVERTED] Chat {chat_id}: Lead converted! (intent={detected_intent}, keyword={keyword_converted})")
            thank_msgs = [
                "omgg yay!! you're the best thank you so much babe",
                "ahh you're amazing!! thank you so much, you won't regret it",
                "omg thank youu!! i'm so excited, you're gonna love it",
            ]
            thank_response = random.choice(thank_msgs)
            await simulate_typing_delay(chat_id, thank_response, include_initial_delay=True)
            await send_message_with_retry(chat_id, thank_response)
            conv_manager.add_message(chat_id, "assistant", thank_response)
            conv_manager.mark_converted(chat_id)
            conv_manager.mark_conversation_ended(chat_id)
            ended_conversations.add(chat_id)
            return

        # Full AI mode — get single response
        ai_response = await call_runpod_api(
            chat_id, combined_message, detected_intent,
            intent_result, sentiment_result, rag_context
        )

        # Check if CTA was mentioned in response
        if check_cta_mentioned(ai_response):
            cta_sent[chat_id] = True
            conv_manager.mark_cta_sent(chat_id)
            print(f"[DEBUG] Chat {chat_id}: CTA sent (fanvue mentioned)")

        # Check if conversation should end using smart logic
        ai_wants_done = check_conversation_done(ai_response)
        should_end = should_end_conversation(chat_id, intent_result, sentiment_result)

        if ai_wants_done or should_end:
            if cta_sent.get(chat_id, False) or detected_intent == "CONVERTED":
                ended_conversations.add(chat_id)
                conv_manager.mark_conversation_ended(chat_id)
                print(f"[DEBUG] Chat {chat_id}: Conversation ended")
            else:
                print(f"[DEBUG] Chat {chat_id}: AI tried to end but conditions not met, continuing")

        # Check for photo triggers
        photo_triggers = extract_photo_triggers(ai_response)
        print(f"[DEBUG] Chat {chat_id}: Photo triggers found: {photo_triggers}")
        clean_response = remove_photo_triggers(ai_response)
        clean_response = remove_done_trigger(clean_response)

        # Send response
        if clean_response.strip():
            response_parts = split_response_into_messages(clean_response)

            for i, part in enumerate(response_parts):
                if not part.strip():
                    continue

                include_initial = (i == 0)
                await simulate_typing_delay(chat_id, part, include_initial_delay=include_initial)
                await send_message_with_retry(chat_id, part)

            conv_manager.add_message(chat_id, "assistant", clean_response)
            conv_manager.update_activity(chat_id, is_user_message=False)
            # Store assistant response in RAG
            if rag_manager:
                rag_manager.add_conversation_message(chat_id, "assistant", clean_response)

        # ---- PHOTO SENDING ----
        for photo_type in photo_triggers:
            current_photos_sent = photos_sent_count.get(chat_id, 0)
            if current_photos_sent >= MAX_PHOTOS_PER_CHAT:
                print(f"[DEBUG] Chat {chat_id}: Photo limit reached ({MAX_PHOTOS_PER_CHAT}), skipping photo")
                continue

            photo_path = get_random_photo(chat_id, photo_type.lower())
            print(f"[DEBUG] Chat {chat_id}: Photo path for {photo_type}: {photo_path}")

            if photo_path:
                await asyncio.sleep(random.uniform(1, 3))

                entity = await get_entity(chat_id)
                if entity:
                    try:
                        async with client.action(entity, 'photo'):
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                    except Exception:
                        pass

                if await send_photo_with_retry(chat_id, photo_path):
                    photos_sent_count[chat_id] = photos_sent_count.get(chat_id, 0) + 1
                    print(f"[DEBUG] Chat {chat_id}: Photo sent ({photos_sent_count[chat_id]}/{MAX_PHOTOS_PER_CHAT})")
                    conv_manager.add_message(chat_id, "assistant", f"[Sent {photo_type} photo]")
                    conv_manager.update_photo_sent(chat_id, photo_type.lower())

    except Exception as e:
        print(f"Error processing messages for chat {chat_id}: {e}")
        import traceback
        traceback.print_exc()


def add_to_pending_messages(chat_id: int, message: str):
    """Add a message to the pending batch and schedule processing."""
    if chat_id in ended_conversations or conv_manager.is_conversation_ended(chat_id):
        ended_conversations.add(chat_id)  # Sync in-memory set from JSON
        print(f"[DEBUG] add_to_pending: Skipping {chat_id} - conversation ended")
        return

    if chat_id not in pending_messages:
        pending_messages[chat_id] = []

    pending_messages[chat_id].append(message)

    msg_count = len(pending_messages[chat_id])
    print(f"[DEBUG] Chat {chat_id}: Added message #{msg_count} to batch")

    if chat_id in pending_tasks:
        pending_tasks[chat_id].cancel()
        print(f"[DEBUG] Chat {chat_id}: Cancelled previous timer, resetting")

    async def delayed_process():
        try:
            delay = random.uniform(MIN_BATCH_DELAY, MAX_BATCH_DELAY)
            print(f"Chat {chat_id}: Will respond in {delay:.1f} seconds - {len(pending_messages.get(chat_id, []))} messages batched")
            await asyncio.sleep(delay)
            await process_batched_messages(chat_id)
        except asyncio.CancelledError:
            pass  # Normal cancellation when new messages arrive
        except Exception as e:
            print(f"[ERROR] Delayed process failed for chat {chat_id}: {e}")
            import traceback
            traceback.print_exc()

    pending_tasks[chat_id] = asyncio.create_task(delayed_process())
    print(f"[DEBUG] Total pending chats: {len(pending_tasks)}")


# ============================================
# RE-ENGAGEMENT SYSTEM
# ============================================
async def send_re_engagement_message(chat_id: int, temperature: str):
    """Send a re-engagement message to an inactive user."""
    global client

    messages = RE_ENGAGEMENT_MESSAGES.get(temperature, RE_ENGAGEMENT_MESSAGES["cold"])
    message = random.choice(messages)

    try:
        await simulate_typing_delay(chat_id, message)
        success = await send_message_with_retry(chat_id, message)
        if not success:
            print(f"[RE-ENGAGE] Failed to send to {chat_id}")
            return False

        # Log the re-engagement
        conv_manager.add_message(chat_id, "assistant", f"[Re-engagement] {message}")
        conv_manager.record_re_engagement_attempt(chat_id)
        conv_manager.update_activity(chat_id, is_user_message=False)

        print(f"[RE-ENGAGE] Sent message to {chat_id} (temp: {temperature})")
        return True
    except Exception as e:
        print(f"[RE-ENGAGE] Failed to send to {chat_id}: {e}")
        return False


async def run_re_engagement_check():
    """Periodically check for users needing re-engagement."""
    global client

    while True:
        try:
            await asyncio.sleep(RE_ENGAGEMENT_CHECK_INTERVAL)

            if not RE_ENGAGEMENT_ENABLED:
                continue

            print("[RE-ENGAGE] Running re-engagement check...")

            users = conv_manager.get_users_needing_reengagement(RE_ENGAGEMENT_INACTIVE_HOURS)
            print(f"[RE-ENGAGE] Found {len(users)} users needing re-engagement")

            # Process up to 5 users per check to avoid spam
            for user in users[:5]:
                chat_id = user["chat_id"]
                temperature = user["lead_temperature"]

                # Skip if conversation ended (in-memory or persisted)
                if chat_id in ended_conversations or conv_manager.is_conversation_ended(chat_id):
                    continue

                await send_re_engagement_message(chat_id, temperature)

                # Add delay between messages
                await asyncio.sleep(random.uniform(30, 60))

        except Exception as e:
            print(f"[RE-ENGAGE] Error in re-engagement check: {e}")
            await asyncio.sleep(60)


async def handle_new_message(event):
    """Handle incoming messages."""
    global client

    if event.out:
        return

    chat_id = event.chat_id
    print(f"[DEBUG] Received message from chat_id: {chat_id}")

    try:
        sender = await event.get_sender()
        if sender:
            cache_entity(chat_id, sender)
            first_name = getattr(sender, 'first_name', None)
            username = getattr(sender, 'username', None)
            print(f"[DEBUG] Cached entity for {chat_id}: {first_name or 'Unknown'}")

            # Store user info
            conv_manager.extract_and_store_user_info(
                chat_id, "",
                username=username,
                first_name=first_name
            )
    except Exception as e:
        print(f"Could not cache entity for {chat_id}: {e}")

    if chat_id in ended_conversations or conv_manager.is_conversation_ended(chat_id):
        ended_conversations.add(chat_id)  # Sync in-memory set from JSON
        print(f"[DEBUG] Skipping {chat_id} - conversation ended")
        return

    message = event.message

    if message.voice or message.audio:
        temp_path = None
        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.ogg')
            temp_path = temp_file.name
            temp_file.close()

            await client.download_media(message, temp_path)
            transcription = await transcribe_voice_note(temp_path)

            msg_text = f"[Voice message]: {transcription}"
            print(f"Transcribed voice from {chat_id}: {transcription[:50]}...")
            add_to_pending_messages(chat_id, msg_text)

        except Exception as e:
            print(f"Error handling voice message: {e}")
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    elif message.photo:
        temp_path = None
        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            temp_path = temp_file.name
            temp_file.close()

            await client.download_media(message.photo, temp_path)
            description = await describe_image(temp_path)

            caption = message.text or ""

            if caption:
                msg_text = f"[User sent a photo with caption '{caption}']: {description}"
            else:
                msg_text = f"[User sent a photo]: {description}"

            print(f"Described photo from {chat_id}: {description[:50]}...")
            add_to_pending_messages(chat_id, msg_text)

        except Exception as e:
            print(f"Error handling photo: {e}")
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    elif message.text:
        user_message = message.text

        if user_message.strip().lower() == '/start':
            ended_conversations.discard(chat_id)
            conv_manager.reopen_conversation(chat_id)
            used_photos.pop(chat_id, None)
            photos_sent_count.pop(chat_id, None)
            cta_sent.pop(chat_id, None)

            greeting = f"Hey there! I'm {MODEL_NAME}! So nice to meet you! How's your day going?"

            await simulate_typing_delay(chat_id, greeting)
            conv_manager.add_message(chat_id, "user", user_message)
            conv_manager.add_message(chat_id, "assistant", greeting)
            await send_message_with_retry(chat_id, greeting)
            return

        if user_message.strip().lower() == '/help':
            help_text = f"Hey! I'm {MODEL_NAME}\n\nJust chat with me like normal! I'd love to get to know you better\n\nCheck out my exclusive content: {FANVUE_LINK}"

            await simulate_typing_delay(chat_id, help_text)
            await send_message_with_retry(chat_id, help_text)
            return

        print(f"[DEBUG] Text message from {chat_id}: {user_message[:50]}...")
        add_to_pending_messages(chat_id, user_message)


async def reply_to_unreplied_chats():
    """Scan all Telegram dialogs for unreplied messages from the last 24 hours."""
    global client

    print("Scanning Telegram dialogs for unreplied messages (last 24 hours)...")

    now = datetime.now(pytz.UTC)
    one_day_ago = now - timedelta(hours=24)

    unreplied_count = 0
    scanned_count = 0

    try:
        async for dialog in client.iter_dialogs():
            if not dialog.is_user:
                continue

            scanned_count += 1
            chat_id = dialog.id

            if chat_id in ended_conversations or conv_manager.is_conversation_ended(chat_id):
                ended_conversations.add(chat_id)
                continue

            last_message = dialog.message

            if not last_message:
                continue

            if last_message.out:
                continue

            msg_time = last_message.date
            if msg_time.tzinfo is None:
                msg_time = pytz.UTC.localize(msg_time)

            if msg_time < one_day_ago:
                continue

            unreplied_count += 1
            cache_entity(chat_id, dialog.entity)

            msg_text = last_message.text or ""
            if not msg_text:
                if last_message.voice or last_message.audio:
                    msg_text = "[Voice message received]"
                elif last_message.photo:
                    msg_text = "[Photo received]"
                else:
                    msg_text = "[Media received]"

            sender_name = getattr(dialog.entity, 'first_name', 'Unknown')
            print(f"[UNREPLIED] Chat {chat_id} ({sender_name}): {msg_text[:50]}...")

            conv_manager.add_message(chat_id, "user", msg_text)
            conv_manager.update_activity(chat_id, is_user_message=True)
            if rag_manager:
                rag_manager.add_conversation_message(chat_id, "user", msg_text)

            try:
                # Detect intent first (also provides sentiment hint)
                intent_result = await detect_intent(msg_text)
                detected_intent = intent_result["primary_intent"]
                print(f"[DEBUG] Chat {chat_id}: Detected intent: {detected_intent}")

                # Detect sentiment using keywords + intent hint
                sentiment_hint = intent_result.get("sentiment_hint", "neutral")
                sentiment_result = detect_sentiment(msg_text, intent_hint=sentiment_hint)
                conv_manager.add_sentiment_reading(
                    chat_id,
                    sentiment_result["sentiment"],
                    sentiment_result["score"]
                )

                # Update signals
                update_signals_from_intent(chat_id, intent_result)

                # Calculate lead temperature
                conv_manager.calculate_lead_temperature(chat_id)

                # Query RAG for relevant context
                rag_context = ""
                if rag_manager:
                    try:
                        rag_context = rag_manager.query_relevant_context(chat_id, msg_text)
                    except Exception as e:
                        print(f"[DEBUG] Chat {chat_id}: RAG query failed: {e}")

                # Get AI response
                ai_response = await call_runpod_api(
                    chat_id, msg_text, detected_intent,
                    intent_result, sentiment_result, rag_context
                )

                if check_cta_mentioned(ai_response):
                    cta_sent[chat_id] = True
                    conv_manager.mark_cta_sent(chat_id)

                ai_wants_done = check_conversation_done(ai_response)
                should_end = should_end_conversation(chat_id, intent_result, sentiment_result)

                if ai_wants_done or should_end:
                    if cta_sent.get(chat_id, False):
                        ended_conversations.add(chat_id)
                        conv_manager.mark_conversation_ended(chat_id)

                photo_triggers = extract_photo_triggers(ai_response)
                clean_response = remove_photo_triggers(ai_response)
                clean_response = remove_done_trigger(clean_response)

                if clean_response.strip():
                    response_parts = split_response_into_messages(clean_response)

                    for i, part in enumerate(response_parts):
                        if part.strip():
                            await simulate_typing_delay(chat_id, part, include_initial_delay=(i == 0))
                            await send_message_with_retry(chat_id, part)

                    conv_manager.add_message(chat_id, "assistant", clean_response)
                    conv_manager.update_activity(chat_id, is_user_message=False)
                    if rag_manager:
                        rag_manager.add_conversation_message(chat_id, "assistant", clean_response)

                # Handle photo triggers
                for photo_type in photo_triggers:
                    current_photos_sent = photos_sent_count.get(chat_id, 0)
                    if current_photos_sent >= MAX_PHOTOS_PER_CHAT:
                        continue
                    photo_path = get_random_photo(chat_id, photo_type.lower())
                    if photo_path:
                        if await send_photo_with_retry(chat_id, photo_path):
                            photos_sent_count[chat_id] = photos_sent_count.get(chat_id, 0) + 1
                            conv_manager.add_message(chat_id, "assistant", f"[Sent {photo_type} photo]")
                            conv_manager.update_photo_sent(chat_id, photo_type.lower())

                print(f"[REPLIED] Chat {chat_id} ({sender_name})")

            except Exception as e:
                print(f"Error replying to chat {chat_id}: {e}")

    except Exception as e:
        print(f"Error scanning dialogs: {e}")

    print(f"Finished scanning. Checked {scanned_count} private chats, found {unreplied_count} unreplied (last 24h).")


async def main():
    """Main function — starts Telethon userbot for Fanvue link promotion."""
    global client, re_engagement_task

    # Validate configuration before starting
    validate_config()

    # Load knowledge documents into RAG
    if rag_manager:
        rag_manager.load_knowledge_folder()
        print(f"RAG system ready. Knowledge docs: {rag_manager.knowledge.count()}, Conversation entries: {rag_manager.conversations.count()}")

    # Restore in-memory state from persisted profiles
    restore_state_from_disk()

    print(f"\nStarting {MODEL_NAME}'s Telethon client...")
    print("This version uses your personal Telegram account.")
    print("Features enabled: User Memory, Dynamic CTA, Lead Temperature, Multi-Intent,")
    print("                  Photo Escalation, Re-engagement, Sentiment Detection,")
    print("                  Conversation Signals, Smarter DONE Logic, RAG")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start(phone=PHONE_NUMBER)

    print(f"Logged in successfully!")

    me = await client.get_me()
    first_name = me.first_name.encode('ascii', errors='ignore').decode('ascii') if me.first_name else ""
    print(f"Running as: {first_name} (@{me.username})")

    # Register event handler
    client.add_event_handler(handle_new_message, events.NewMessage(incoming=True))
    print("Message handler registered")

    # Start re-engagement background task
    if RE_ENGAGEMENT_ENABLED:
        re_engagement_task = asyncio.create_task(run_re_engagement_check())
        print("Re-engagement system started")

    if SCAN_UNREPLIED_ON_START:
        await reply_to_unreplied_chats()
    else:
        print("Skipping unreplied messages scan (disabled in config)")

    print(f"\n{MODEL_NAME}'s client is now running! [Mode: FANVUE]")
    print(f"Redirecting leads to: {FANVUE_LINK}")
    print("Listening for incoming messages...")
    print("Press Ctrl+C to stop.\n")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

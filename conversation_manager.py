import json
import os
from datetime import datetime, timedelta
import re
from config import CONVERSATIONS_FOLDER, MAX_RE_ENGAGEMENT_ATTEMPTS

class ConversationManager:
    def __init__(self):
        self.conversations_folder = CONVERSATIONS_FOLDER
        self.user_profiles_folder = os.path.join(CONVERSATIONS_FOLDER, "user_profiles")
        os.makedirs(self.conversations_folder, exist_ok=True)
        os.makedirs(self.user_profiles_folder, exist_ok=True)

    def _get_conversation_path(self, chat_id: int) -> str:
        """Get the file path for a specific chat's conversation history."""
        return os.path.join(self.conversations_folder, f"chat_{chat_id}.json")

    def _get_user_profile_path(self, chat_id: int) -> str:
        """Get the file path for a user's profile."""
        return os.path.join(self.user_profiles_folder, f"user_{chat_id}.json")

    # ==========================================
    # USER MEMORY SYSTEM
    # ==========================================

    def load_user_profile(self, chat_id: int) -> dict:
        """Load or create a user profile with memory and engagement data."""
        filepath = self._get_user_profile_path(chat_id)

        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return self._create_new_user_profile(chat_id)

        return self._create_new_user_profile(chat_id)

    def _create_new_user_profile(self, chat_id: int) -> dict:
        """Create a new user profile structure."""
        return {
            "chat_id": chat_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),

            # Basic info (extracted from conversation)
            "username": None,
            "first_name": None,
            "extracted_info": {
                "name": None,
                "age": None,
                "location": None,
                "occupation": None,
                "interests": [],
                "mentioned_topics": []
            },

            # Lead Temperature & Scoring
            "lead_temperature": "cold",  # cold, warm, hot
            "engagement_score": 0,
            "interest_level": 0,  # 0-100

            # Conversation Signals
            "signals": {
                "compliments_given": 0,
                "photos_requested": 0,
                "content_requests": 0,
                "price_inquiries": 0,
                "objections_raised": 0,
                "positive_responses": 0,
                "negative_responses": 0,
                "questions_asked": 0,
                "flirty_messages": 0,
                "ai_accusations": 0,
                "platform_move_attempts": 0,
                "boundary_tests": 0
            },

            # Sentiment Tracking
            "sentiment_history": [],  # List of {timestamp, sentiment, score}
            "average_sentiment": "neutral",  # negative, neutral, positive
            "sentiment_trend": "stable",  # declining, stable, improving

            # Engagement Metrics
            "total_messages": 0,
            "total_sessions": 1,
            "avg_message_length": 0,
            "avg_response_time": 0,  # seconds
            "longest_session": 0,  # messages
            "last_active": datetime.now().isoformat(),
            "first_message_date": datetime.now().isoformat(),

            # Photo Tracking
            "photos_sent": 0,
            "photos_by_category": {
                "morning": 0,
                "night": 0,
                "casual": 0,
                "flirty": 0,
                "spicy": 0
            },
            "photo_reactions": [],  # Track how user reacted to photos

            # CTA & Conversion Tracking
            "cta_sent": False,
            "cta_sent_at": None,
            "cta_mentions": 0,
            "fanvue_clicks": 0,  # If trackable
            "converted": False,
            "conversion_date": None,

            # Re-engagement Data
            "last_message_from_user": None,
            "last_message_from_bot": None,
            "inactive_days": 0,
            "re_engagement_attempts": 0,
            "last_re_engagement": None,

            # Objection Tracking
            "objections": [],  # List of {type, timestamp, handled}

            # Intent History
            "intent_history": [],  # Last N intents detected

            # Conversation Quality
            "conversation_quality": "normal",  # low, normal, high
            "is_time_waster": False,
            "is_blocked": False,
            "conversation_ended": False,
            "conversation_ended_at": None
        }

    def save_user_profile(self, chat_id: int, profile: dict) -> None:
        """Save user profile to file atomically (write to temp then rename)."""
        filepath = self._get_user_profile_path(chat_id)
        profile["updated_at"] = datetime.now().isoformat()

        try:
            tmp_path = filepath + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, filepath)
        except Exception as e:
            print(f"[ERROR] Failed to save user profile for {chat_id}: {e}")
            # Clean up temp file if it exists
            try:
                if os.path.exists(filepath + ".tmp"):
                    os.remove(filepath + ".tmp")
            except Exception:
                pass

    def update_user_signal(self, chat_id: int, signal_type: str, increment: int = 1) -> dict:
        """Update a specific signal counter for a user."""
        profile = self.load_user_profile(chat_id)

        if signal_type in profile["signals"]:
            profile["signals"][signal_type] += increment

        self.save_user_profile(chat_id, profile)
        return profile

    def add_sentiment_reading(self, chat_id: int, sentiment: str, score: float) -> dict:
        """Add a sentiment reading to user's history."""
        profile = self.load_user_profile(chat_id)

        reading = {
            "timestamp": datetime.now().isoformat(),
            "sentiment": sentiment,
            "score": score
        }

        profile["sentiment_history"].append(reading)

        # Keep only last 50 readings
        if len(profile["sentiment_history"]) > 50:
            profile["sentiment_history"] = profile["sentiment_history"][-50:]

        # Update average sentiment and trend
        profile["average_sentiment"] = self._calculate_average_sentiment(profile["sentiment_history"])
        profile["sentiment_trend"] = self._calculate_sentiment_trend(profile["sentiment_history"])

        self.save_user_profile(chat_id, profile)
        return profile

    def _calculate_average_sentiment(self, history: list) -> str:
        """Calculate average sentiment from history."""
        if not history:
            return "neutral"

        recent = history[-10:]  # Last 10 readings
        scores = [r.get("score", 0) for r in recent]
        avg = sum(scores) / len(scores)

        if avg > 0.3:
            return "positive"
        elif avg < -0.3:
            return "negative"
        return "neutral"

    def _calculate_sentiment_trend(self, history: list) -> str:
        """Calculate if sentiment is improving, declining, or stable."""
        if len(history) < 5:
            return "stable"

        recent = history[-5:]
        older = history[-10:-5] if len(history) >= 10 else history[:5]

        recent_avg = sum(r.get("score", 0) for r in recent) / len(recent)
        older_avg = sum(r.get("score", 0) for r in older) / len(older)

        diff = recent_avg - older_avg

        if diff > 0.2:
            return "improving"
        elif diff < -0.2:
            return "declining"
        return "stable"

    def add_intent_to_history(self, chat_id: int, intent: str, confidence: float) -> dict:
        """Add detected intent to user's history."""
        profile = self.load_user_profile(chat_id)

        entry = {
            "timestamp": datetime.now().isoformat(),
            "intent": intent,
            "confidence": confidence
        }

        profile["intent_history"].append(entry)

        # Keep only last 30 intents
        if len(profile["intent_history"]) > 30:
            profile["intent_history"] = profile["intent_history"][-30:]

        self.save_user_profile(chat_id, profile)
        return profile

    def calculate_lead_temperature(self, chat_id: int) -> str:
        """Calculate lead temperature based on signals and engagement."""
        profile = self.load_user_profile(chat_id)
        signals = profile["signals"]

        # Scoring system
        score = 0

        # Positive signals
        score += signals.get("compliments_given", 0) * 5
        score += signals.get("photos_requested", 0) * 8
        score += signals.get("content_requests", 0) * 10
        score += signals.get("price_inquiries", 0) * 15
        score += signals.get("positive_responses", 0) * 3
        score += signals.get("flirty_messages", 0) * 5
        score += signals.get("questions_asked", 0) * 2

        # Negative signals
        score -= signals.get("objections_raised", 0) * 5
        score -= signals.get("negative_responses", 0) * 3
        score -= signals.get("ai_accusations", 0) * 10
        score -= signals.get("boundary_tests", 0) * 3

        # Sentiment bonus
        if profile.get("average_sentiment") == "positive":
            score += 10
        elif profile.get("average_sentiment") == "negative":
            score -= 10

        # Trend bonus
        if profile.get("sentiment_trend") == "improving":
            score += 5
        elif profile.get("sentiment_trend") == "declining":
            score -= 5

        # Message volume bonus
        total_messages = profile.get("total_messages", 0)
        if total_messages >= 20:
            score += 10
        elif total_messages >= 10:
            score += 5

        # Update profile
        profile["engagement_score"] = score

        # Determine temperature
        if score >= 40:
            temperature = "hot"
        elif score >= 15:
            temperature = "warm"
        else:
            temperature = "cold"

        profile["lead_temperature"] = temperature
        profile["interest_level"] = min(100, max(0, score * 2))  # 0-100 scale

        self.save_user_profile(chat_id, profile)
        return temperature

    def add_objection(self, chat_id: int, objection_type: str) -> dict:
        """Track an objection raised by the user."""
        profile = self.load_user_profile(chat_id)

        objection = {
            "type": objection_type,
            "timestamp": datetime.now().isoformat(),
            "handled": False
        }

        profile["objections"].append(objection)
        profile["signals"]["objections_raised"] += 1

        self.save_user_profile(chat_id, profile)
        return profile

    def mark_objection_handled(self, chat_id: int) -> dict:
        """Mark the most recent objection as handled."""
        profile = self.load_user_profile(chat_id)

        if profile["objections"]:
            profile["objections"][-1]["handled"] = True

        self.save_user_profile(chat_id, profile)
        return profile

    def update_photo_sent(self, chat_id: int, category: str) -> dict:
        """Track a photo being sent to user."""
        profile = self.load_user_profile(chat_id)

        profile["photos_sent"] += 1
        if category in profile["photos_by_category"]:
            profile["photos_by_category"][category] += 1

        self.save_user_profile(chat_id, profile)
        return profile

    def mark_cta_sent(self, chat_id: int) -> dict:
        """Mark that CTA has been sent to this user."""
        profile = self.load_user_profile(chat_id)

        profile["cta_sent"] = True
        profile["cta_sent_at"] = datetime.now().isoformat()
        profile["cta_mentions"] += 1

        self.save_user_profile(chat_id, profile)
        return profile

    def mark_converted(self, chat_id: int) -> dict:
        """Mark user as converted."""
        profile = self.load_user_profile(chat_id)

        profile["converted"] = True
        profile["conversion_date"] = datetime.now().isoformat()
        profile["lead_temperature"] = "hot"

        self.save_user_profile(chat_id, profile)
        return profile

    def mark_conversation_ended(self, chat_id: int) -> dict:
        """Mark a conversation as ended (persists across restarts)."""
        profile = self.load_user_profile(chat_id)
        profile["conversation_ended"] = True
        profile["conversation_ended_at"] = datetime.now().isoformat()
        self.save_user_profile(chat_id, profile)
        return profile

    def reopen_conversation(self, chat_id: int) -> dict:
        """Reopen a previously ended conversation (e.g. on /start)."""
        profile = self.load_user_profile(chat_id)
        profile["conversation_ended"] = False
        profile["conversation_ended_at"] = None
        self.save_user_profile(chat_id, profile)
        return profile

    def is_conversation_ended(self, chat_id: int) -> bool:
        """Check if a conversation has been ended."""
        profile = self.load_user_profile(chat_id)
        return profile.get("conversation_ended", False)

    def get_users_needing_reengagement(self, inactive_hours: int = 24) -> list:
        """Get list of users who haven't messaged in X hours and need re-engagement."""
        users = []
        cutoff = datetime.now() - timedelta(hours=inactive_hours)

        if not os.path.exists(self.user_profiles_folder):
            return users

        for filename in os.listdir(self.user_profiles_folder):
            if not filename.startswith("user_") or not filename.endswith(".json"):
                continue

            try:
                filepath = os.path.join(self.user_profiles_folder, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    profile = json.load(f)

                # Skip converted, blocked, time wasters, or ended conversations
                if profile.get("converted") or profile.get("is_blocked") or profile.get("is_time_waster") or profile.get("conversation_ended"):
                    continue

                # Check last activity
                last_active_str = profile.get("last_active")
                if not last_active_str:
                    continue

                last_active = datetime.fromisoformat(last_active_str)

                if last_active < cutoff:
                    # Check re-engagement attempts
                    re_attempts = profile.get("re_engagement_attempts", 0)
                    if re_attempts < MAX_RE_ENGAGEMENT_ATTEMPTS:
                        users.append({
                            "chat_id": profile["chat_id"],
                            "last_active": last_active,
                            "lead_temperature": profile.get("lead_temperature", "cold"),
                            "re_engagement_attempts": re_attempts,
                            "cta_sent": profile.get("cta_sent", False),
                            "photos_sent": profile.get("photos_sent", 0)
                        })
            except Exception as e:
                print(f"Error loading profile {filename}: {e}")
                continue

        # Sort by lead temperature (hot first) and then by last activity
        temp_order = {"hot": 0, "warm": 1, "cold": 2}
        users.sort(key=lambda x: (temp_order.get(x["lead_temperature"], 2), x["last_active"]))

        return users

    def record_re_engagement_attempt(self, chat_id: int) -> dict:
        """Record that we attempted to re-engage this user."""
        profile = self.load_user_profile(chat_id)

        profile["re_engagement_attempts"] = profile.get("re_engagement_attempts", 0) + 1
        profile["last_re_engagement"] = datetime.now().isoformat()

        self.save_user_profile(chat_id, profile)
        return profile

    def update_activity(self, chat_id: int, is_user_message: bool = True) -> dict:
        """Update user's activity timestamps and counters."""
        profile = self.load_user_profile(chat_id)

        now = datetime.now()
        profile["last_active"] = now.isoformat()

        if is_user_message:
            profile["last_message_from_user"] = now.isoformat()
            profile["total_messages"] = profile.get("total_messages", 0) + 1
            # Reset re-engagement counter when user messages
            profile["re_engagement_attempts"] = 0
            profile["inactive_days"] = 0
        else:
            profile["last_message_from_bot"] = now.isoformat()

        self.save_user_profile(chat_id, profile)
        return profile

    def extract_and_store_user_info(self, chat_id: int, message: str, username: str = None, first_name: str = None) -> dict:
        """Extract and store user information from messages."""
        profile = self.load_user_profile(chat_id)

        # Store basic info if provided
        if username and not profile.get("username"):
            profile["username"] = username
        if first_name and not profile.get("first_name"):
            profile["first_name"] = first_name

        # Simple extraction patterns (can be enhanced with NLP)

        # Try to extract name mentions
        name_patterns = [
            r"(?:my name is|i'm|im|i am|call me)\s+([A-Z][a-z]+)",
            r"(?:name's|names)\s+([A-Z][a-z]+)"
        ]
        for pattern in name_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match and not profile["extracted_info"]["name"]:
                profile["extracted_info"]["name"] = match.group(1).title()

        # Try to extract age
        age_patterns = [
            r"(?:i'm|im|i am)\s+(\d{2})\s*(?:years|yrs|yo)?",
            r"(\d{2})\s*(?:years old|yo|yrs old)"
        ]
        for pattern in age_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match and not profile["extracted_info"]["age"]:
                age = int(match.group(1))
                if 18 <= age <= 99:
                    profile["extracted_info"]["age"] = age

        # Track mentioned topics
        topics_keywords = {
            "gaming": ["game", "gaming", "play", "xbox", "ps5", "pc gaming", "fortnite", "cod"],
            "fitness": ["gym", "workout", "fitness", "training", "lift", "exercise"],
            "music": ["music", "song", "concert", "spotify", "band", "singing"],
            "travel": ["travel", "vacation", "trip", "visit", "country", "city"],
            "movies": ["movie", "film", "netflix", "watch", "cinema", "series"],
            "sports": ["football", "soccer", "basketball", "sports", "team"],
            "work": ["work", "job", "office", "career", "business"],
            "food": ["food", "eating", "restaurant", "cook", "dinner", "lunch"]
        }

        message_lower = message.lower()
        for topic, keywords in topics_keywords.items():
            for keyword in keywords:
                if keyword in message_lower:
                    if topic not in profile["extracted_info"]["mentioned_topics"]:
                        profile["extracted_info"]["mentioned_topics"].append(topic)
                    break

        self.save_user_profile(chat_id, profile)
        return profile

    def get_user_context_for_prompt(self, chat_id: int) -> str:
        """Generate a context string about the user for the system prompt."""
        profile = self.load_user_profile(chat_id)

        context_parts = []

        # Basic info
        if profile.get("first_name"):
            context_parts.append(f"User's name: {profile['first_name']}")

        extracted = profile.get("extracted_info", {})
        if extracted.get("name"):
            context_parts.append(f"They mentioned their name is {extracted['name']}")
        if extracted.get("age"):
            context_parts.append(f"Age: {extracted['age']}")
        if extracted.get("interests"):
            context_parts.append(f"Interests: {', '.join(extracted['interests'])}")
        if extracted.get("mentioned_topics"):
            context_parts.append(f"Topics they've discussed: {', '.join(extracted['mentioned_topics'][:5])}")

        # Lead temperature context
        temp = profile.get("lead_temperature", "cold")
        if temp == "hot":
            context_parts.append("This user is VERY interested (hot lead) - push for conversion!")
        elif temp == "warm":
            context_parts.append("This user is showing interest (warm lead) - nurture them toward fanvue")

        # Sentiment context
        sentiment = profile.get("average_sentiment", "neutral")
        trend = profile.get("sentiment_trend", "stable")
        if sentiment == "positive" and trend == "improving":
            context_parts.append("User is in a great mood and getting more positive!")
        elif sentiment == "negative" or trend == "declining":
            context_parts.append("User seems less engaged - be extra warm and engaging")

        return "\n".join(context_parts) if context_parts else ""

    # ==========================================
    # ORIGINAL CONVERSATION METHODS
    # ==========================================

    def load_conversation(self, chat_id: int) -> dict:
        """Load conversation history for a specific chat."""
        filepath = self._get_conversation_path(chat_id)

        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return self._create_new_conversation(chat_id)

        return self._create_new_conversation(chat_id)

    def _create_new_conversation(self, chat_id: int) -> dict:
        """Create a new conversation structure."""
        return {
            "chat_id": chat_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "messages": []
        }

    def save_conversation(self, chat_id: int, conversation: dict) -> None:
        """Save conversation history to file atomically (write to temp then rename)."""
        filepath = self._get_conversation_path(chat_id)
        conversation["updated_at"] = datetime.now().isoformat()

        try:
            tmp_path = filepath + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(conversation, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, filepath)
        except Exception as e:
            print(f"[ERROR] Failed to save conversation for {chat_id}: {e}")

    def add_message(self, chat_id: int, role: str, content: str, username: str = None,
                    sentiment: str = None, intent: str = None) -> dict:
        """Add a message to the conversation history with optional metadata."""
        conversation = self.load_conversation(chat_id)

        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        }

        if username:
            message["username"] = username
        if sentiment:
            message["sentiment"] = sentiment
        if intent:
            message["intent"] = intent

        conversation["messages"].append(message)
        self.save_conversation(chat_id, conversation)

        return conversation

    def get_conversation_history(self, chat_id: int, max_messages: int = 20) -> list:
        """Get recent conversation history for context."""
        conversation = self.load_conversation(chat_id)
        messages = conversation.get("messages", [])

        # Return the last N messages for context
        return messages[-max_messages:] if len(messages) > max_messages else messages

    def format_history_for_prompt(self, chat_id: int, max_messages: int = 20) -> str:
        """Format conversation history as a string for the AI prompt."""
        messages = self.get_conversation_history(chat_id, max_messages)

        if not messages:
            return ""

        formatted = []
        for msg in messages:
            role = "User" if msg["role"] == "user" else "You"
            formatted.append(f"{role}: {msg['content']}")

        return "\n".join(formatted)

    def get_all_chat_ids(self) -> list[int]:
        """Get all chat IDs from saved conversations."""
        chat_ids = []
        if not os.path.exists(self.conversations_folder):
            return chat_ids

        for filename in os.listdir(self.conversations_folder):
            if filename.startswith("chat_") and filename.endswith(".json"):
                try:
                    chat_id = int(filename[5:-5])  # Extract number from "chat_123.json"
                    chat_ids.append(chat_id)
                except ValueError:
                    continue
        return chat_ids

    def needs_reply(self, chat_id: int) -> bool:
        """Check if the last message in conversation is from user (needs reply)."""
        conversation = self.load_conversation(chat_id)
        messages = conversation.get("messages", [])

        if not messages:
            return False

        last_message = messages[-1]
        return last_message.get("role") == "user"

    def get_last_user_message(self, chat_id: int) -> str | None:
        """Get the last user message that needs a reply."""
        conversation = self.load_conversation(chat_id)
        messages = conversation.get("messages", [])

        # Find all consecutive user messages at the end
        user_messages = []
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_messages.insert(0, msg.get("content", ""))
            else:
                break

        return " ".join(user_messages) if user_messages else None

    def get_message_count(self, chat_id: int, role: str = None) -> int:
        """Get count of messages, optionally filtered by role."""
        conversation = self.load_conversation(chat_id)
        messages = conversation.get("messages", [])

        if role:
            return len([m for m in messages if m.get("role") == role])
        return len(messages)

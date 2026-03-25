import os
import hashlib
from datetime import datetime
from openai import OpenAI
from chromadb.config import Settings
import chromadb

from config import (
    OPENAI_API_KEY,
    VECTOR_STORE_FOLDER,
    KNOWLEDGE_FOLDER,
    RAG_MAX_RESULTS,
    RAG_RELEVANCE_THRESHOLD
)


class RAGManager:
    def __init__(self):
        os.makedirs(VECTOR_STORE_FOLDER, exist_ok=True)
        os.makedirs(KNOWLEDGE_FOLDER, exist_ok=True)

        self.chroma_client = chromadb.PersistentClient(
            path=VECTOR_STORE_FOLDER,
            settings=Settings(anonymized_telemetry=False)
        )
        self.openai_client = OpenAI(api_key=OPENAI_API_KEY)
        self.embedding_model = "text-embedding-3-small"

        # Two collections: conversation memory + knowledge base
        self.conversations = self.chroma_client.get_or_create_collection(
            name="conversations",
            metadata={"hnsw:space": "cosine"}
        )
        self.knowledge = self.chroma_client.get_or_create_collection(
            name="knowledge",
            metadata={"hnsw:space": "cosine"}
        )

        print(f"[RAG] Initialized. Conversations: {self.conversations.count()} docs, Knowledge: {self.knowledge.count()} docs")

    def _get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for text using OpenAI."""
        text = text.strip()
        if not text:
            return []

        try:
            response = self.openai_client.embeddings.create(
                model=self.embedding_model,
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            print(f"[RAG] Embedding error: {e}")
            return []

    def _get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for multiple texts in one API call."""
        texts = [t.strip() for t in texts if t.strip()]
        if not texts:
            return []

        try:
            response = self.openai_client.embeddings.create(
                model=self.embedding_model,
                input=texts
            )
            return [d.embedding for d in response.data]
        except Exception as e:
            print(f"[RAG] Batch embedding error: {e}")
            return []

    # ==========================================
    # CONVERSATION MEMORY
    # ==========================================

    def add_conversation_message(self, chat_id: int, role: str, content: str, timestamp: str = None):
        """Store a conversation message in the vector store."""
        if not content or len(content.strip()) < 5:
            return

        content = content.strip()
        if not timestamp:
            timestamp = datetime.now().isoformat()

        # Create a unique ID for this message
        doc_id = hashlib.md5(f"{chat_id}_{timestamp}_{content[:50]}".encode()).hexdigest()

        embedding = self._get_embedding(content)
        if not embedding:
            return

        try:
            self.conversations.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[content],
                metadatas=[{
                    "chat_id": str(chat_id),
                    "role": role,
                    "timestamp": timestamp
                }]
            )
        except Exception as e:
            print(f"[RAG] Error storing conversation message: {e}")

    def query_conversation_context(self, chat_id: int, query: str, n_results: int = None) -> list[dict]:
        """Retrieve relevant past conversation snippets for a specific chat."""
        if not query or self.conversations.count() == 0:
            return []

        n_results = n_results or RAG_MAX_RESULTS

        embedding = self._get_embedding(query)
        if not embedding:
            return []

        try:
            results = self.conversations.query(
                query_embeddings=[embedding],
                n_results=n_results,
                where={"chat_id": str(chat_id)}
            )
        except Exception as e:
            print(f"[RAG] Conversation query error: {e}")
            return []

        return self._parse_results(results)

    # ==========================================
    # KNOWLEDGE BASE
    # ==========================================

    def add_knowledge_document(self, doc_path: str):
        """Load and index a knowledge document (txt or md)."""
        if not os.path.exists(doc_path):
            print(f"[RAG] Document not found: {doc_path}")
            return

        ext = os.path.splitext(doc_path)[1].lower()
        if ext not in ('.txt', '.md'):
            print(f"[RAG] Unsupported file type: {ext} (use .txt or .md)")
            return

        try:
            with open(doc_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"[RAG] Error reading {doc_path}: {e}")
            return

        if not content.strip():
            return

        filename = os.path.basename(doc_path)
        chunks = self._chunk_text(content)

        if not chunks:
            return

        # Batch embed all chunks
        embeddings = self._get_embeddings_batch(chunks)
        if len(embeddings) != len(chunks):
            print(f"[RAG] Embedding count mismatch for {filename}, skipping")
            return

        ids = []
        metadatas = []
        for i, chunk in enumerate(chunks):
            doc_id = hashlib.md5(f"{filename}_{i}_{chunk[:50]}".encode()).hexdigest()
            ids.append(doc_id)
            metadatas.append({
                "source": filename,
                "chunk_index": i,
                "total_chunks": len(chunks)
            })

        try:
            self.knowledge.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=chunks,
                metadatas=metadatas
            )
            print(f"[RAG] Indexed knowledge doc: {filename} ({len(chunks)} chunks)")
        except Exception as e:
            print(f"[RAG] Error indexing {filename}: {e}")

    def load_knowledge_folder(self):
        """Load all knowledge documents from the knowledge folder."""
        if not os.path.exists(KNOWLEDGE_FOLDER):
            return

        files = [f for f in os.listdir(KNOWLEDGE_FOLDER)
                 if f.lower().endswith(('.txt', '.md'))]

        if not files:
            print("[RAG] No knowledge documents found")
            return

        print(f"[RAG] Loading {len(files)} knowledge document(s)...")
        for filename in files:
            filepath = os.path.join(KNOWLEDGE_FOLDER, filename)
            self.add_knowledge_document(filepath)

    def query_knowledge(self, query: str, n_results: int = None) -> list[dict]:
        """Query the knowledge base for relevant information."""
        if not query or self.knowledge.count() == 0:
            return []

        n_results = n_results or RAG_MAX_RESULTS

        embedding = self._get_embedding(query)
        if not embedding:
            return []

        try:
            results = self.knowledge.query(
                query_embeddings=[embedding],
                n_results=n_results
            )
        except Exception as e:
            print(f"[RAG] Knowledge query error: {e}")
            return []

        return self._parse_results(results)

    # ==========================================
    # COMBINED QUERY
    # ==========================================

    def query_relevant_context(self, chat_id: int, query: str) -> str:
        """Query both conversation history and knowledge base, return formatted context."""
        context_parts = []

        # Query conversation memory for this chat
        conv_results = self.query_conversation_context(chat_id, query, n_results=3)
        relevant_conv = [r for r in conv_results if r["score"] >= RAG_RELEVANCE_THRESHOLD]

        if relevant_conv:
            conv_lines = []
            for r in relevant_conv:
                role = r["metadata"].get("role", "unknown")
                label = "User" if role == "user" else "You"
                conv_lines.append(f"  {label}: {r['document']}")
            context_parts.append("Past conversation snippets:\n" + "\n".join(conv_lines))

        # Query knowledge base
        knowledge_results = self.query_knowledge(query, n_results=3)
        relevant_knowledge = [r for r in knowledge_results if r["score"] >= RAG_RELEVANCE_THRESHOLD]

        if relevant_knowledge:
            knowledge_lines = []
            for r in relevant_knowledge:
                source = r["metadata"].get("source", "unknown")
                knowledge_lines.append(f"  [{source}]: {r['document']}")
            context_parts.append("Relevant knowledge:\n" + "\n".join(knowledge_lines))

        return "\n\n".join(context_parts) if context_parts else ""

    # ==========================================
    # HELPERS
    # ==========================================

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        """Split text into overlapping chunks."""
        text = text.strip()
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size

            # Try to break at a sentence boundary
            if end < len(text):
                # Look for sentence-ending punctuation near the end
                for boundary in ['. ', '.\n', '! ', '!\n', '? ', '?\n', '\n\n']:
                    idx = text.rfind(boundary, start + chunk_size // 2, end)
                    if idx != -1:
                        end = idx + len(boundary)
                        break

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            start = end - overlap

        return chunks

    def _parse_results(self, results: dict) -> list[dict]:
        """Parse ChromaDB query results into a clean list."""
        parsed = []
        if not results or not results.get("documents"):
            return parsed

        documents = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0] if results.get("distances") else [0] * len(documents)

        for doc, meta, dist in zip(documents, metadatas, distances):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to similarity score: 1 - (distance / 2)
            score = 1 - (dist / 2)
            parsed.append({
                "document": doc,
                "metadata": meta,
                "score": score
            })

        return parsed

"""
Embedding Service for Semantic Search.

Supports both local (HuggingFace) and API-based (OpenAI) embeddings.
Local is recommended for privacy and cost, OpenAI for highest quality.
"""

import os
import json
import hashlib
import logging
from typing import List, Dict, Any, Optional, Literal
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default models
LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # 384 dimensions, fast
OPENAI_MODEL = "text-embedding-3-small"  # 1536 dimensions, high quality


@dataclass
class EmbeddingResult:
    """Result of embedding generation."""
    text: str
    embedding: List[float]
    model: str
    dimensions: int


class EmbeddingService:
    """
    Generate embeddings for text using local or API-based models.
    
    Usage:
        service = EmbeddingService(provider="local")  # or "openai"
        embeddings = service.embed_texts(["Hello world", "Goodbye"])
    """
    
    def __init__(
        self,
        provider: Literal["local", "openai", "auto"] = "auto",
        model: Optional[str] = None,
        cache_dir: Optional[Path] = None,
    ):
        """
        Initialize embedding service.
        
        Args:
            provider: "local" for HuggingFace, "openai" for API, "auto" to auto-detect
            model: Override default model
            cache_dir: Directory to cache embeddings
        """
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Auto-detect provider
        if provider == "auto":
            if os.environ.get("OPENAI_API_KEY"):
                provider = "openai"
            else:
                provider = "local"
        
        self.provider = provider
        self.model = model
        self._local_model = None
        self._openai_client = None
        
        logger.info(f"EmbeddingService initialized with provider={provider}")
    
    def embed_texts(
        self,
        texts: List[str],
        batch_size: int = 32,
        show_progress: bool = False,
    ) -> List[EmbeddingResult]:
        """
        Generate embeddings for a list of texts.
        
        Args:
            texts: List of texts to embed
            batch_size: Number of texts per batch
            show_progress: Show progress bar
            
        Returns:
            List of EmbeddingResult objects
        """
        if self.provider == "openai":
            return self._embed_openai(texts, batch_size, show_progress)
        else:
            return self._embed_local(texts, batch_size, show_progress)
    
    def embed_text(self, text: str) -> EmbeddingResult:
        """Embed a single text."""
        results = self.embed_texts([text])
        return results[0] if results else None
    
    def _embed_local(
        self,
        texts: List[str],
        batch_size: int,
        show_progress: bool,
    ) -> List[EmbeddingResult]:
        """Generate embeddings using local HuggingFace model."""
        model_name = self.model or LOCAL_MODEL
        
        # Lazy load model
        if self._local_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info(f"Loading local model: {model_name}")
                self._local_model = SentenceTransformer(model_name)
                logger.info(f"Model loaded successfully")
            except ImportError:
                raise ImportError(
                    "sentence-transformers not installed. Install with: "
                    "pip install sentence-transformers"
                )
        
        # Generate embeddings
        embeddings = self._local_model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=show_progress,
            convert_to_numpy=True,
        )
        
        results = []
        for text, emb in zip(texts, embeddings):
            results.append(EmbeddingResult(
                text=text,
                embedding=emb.tolist(),
                model=model_name,
                dimensions=len(emb),
            ))
        
        return results
    
    def _embed_openai(
        self,
        texts: List[str],
        batch_size: int,
        show_progress: bool,
    ) -> List[EmbeddingResult]:
        """Generate embeddings using OpenAI API."""
        model_name = self.model or OPENAI_MODEL
        
        # Lazy initialize client
        if self._openai_client is None:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI()
            except ImportError:
                raise ImportError(
                    "openai not installed. Install with: pip install openai"
                )
        
        results = []
        
        # Process in batches
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            
            response = self._openai_client.embeddings.create(
                model=model_name,
                input=batch,
            )
            
            for text, data in zip(batch, response.data):
                results.append(EmbeddingResult(
                    text=text,
                    embedding=data.embedding,
                    model=model_name,
                    dimensions=len(data.embedding),
                ))
            
            if show_progress:
                logger.info(f"Embedded {min(i + batch_size, len(texts))}/{len(texts)}")
        
        return results
    
    def get_dimensions(self) -> int:
        """Get the embedding dimensions for current model."""
        if self.provider == "openai":
            return 1536  # text-embedding-3-small
        else:
            return 384  # all-MiniLM-L6-v2


class FragmentEmbedder:
    """
    Embed podcast fragments for semantic search.
    
    Handles batch processing, progress tracking, and database storage.
    """
    
    def __init__(
        self,
        provider: str = "auto",
        db_path: Optional[str] = None,
    ):
        """
        Initialize fragment embedder.
        
        Args:
            provider: Embedding provider ("local", "openai", "auto")
            db_path: Path to SQLite database
        """
        self.service = EmbeddingService(provider=provider)
        self.db_path = db_path
    
    def embed_fragments(
        self,
        fragments: List[Dict[str, Any]],
        batch_size: int = 32,
        progress_callback: Optional[callable] = None,
    ) -> List[Dict[str, Any]]:
        """
        Embed a list of fragments.
        
        Args:
            fragments: List of fragment dicts with 'id' and 'text' keys
            batch_size: Batch size for embedding
            progress_callback: Optional callback(current, total, message)
            
        Returns:
            List of dicts with fragment_id and embedding
        """
        texts = [f.get("text", "") for f in fragments]
        
        results = []
        total = len(texts)
        
        for i in range(0, total, batch_size):
            batch_texts = texts[i:i + batch_size]
            batch_fragments = fragments[i:i + batch_size]
            
            embeddings = self.service.embed_texts(batch_texts)
            
            for frag, emb in zip(batch_fragments, embeddings):
                results.append({
                    "fragment_id": frag.get("id"),
                    "embedding": emb.embedding,
                    "dimensions": emb.dimensions,
                    "model": emb.model,
                })
            
            if progress_callback:
                progress_callback(
                    min(i + batch_size, total),
                    total,
                    f"Embedded {min(i + batch_size, total)}/{total} fragments"
                )
        
        return results
    
    def save_embeddings_to_db(
        self,
        embeddings: List[Dict[str, Any]],
        table_name: str = "fragment_embeddings",
    ):
        """
        Save embeddings to SQLite database.
        
        Creates table if it doesn't exist.
        """
        import sqlite3
        
        if not self.db_path:
            raise ValueError("db_path not set")
        
        conn = sqlite3.connect(self.db_path)
        
        # Create table
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                fragment_id TEXT PRIMARY KEY,
                embedding BLOB,
                dimensions INTEGER,
                model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Insert embeddings
        for emb in embeddings:
            embedding_json = json.dumps(emb["embedding"])
            conn.execute(f"""
                INSERT OR REPLACE INTO {table_name}
                (fragment_id, embedding, dimensions, model)
                VALUES (?, ?, ?, ?)
            """, (
                emb["fragment_id"],
                embedding_json,
                emb["dimensions"],
                emb["model"],
            ))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Saved {len(embeddings)} embeddings to {table_name}")
    
    def similarity_search(
        self,
        query: str,
        top_k: int = 10,
        table_name: str = "fragment_embeddings",
        format_id: Optional[str] = None,
        min_similarity: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for similar fragments using cosine similarity.
        
        Args:
            query: Search query text
            top_k: Number of results to return
            table_name: Embeddings table name
            format_id: Filter to fragments from this podcast (items.format_id)
            min_similarity: Optional minimum score (0-1). Default: none, return top_k by rank
            
        Returns:
            List of dicts with fragment_id and similarity score
        """
        import sqlite3
        import numpy as np
        
        if not self.db_path:
            raise ValueError("db_path not set")
        
        # Embed query
        query_result = self.service.embed_text(query)
        query_embedding = np.array(query_result.embedding)
        
        # Load embeddings - filter by format_id at DB level so we only compare within that podcast
        conn = sqlite3.connect(self.db_path)
        if format_id:
            cursor = conn.execute(f"""
                SELECT e.fragment_id, e.embedding
                FROM {table_name} e
                JOIN fragments f ON f.id = e.fragment_id
                JOIN items i ON f.item_id = i.id
                WHERE i.format_id = ?
            """, (format_id,))
        else:
            cursor = conn.execute(f"""
                SELECT fragment_id, embedding FROM {table_name}
            """)
        
        results = []
        for row in cursor:
            fragment_id = row[0]
            embedding = np.array(json.loads(row[1]))
            
            # Cosine similarity (range typically -1 to 1, often 0.2-0.8 for related text)
            similarity = np.dot(query_embedding, embedding) / (
                np.linalg.norm(query_embedding) * np.linalg.norm(embedding) + 1e-9
            )
            sim_float = float(similarity)
            
            if min_similarity is not None and sim_float < min_similarity:
                continue
            
            results.append({
                "fragment_id": fragment_id,
                "similarity": sim_float,
            })
        
        conn.close()
        
        # Sort by similarity and return top_k (no threshold - return best matches by rank)
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]


def check_embedding_status(db_path: str) -> Dict[str, Any]:
    """
    Check embedding status in database.
    
    Returns:
        Dict with total_fragments, embedded_count, percentage, etc.
    """
    import sqlite3
    
    conn = sqlite3.connect(db_path)
    
    # Count total fragments
    cursor = conn.execute("SELECT COUNT(*) FROM fragments")
    total_fragments = cursor.fetchone()[0]
    
    # Count embedded fragments
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM fragment_embeddings")
        embedded_count = cursor.fetchone()[0]
    except sqlite3.OperationalError:
        embedded_count = 0
    
    # Get model info
    try:
        cursor = conn.execute(
            "SELECT model, dimensions FROM fragment_embeddings LIMIT 1"
        )
        row = cursor.fetchone()
        model = row[0] if row else None
        dimensions = row[1] if row else None
    except sqlite3.OperationalError:
        model = None
        dimensions = None
    
    conn.close()
    
    percentage = (embedded_count / total_fragments * 100) if total_fragments > 0 else 0
    
    return {
        "total_fragments": total_fragments,
        "embedded_count": embedded_count,
        "percentage": round(percentage, 1),
        "model": model,
        "dimensions": dimensions,
        "needs_embedding": total_fragments - embedded_count,
    }

import logging
import asyncio
from typing import List, Optional, Dict, Any
from openai import AsyncOpenAI

from libAgents.config import (
    OPENAI_API_KEY,
    LLM_PROVIDER,
    LITELLEM_KEY,
    LITELLEM_BASE_URL,
)
from libAgents.tracker import TokenTracker

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-large"  # OpenAI's embedding model


async def _get_embeddings_core(
    texts: List[str], tracker: Optional[TokenTracker] = None
) -> Dict[str, Any]:
    """
    Core embedding function that makes the actual API call.
    """
    # Determine which API key and base URL to use
    if LLM_PROVIDER == "openai":
        api_key = OPENAI_API_KEY
    elif LLM_PROVIDER == "litellm":
        api_key = LITELLEM_KEY
        base_url = LITELLEM_BASE_URL
    else:
        # Fallback to OpenAI
        api_key = OPENAI_API_KEY

    if not api_key:
        logger.warning(
            "No API key available for embeddings, returning empty embeddings"
        )
        return {"embeddings": [], "tokens": 0}

    # Create OpenAI client
    if LLM_PROVIDER == "openai":
        client = AsyncOpenAI(
            api_key=api_key,
        )
    elif LLM_PROVIDER == "litellm":
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # Get embeddings
    response = await client.embeddings.create(model=EMBEDDING_MODEL, input=texts)

    # Extract embeddings
    embeddings = [data.embedding for data in response.data]
    tokens = response.usage.total_tokens

    # Track token usage
    if tracker:
        tracker.track_usage("embeddings", tokens)

    return {"embeddings": embeddings, "tokens": tokens}


async def get_embeddings(
    texts: List[str], 
    tracker: Optional[TokenTracker] = None,
    timeout: float = 30.0,
    max_retries: int = 5,
    base_delay: float = 5
) -> Dict[str, Any]:
    """
    Get embeddings for a list of texts using OpenAI's embedding model with timeout and retry.

    Args:
        texts: List of text strings to get embeddings for
        tracker: Optional token tracker for usage tracking
        timeout: Timeout in seconds for each API call (default: 30.0)
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Base delay in seconds for exponential backoff (default: 1.0)

    Returns:
        Dictionary containing embeddings list and token usage
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            # Use asyncio.wait_for to add timeout
            result = await asyncio.wait_for(
                _get_embeddings_core(texts, tracker), 
                timeout=timeout
            )
            # logger.info(f"Embeddings: {result['embeddings']}")
            return result
            
        except asyncio.TimeoutError as e:
            last_exception = e
            logger.warning(f"Attempt {attempt + 1}/{max_retries + 1} timed out after {timeout}s")
            
        except Exception as e:
            last_exception = e
            logger.warning(f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}")
        
        # If this isn't the last attempt, wait before retrying
        if attempt < max_retries:
            delay = base_delay * (2 ** attempt)  # Exponential backoff
            logger.info(f"Retrying in {delay:.2f} seconds...")
            await asyncio.sleep(delay)
    
    # All retries failed
    logger.error(f"All {max_retries + 1} attempts failed. Last error: {last_exception}")
    return {"embeddings": [], "tokens": 0}


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity score between 0 and 1
    """
    try:
        # Calculate dot product
        dot_product = sum(a * b for a, b in zip(vec1, vec2))

        # Calculate magnitudes
        magnitude1 = sum(a * a for a in vec1) ** 0.5
        magnitude2 = sum(b * b for b in vec2) ** 0.5

        # Avoid division by zero
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0

        # Calculate cosine similarity
        similarity = dot_product / (magnitude1 * magnitude2)

        # Ensure result is between 0 and 1
        return max(0.0, min(1.0, similarity))

    except Exception as error:
        logger.error(f"Error calculating cosine similarity: {error}")
        return 0.0

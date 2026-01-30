import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Define your custom exception class if not already defined.
class NoObjectGeneratedError(Exception):
    def __init__(self, text: str, usage: Optional[Dict[str, Any]] = None):
        super().__init__(text)
        self.text = text
        self.usage = usage if usage is not None else {}


@dataclass
class ErrorResponseWrapper:
    """Wrapper for error recovery responses, mimicking ResponseWrapper interface."""
    object: str  # JSON string, not parsed object
    usage: Any

    @dataclass
    class UsageInfo:
        total_tokens: int


async def handle_generate_object_error(error: Exception) -> ErrorResponseWrapper:
    """
    Handles errors from generate_object.

    If the error is an instance of NoObjectGeneratedError, this function will attempt to
    manually parse a partial JSON response from error.text and return it along with the token usage.
    For JSONDecodeError, returns a default fallback response.
    Otherwise, it will re-raise the error.

    Returns:
        ErrorResponseWrapper with:
            - object: JSON string (to be parsed by caller with json.loads)
            - usage: Usage info with total_tokens attribute
    """
    if isinstance(error, NoObjectGeneratedError):
        logger.debug(
            "Object not generated according to the schema, fallback to manual parsing"
        )
        try:
            # Validate that the text is valid JSON by parsing it
            json.loads(error.text)
            total_tokens = error.usage.get("totalTokens", 0) if error.usage else 0
            # Return the original JSON string (not parsed), matching ResponseWrapper interface
            return ErrorResponseWrapper(
                object=error.text,
                usage=ErrorResponseWrapper.UsageInfo(total_tokens=total_tokens)
            )
        except Exception:
            # If parsing fails, re-raise the original error.
            raise error

    # Handle JSON parsing errors gracefully
    if isinstance(error, json.JSONDecodeError):
        logger.warning(f"JSONDecodeError in generate_object: {error}")
        # Return a default fallback response
        fallback = {
            "action": "answer",
            "thoughts": "JSON parsing failed, using fallback response",
            "action-details": {
                "answer": "Failed to process query due to a parsing error."
            },
            "pass": False,
            "type": "error",
            "think": "JSON parsing error occurred"
        }
        return ErrorResponseWrapper(
            object=json.dumps(fallback),
            usage=ErrorResponseWrapper.UsageInfo(total_tokens=0)
        )

    raise error

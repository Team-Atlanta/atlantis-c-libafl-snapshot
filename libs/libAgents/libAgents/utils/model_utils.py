import logging
import random
from typing import Dict
from libAgents.model import generate_text
from libAgents.config import get_model

logger = logging.getLogger(__name__)


def get_model_by_weights(weighted_models: Dict[str, int]) -> str:
    """
    Get a model from a weighted list of models.
    Filters out unavailable models (e.g., gemini models without credentials).
    """
    if weighted_models is None:
        logger.warning("No weighted models provided. Using default model: gpt-4.1")
        return "gpt-4.1"

    # Filter out gemini models as they require separate Google credentials
    # that may not be available in all environments
    filtered_models = {k: v for k, v in weighted_models.items() if "gemini" not in k.lower()}

    if not filtered_models:
        logger.warning("No available models after filtering. Using default model: gpt-4.1")
        return "gpt-4.1"

    keys = list(filtered_models.keys())
    weights = list(filtered_models.values())
    chosen = random.choices(keys, weights=weights, k=1)[0]
    logger.info(f"Chosen model: {chosen}")
    return chosen


async def extract_script_from_response(response: str, model: str, use_model: bool = False) -> str:
    prompt = f"""Extract the valid copy/pasteable python script from the following response (if no valid script, print 'No valid script found'): 

<response>
{response}
</response>

Remember: 
do not include any other prelogue or postlogue (e.g., ```python, ```, etc.), because I need to copy/paste the script into the interpreter.
"""
    if use_model:
        if isinstance(model, str):
            model = get_model("extract_script", override_model=model)

        res = await generate_text(
            model,
            prompt,
        )
        if "No valid script found" in res.object:
            logger.error(f"No valid script found\n: {res.object}")
            return None
        else:
            # print(f"res.object: {res}")
            if "```python" in res.object:
                res.object = res.object.split("```python")[1].split("```")[0]
            return res.object

    if "<script>" in response:
        return response.split("<script>")[1].split("</script>")[0]
    elif "```python" in response:
        return response.split("```python")[1].split("```")[0]
    else:
        model = get_model("extract_script", override_model=model)
        res = await generate_text(
            model,
            prompt,
        )
        if "No valid script found" in res.object:
            logger.error("No valid script found")
            return None
        else:
            return res.object
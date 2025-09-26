#!/usr/bin/env python3.12
"""Request a haiku from OpenAI API via LiteLLM proxy."""

import os
import sys
import time
from pathlib import Path
from openai import OpenAI


def poll_for_key(key_path: Path, timeout: int = 60) -> bool:
    """
    Poll for the existence of the API key file.

    Args:
        key_path: Path to the API key file
        timeout: Maximum time to wait in seconds (default: 60)

    Returns:
        True if key file exists, False if timeout reached
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        if key_path.exists():
            print(f"Found API key at {key_path}")
            return True

        elapsed = int(time.time() - start_time)
        print(f"Waiting for API key at {key_path}... ({elapsed}s elapsed)")
        time.sleep(2)

    print(f"Timeout: API key not found at {key_path} after {timeout} seconds")
    return False


def poll_for_litellm_health(base_url: str, max_attempts: int = 20) -> bool:
    """
    Poll for LiteLLM proxy to be responsive.

    Args:
        base_url: Base URL of LiteLLM proxy (e.g., 'http://litellm:4000')
        max_attempts: Maximum number of attempts (default: 20)

    Returns:
        True if LiteLLM is responsive, False otherwise
    """
    import requests

    # Remove /v1 suffix if present to get base URL
    if base_url.endswith('/v1'):
        base_url = base_url[:-3]

    health_url = f"{base_url}/health"

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(health_url, timeout=5)
            # Any response means service is up (don't care about status code)
            print(f"LiteLLM proxy is responsive at {health_url}")
            return True
        except requests.exceptions.RequestException:
            print(f"Waiting for LiteLLM proxy at {health_url}... (attempt {attempt}/{max_attempts})")
            time.sleep(2)

    print(f"Timeout: LiteLLM proxy not responsive at {health_url} after {max_attempts} attempts")
    return False


# Get LiteLLM URL from environment
litellm_url = os.getenv("LITELLM_URL", "http://litellm:4000")
base_url = f"{litellm_url}/v1"

# Poll for LiteLLM health first
if not poll_for_litellm_health(litellm_url):
    print("ERROR: LiteLLM proxy not healthy")
    sys.exit(1)

# Poll for key file
key_path = Path("/keys/api_key")
if not poll_for_key(key_path):
    print("ERROR: API key not available")
    sys.exit(1)

# Read API key
try:
    with open(key_path) as f:
        api_key = f.read().strip()
except Exception as e:
    print(f"ERROR: Failed to read API key: {e}")
    sys.exit(1)

if not api_key:
    print("ERROR: API key is empty")
    sys.exit(1)

print(f"Using LiteLLM proxy at: {base_url}")

# Create client pointing to LiteLLM proxy
client = OpenAI(
    api_key=api_key,
    base_url=base_url
)


# Make a request to generate a haiku
try:
    print("Requesting haiku from OpenAI via LiteLLM proxy...")
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": "Write a haiku about fuzzing and software testing."
            }
        ],
        temperature=0.7,
        max_tokens=100
    )

    # Extract and print the haiku
    haiku = response.choices[0].message.content
    print("\n" + "="*50)
    print("HAIKU FROM BUILD:")
    print("="*50)
    print(haiku)
    print("="*50)

    # Print token usage
    print(f"\nTokens used: {response.usage.total_tokens}\n")

except Exception as e:
    print(f"ERROR: Failed to get haiku: {e}")
    sys.exit(1)

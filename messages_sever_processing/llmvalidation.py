import json
import httpx
import os
from dotenv import load_dotenv  # 1. You usually need this to read .env files

# Load environment variables from .env file
load_dotenv()

# 2. Check if key exists immediately to debug
API_KEY = os.getenv("SILICON_FLOW_API_KEY")
if not API_KEY:
    raise ValueError("API Key not found! Make sure SILICON_FLOW_API_KEY is set in your .env file.")

API_URL = "https://api.siliconflow.com/v1/chat/completions"


async def check_usage_with_siliconflow(sentence: str, target_words: list[str]) -> dict:
    if not target_words:
        return {"valid_words": [], "feedback": ""}

    system_prompt = (
        "You are a language tutor. Analyze the User Sentence. "
        "Check if the Target Words are used correctly (grammar/context). "
        "Return ONLY valid JSON: {\"valid_words\": [list of strings], \"feedback\": \"very brief comment\"}."
    )

    user_prompt = f"Sentence: \"{sentence}\"\nTarget Words: {json.dumps(target_words)}"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }


    payload = {
        "model": "Qwen/Qwen3-8B",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 200,
        "response_format": {"type": "json_object"}
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(API_URL, json=payload, headers=headers)

            # This will print the actual error text from the server if it fails again
            if response.status_code != 200:
                print(f"Error Status: {response.status_code}")
                print(f"Error Body: {response.text}")

            response.raise_for_status()

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)

    except Exception as e:
        print(f"⚠️ AI Validation Failed: {e}")
        return {"valid_words": [], "feedback": "AI Validation unavailable."}
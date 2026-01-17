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
        "You are a strict language teacher, being able to adapt and analyze to any language.\n"
        "Your task is to evaluate whether specific target words are used correctly \n"
        "in the User Sentence.\n\n"

        "Rules:\n"
        "1. Language Check:\n"
        "- If the User Sentence is not written entirely in the target language, "
        "or contains a mix of multiple languages (excluding proper names or numbers), "
        "REJECT ALL words.\n"
        "- Slightly unnatural phrasing is acceptable if the sentence is still grammatical and meaningful.\n"
        "- Target words MAY appear in conjugated or declined forms; this is acceptable if correct.\n\n"

        "2. Grammar & Usage Check:\n"
        "- A target word is valid ONLY if it is grammatically correct, properly conjugated/declined, "
        "and fits the sentence context.\n"
        "- Simply appending or listing the word without proper sentence integration is a FAIL.\n\n"
        
        "3. SEMANTIC CHECK (CRITICAL): The sentence must make LOGICAL SENSE. Reject nonsense, surrealism, or impossible actions.\n"

        "4. Output:\n"
        "- Return ONLY valid JSON.\n"
        "- JSON must contain exactly two fields:\n"
        "  * 'valid_words': a list of target words used correctly\n"
        "  * 'feedback': one concise constructive reply explaining mistakes or validating use (feedback in ENGLISH)\n"
        "- Do NOT include explanations, markdown, or extra text."
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
        "temperature": 0.1,
        "max_tokens": 1000,
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
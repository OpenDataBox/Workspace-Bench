import requests
import json

API_KEY = ""

url = "https://openrouter.ai/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

data = {
    "model": "anthropic/claude-3.7-sonnet",
    "messages": [
        {"role": "user", "content": "Reply with exactly OK"}
    ],
    "max_tokens": 20
}

response = requests.post(url, headers=headers, json=data)

print("Status Code:", response.status_code)
print("Response:")
print(response.text)

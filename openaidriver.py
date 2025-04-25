import os
import json
import time
import base64
import copy

from openai import OpenAI
from dotenv import load_dotenv
from helpers import prep_llm

import re

# now matches sequences like "U", "U;", "U;D;L;R;A;B;S" or "U;D;L;R;A;B;S;"
ACTION_RE = re.compile(r'^[LRUDABS](?:;[LRUDABS])*(?:;)?$')

load_dotenv()  # reads .env from cwd by default

# Maximum seconds to wait for the LLM stream before timing out
STREAM_TIMEOUT = 15
CLEANUP_WINDOW = 5

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

MODE = "GEMINI"  # or "OPENAI"

client = None
MODEL = None
if MODE == "OPENAI":
    client = OpenAI(api_key=OPENAI_KEY)
    MODEL = "gpt-4.1-nano"
elif MODE == "GEMINI":
    client = OpenAI(
        api_key=GEMINI_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    MODEL = "gemini-2.5-flash-preview-04-17"

# Persistent chat history without image data
chat_history = [
    {
        "role": "system",
        "content": """
        You are an expert AI at completing for Pokémon Red.

        1. Thought Process
        Stream your reasoning step by step in plain language. Show every inference before choosing an action.

        Do not reference the visual grid overlay in your reasoning. It is there to help you measure distances and positions.
        Speak in first person, as if you are the player character in the game.

        2. Inputs & Visuals
        - You'll receive a full-screen game screenshot.
        - You'll sometimes receive a minimap showing your position as a blue circle.
        - Describe what you see in the screenshot and on the minimap, including player position, nearby terrain, objects, and NPCs.
        - Use reasoning to understand your position relative to the minimap and relate that to the screenshot.
        - Use the provided grid to determine your position relative to objects and NPCs. Remember you need to be beside and facing to interact with them.
        - Diagonal beside is not valid; you must be directly next to the object or NPC.
        - Determine the positions of objects and NPCs relative to your character's position.
        - Rely mostly on the screenshot for your reasoning, but use the minimap to confirm your position and plan your route.
        - Use the screehot to validate your movement decisions. Do not move into walls, fences, trees, or objects.

        3. Available Commands
        - Directions: U (up), D (down), L (left), R (right)
        - Buttons: A (A), B (B), S (Start)
        - You may chain commands with semicolons (e.g. R;R;R;), do not use spaces. Always end with a semicolon.
        - It is better to chain multiple commands together than to send them one at a time.
        - If an action repeatedly yields no result, try a different action.

        4. Menu Navigation
        - Press S to open/close the main menu.
        - Use U/D/L/R to move the selection cursor; press A to confirm. B to cancel or backspace.

        5. Map & Interactions
        - Movement is relative to the screen space, not facing direction.
        - To interact with objects or NPCs, move directly beside them (no grid cell in between) and press A.
        - Pokémon uses a grid system, so you can only interact with objects or NPCs that are directly beside you.
        - You are the character in the center of the screen at [4,4] (bottom left cell is [0,0]) on the grid THIS IS YOUR VISUAL POSITON ON SCREEN ON YOUR WORLDSPACE POSITION.
        - Use your characters screen position to plan a route towards your next destination. Use the privided grid to measure distances and positions.
        - To interact with an object or NPC, you must navigate to a tile DIRECTLY beside to it. DIAGONAL IS NOT VALID. YOU 
        - You must be directly to the left, right, above, or below the object or NPC. Your rank and file must be DIRECTLY beside to the object or NPC (NO CELLS BETWEEN).
        - Consider the grid system of the game. If an NPC is two spaces above you, you cannot interact with them. You must first move up to them.
        - Some object can only be interacted with from a specific side. For example, you can only interact with a sign from the front.
        - Use the screenshot to verify you are indeed beside to the object or NPC and facing it before interacting.
        - If an action yeilds no result, it may be because you are not DIRECTLY beside to the object or NPC.
        - Do not repeat the same action if it continues to yield no result. Instead, try a different action or move to a different location.
        - If you repeatedly fail your movement, use the minimap to check your position and ensure your planned actions are not blocked by a wall or object.
        - The Minimap DOES NOT show the objects or NPCs in the world. It only shows your position and the walkable tiles. If the minimap shows a walkable
        but you cannot move there, it is likely because there is an object or NPC blocking your path.
        - When inside a building, a carpet or unique tile may indicate a door or stairs. Stand on them to align with the door or stairs.
        - Exits are not marked on the minimap. You must use the screenshot to find them.
        - Verify your movement decision by checking the minimap and the screenshot. You cannot path through walls ("BLACK") on the minimap.
        - Exit buildings by moving to the door and continuing to move in the same direction until you exit. You must be aligned with the door vertically and horizonatally to exit.

        6. Navigation 
        - You can interact with doors and stairs by moving directly into them. They do not require a button press.
        - Movement is ALWAYS relative to the screen space, D will ALWAYS move vertically down. R will ALWAYS move horizontally right.
        - Facing direction does not matter for movement direction, but it does matter for interactions. Moving in a direction will face that direction.
        - BLACK tiles are walls and cannot be crossed.
        - WHITE tiles are walkable and can be crossed.
        - USE the minimap to plan your route. The minimap shows your position as a blue circle.
        - On the minimap, BLACK tiles will block your path, while WHITE tiles are walkable.
        - Use the screenshot to see what is in front of you and where you can move.
        - You cannot move through fences, trees, or other objects. If you are blocked, you must find a way around.
        - You cannot move through walls or objects. If you are blocked, you must find a way around.
        - If you are blocked by a wall, you must find a way around it. Use the minimap to plan your route.
        - To interact with NPCs behind a counter, you must be directly beside the counter in front of the NPC and facing the counter.

        7. Final Output Format
        Stream your reasoning and then output, after completing all reasoning, on a new line, exactly one line containing a JSON object 
        with the single property "action" whose value is your chosen command or command chain.
        Do NOT include any additional text, code fences, or explanation. DO NOT WRAP THE JSON IN MARKDOWN.
        DO print the JSON object in a single line, without any line breaks or indentation.
        ALWAYS end with a line in the format: {"action":"R;U;..."}
        Example: {"action":"R;R;R;A;"}
        """
    }
]

# Keep a copy of the original system prompt so we can re-inject it after summarizing
original_system_prompt = chat_history[0].copy()

# Counter of how many assistant responses we've accumulated
response_count = 0

def cleanup_image_history():
    """
    Strips out any image_url segments from all chat_history entries.
    """
    for msg in chat_history:
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                seg for seg in content
                if not (isinstance(seg, dict) and seg.get("type") == "image_url")
            ]

def summarize_and_reset():
    """
    Condense the existing user/assistant turns into a single summary,
    then reset chat_history to just the original system prompt + summary.
    """
    global chat_history, response_count

    cleanup_image_history()

    summary_messages = [
        {
            "role": "system",
            "content": """
            You are a summarization engine. Condense the below conversation into a concise summary that explains the previous actions 
            as a easy to follow story. Speak in first person, as if you are the player character in the game.
            Do not repeat yourself, many messages can be summarized into a single sentence.
            Explain your current position and what you are doing, and what you are trying to achieve.
            Do not include {"action","..."} or any other JSON object in your summary.
            Do not include any JSON or code. Your response should capture the essence of the actions taken and the reasoning behind them.
            """
        }
    ] + [
        msg for msg in chat_history
        if msg["role"] in ("assistant")
    ]

    print("Sent JSON: " + str(summary_messages))

    summary_resp = client.chat.completions.create(
        model= MODEL,
        messages=summary_messages,
        temperature=0.2,
        max_tokens=1024
    )
    summary_text = ""
    if not summary_resp.choices:
        print("⚠️ No choices returned")
        summary_text = "No summary available."
    else:
        summary_text = summary_resp.choices[0].message.content.strip()


    print("\nSummary response: " + summary_text + "\n")
    chat_history = [
        original_system_prompt,
        {
            "role": "user",
            "content": "[Prior conversation summary]\n" + summary_text
        }
    ]

    print("New history:")
    print(chat_history)
    response_count = 0

def llm_stream_action(state_data: dict, timeout: float = STREAM_TIMEOUT) -> str:
    """
    Sends the current game state (plus optional images) to the LLM,
    streams reasoning, records the assistant reply, and returns the extracted action.
    """
    global response_count

    cleanup_image_history()

    payload = copy.deepcopy(state_data)
    screenshot = payload.pop("screenshot", None)
    minimap    = payload.pop("minimap",    None)

    text_segment = {"type": "text", "text": json.dumps(payload)}
    content = [text_segment]

    if screenshot:
        content.append({
            "type": "image_url",
            "image_url": screenshot["image_url"]
        })
    if minimap:
        content.append({
            "type": "image_url",
            "image_url": minimap["image_url"]
        })

    full_entry = {"role": "user", "content": content}

    messages = chat_history + [full_entry]
    response = client.chat.completions.create(
        model       = MODEL,
        messages    = messages,
        temperature = 0.2,
        max_tokens  = 1024,
        stream      = True
    )

    collected, start = [], time.time()
    print("LLM:", end=" ")
    for chunk in response:
        if time.time() - start > timeout:
            print(f"\n[Stream timed out after {timeout}s]")
            break
        delta = chunk.choices[0].delta.content or ""
        print(delta, end="", flush=True)
        collected.append(delta)
    print()

    full_output = "".join(collected).strip()

    chat_history.append({"role": "user",      "content": [text_segment]})
    chat_history.append({"role": "assistant", "content": full_output})

    response_count += 1
    if response_count >= CLEANUP_WINDOW:
        summarize_and_reset()

    # 1) Try JSON pull
    try:
        json_start = full_output.rfind("{")
        candidate = full_output[json_start:]
        action = json.loads(candidate).get("action")
        if action is not None:
            return action
    except Exception:
        pass

    # 2) Fallback: take last non-empty line
    lines = [l.strip() for l in full_output.splitlines() if l.strip()]
    if lines:
        last = lines[-1]
        if ACTION_RE.match(last):
            return last

    # 3) no valid action found
    return None

def run_auto_loop(sock, interval: float = 0.5):
    """
    Main loop: capture state, encode images, stream LLM response, send action.
    """
    print("Starting auto LLM-driven loop. Press Ctrl+C to stop.")
    try:
        while True:
            print("GOING AGAIN")
            state = prep_llm(sock)
            time.sleep(1)

            screenshot_path = "latest.png"
            if os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0:
                with open(screenshot_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                state["screenshot"] = {
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "low"
                    }
                }
            else:
                state["screenshot"] = None

            minimap_path = "minimap.png"
            if os.path.exists(minimap_path) and os.path.getsize(minimap_path) > 0:
                with open(minimap_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                state["minimap"] = {
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "low"
                    }
                }
            else:
                state["minimap"] = None

            action = llm_stream_action(state)
            if action:
                print(f"Sending action: {action}")
                sock.sendall((action + "\n").encode("utf-8"))

            time.sleep(interval)
    except KeyboardInterrupt:
        print("Auto loop terminated by user.")

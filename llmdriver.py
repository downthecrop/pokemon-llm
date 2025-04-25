import os
import json
import time
import base64
import copy

from openai import OpenAI
from dotenv import load_dotenv
from helpers import prep_llm

import re

# matches sequences like "U", "U;", "U;D;L;R;A;B;S" or "U;D;L;R;A;B;S;"
ACTION_RE = re.compile(r'^[LRUDABS](?:;[LRUDABS])*(?:;)?$')

load_dotenv()  # reads .env from cwd by default

# Maximum seconds to wait for the LLM stream before timing out
STREAM_TIMEOUT = 30
CLEANUP_WINDOW = 15

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

def build_system_prompt(actionSummary: str) -> str:
    """
    Constructs the system prompt for the LLM, including the chat history.
    """
    return f"""
        You are an AI agent designed to play Pokémon Red. Your task is to analyze the game state, plan your actions, and provide input commands to progress through the game. 

        Your previous actions: {actionSummary}


        Instructions:

        1. Analyze the Game State:
        - Examine the screenshot provided in the game state.
        - Check the minimap (if available) to understand your position in the broader game world.
        - Identify your character's position, nearby terrain, objects, and NPCs.
        - Use the grid system to determine relative positions. Your character is always at [4,4] on the screen grid (bottom left cell is [0,0]).
        - List out all visible objects, NPCs, and terrain features in the screenshot.
        - Print any text that appears in the screenshot, including dialogue boxes, signs, or other text.
        - Explicitly state your current location based on the minimap.
        - The screenshot is the most accurate representation of the game state. Not the minimap or chat context.

        2. Plan Your Actions:
        - Consider your current goals in the game (e.g., reaching a specific location, interacting with an NPC, progressing the story).
        - List out the next 3-5 immediate goals or objectives.
        - Plan a route to your next destination using both the screenshot and minimap.
        - Create a step-by-step plan for your next few actions.
        - Ensure your planned actions don't involve walking into walls, fences, trees, or other obstacles.

        3. Navigation and Interaction:
        - Movement is always relative to the screen space: U (up), D (down), L (left), R (right).
        - To interact with objects or NPCs, move directly beside them (no diagonal interactions) and press A.
        - Align yourself properly with doors and stairs before attempting to use them.
        - Remember that you can't move through walls or objects, even if the minimap shows a walkable tile.
        - You can only pass ledges by moving DOWN, never UP.
        - If you repeartedly try the same action and it fails, explore other options.
        - Exits, stairs, and ladders are ALWAYS marked by a unique tile type.
        - Explore rooms before trying to exit them.
        - If you cannot find the exit, navigate around the room to find unique looking that could indicate the exit.
        - If you want to leave a building or room you must find the unique exit tile.
        - You cannot move when an interface is open, you must close or complete the interaction it first.


        4. Menu Navigation:
        - Press S to open the menu.
        - Use U/D/L/R to move the selection cursor, A to confirm, and B to cancel or go back.

        5. Command Chaining:
        - You may chain commands using semicolons (e.g., R;R;R;A;).
        - It's better to chain multiple commands together than to send them one at a time.
        - Always end your command chain with a semicolon.

        6. Reasoning Process:
        Wrap your analysis and planning inside <game_analysis> tags in your thinking block, including:
        - Your understanding of the current game state
        - Your immediate and long-term goals
        - The rationale behind your chosen actions
        - How you're using the screenshot and minimap to navigate
        - Any potential obstacles or challenges you foresee

        7. Output Format:
        After your analysis, on a new line, provide a single line JSON object with the "action" property containing your chosen command or command chain.

        Example output structure:

        <game_analysis>
        [Your detailed analysis and planning goes here]
        </game_analysis>

        {{"action":"U;U;R;A;"}}


        Remember:
        - Always use both the screenshot and minimap for navigation.
        - Be careful to align properly with doors and entrances/exits.
        - Avoid repeatedly walking into walls or obstacles.
        - If an action yields no result, try a different approach.
        - Explain your higher-level thinking process and current goals in your reasoning.

        Now, analyze the game state and decide on your next action. Your final output should consist only of the JSON object with the action and should not duplicate or rehash any of the work you did in the thinking block.
        
        Here is the current game state:
        """

# Generate original system prompt
chat_history = [
    {
        "role": "system",
        "content": build_system_prompt("")
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
            You are a summarization engine. Condense the below conversation into a concise summary that explains the previous actions.
            The summary should encompass all action together, and should not be a list of unique actions.
            Speak in first person, as if you are the player character in the game.
            Do not repeat yourself, many messages can be summarized into a single sentence.
            The summary should be conversational instead of a list of actions.
            Explain your current position and what you are doing, and what you are trying to achieve.
            Do not include {"action","..."} or any other JSON object in your summary.
            DO NOT INCLUDE ANY JSON OBJECTS IN YOUR RESPONSE.
            Do not include any JSON or code. Your response should capture the essence of the actions taken and the reasoning behind them.
            """
        }
    ] + [
        msg for msg in chat_history
        if msg["role"] in ("assistant")
    ]

    summary_resp = client.chat.completions.create(
        model= MODEL,
        messages=summary_messages,
        temperature=1.0,
        max_tokens=1024,
    )
    summary_text = ""
    if not summary_resp.choices:
        print("⚠️ No choices returned")
        summary_text = "No summary available."
    else:
        summary_text = summary_resp.choices[0].message.content.strip()


    # Add summary to system prompt
    prompt = build_system_prompt(summary_text)
    original_system_prompt["content"] = prompt

    chat_history = [
        original_system_prompt
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
        temperature = 1.0,
        max_tokens  = 2048,
        stream      = True
    )

    collected = []
    start     = time.time()

    print("LLM:", end=" ")
    for chunk in response:
        # 1) Timeout check
        if time.time() - start > timeout:
            print(f"\n[Stream timed out after {timeout}s]")
            break

        delta = chunk.choices[0].delta.content or ""
        finish = chunk.choices[0].finish_reason

        # 2) If the API signals “stop” (no more tokens), grab any final content and break
        if finish is not None:
            if delta:
                print(delta, end="", flush=True)
                collected.append(delta)
            break

        # 3) Otherwise it’s just another token—print & collect it
        print(delta, end="", flush=True)
        collected.append(delta)

    print()  # newline after streaming

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
            state = prep_llm(sock)
            time.sleep(2)

            screenshot_path = "latest.png"
            if os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0:
                with open(screenshot_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                state["screenshot"] = {
                    "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high"
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
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high"
                    }
                }
            else:
                print("No minimap available.")
                state["minimap"] = None

            action = llm_stream_action(state)
            if action:
                print(f"Sending action: {action}")
                sock.sendall((action + "\n").encode("utf-8"))

            time.sleep(interval)
    except KeyboardInterrupt:
        print("Auto loop terminated by user.")

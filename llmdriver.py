# --- llmdriver.py ---
import os
import json
import time
import base64
import copy
import asyncio
import datetime
import logging
import tiktoken
import random
import socket # Added for socket errors

from openai import OpenAI
from dotenv import load_dotenv
from helpers import prep_llm # Keep using prep_llm

import re

# Configure logging for this module
log = logging.getLogger('llmdriver')


# matches sequences like "U", "U;", "U;D;L;R;A;B;S" or "U;D;L;R;A;B;S;"
ACTION_RE = re.compile(r'^[LRUDABS](?:;[LRUDABS])*(?:;)?$')

load_dotenv()  # reads .env from cwd by default

# Maximum seconds to wait for the LLM stream before timing out
STREAM_TIMEOUT = 30
CLEANUP_WINDOW = 15 # How many LLM responses before summarizing history

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

MODE = "GEMINI"  # or "OPENAI"

client = None
MODEL = None

# --- Tokenizer Setup ---
# Use cl100k_base for GPT-4 and presumably decent for Gemini Flash
try:
    encoding = tiktoken.get_encoding("cl100k_base")
    log.info("Tiktoken encoder 'cl100k_base' loaded.")
except Exception as e:
    log.error(f"Failed to load tiktoken encoder: {e}. Token counts will be approximate (char/4).")
    encoding = None # Fallback will be used

# Rough estimate for image token cost (highly model dependent - ADJUST AS NEEDED)
IMAGE_TOKEN_COST_HIGH_DETAIL = 258 # Placeholder based on past OpenAI models
IMAGE_TOKEN_COST_LOW_DETAIL = 85 # Placeholder

if MODE == "OPENAI":
    client = OpenAI(api_key=OPENAI_KEY)
    MODEL = "gpt-4.1-nano" # Or your preferred OpenAI model
    if not OPENAI_KEY:
         log.error("MODE is OPENAI but OPENAI_API_KEY not found in environment variables.")
elif MODE == "GEMINI":
    if not GEMINI_KEY:
        log.error("MODE is GEMINI but GEMINI_API_KEY not found in environment variables.")
    client = OpenAI(
        api_key=GEMINI_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    MODEL = "gemini-2.5-flash-preview-04-17"
else:
    log.error(f"Invalid MODE selected: {MODE}")
    raise ValueError(f"Invalid MODE: {MODE}")

# --- Helper function for token estimation ---
def count_tokens(text: str) -> int:
    """Estimates token count for a given text using the loaded encoding."""
    if not text:
        return 0
    if not encoding:
        # Fallback: very rough estimate (chars / 4)
        return len(text) // 4
    try:
        return len(encoding.encode(text))
    except Exception as e:
        log.warning(f"Tiktoken encoding failed for text snippet (len {len(text)}): {e}. Using fallback estimate.")
        return len(text) // 4

# --- System Prompt Definition ---
def build_system_prompt(actionSummary: str) -> str:
    """
    Constructs the system prompt for the LLM, including the chat history summary.
    """
    summary_limit = 1500 # Limit summary characters in prompt
    truncated_summary = actionSummary
    if len(actionSummary) > summary_limit:
        truncated_summary = actionSummary[:summary_limit] + "... (truncated)"
        log.warning(f"Action summary was truncated for system prompt (>{summary_limit} chars).")

    # The system prompt content remains the same as previous versions
    return f"""
        You are an AI agent designed to play Pokémon Red. Your task is to analyze the game state, plan your actions, and provide input commands to progress through the game.

        Your previous actions summary: {truncated_summary}


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

# --- Global State & History Initialization ---
chat_history = [
    {
        "role": "system",
        "content": build_system_prompt("") # Initial prompt with empty summary
    }
]
# Keep a pristine copy of the initial system prompt structure if needed for full resets
# original_system_prompt_structure = copy.deepcopy(chat_history[0])
response_count = 0
action_count = 0 # Track total actions/cycles for the state
tokens_used_session = 0 # Track total estimated tokens for the state
start_time = datetime.datetime.now() # Track game time for the state

# --- History Management Functions ---
def cleanup_image_history():
    """Replaces image_url data with placeholders in chat history."""
    for msg in chat_history:
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            has_image = False
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "image_url":
                    if not has_image:
                         new_content.append({"type": "text", "text": "[Image Content Removed]"})
                         has_image = True
                else:
                    new_content.append(seg)
            msg["content"] = new_content


def summarize_and_reset():
    """
    Condenses assistant turns into a summary, updates the system prompt,
    resets history, and accounts for summarization token usage.
    """
    global chat_history, response_count, tokens_used_session

    log.info(f"Summarizing chat history ({len(chat_history)} messages)...")
    # Create a temporary history copy without images for the summarization API call
    history_for_summary = copy.deepcopy(chat_history)
    for msg in history_for_summary:
         content = msg.get("content")
         if isinstance(content, list):
             # Keep only text parts for summarization input
             text_parts = [seg.get("text", "") for seg in content if isinstance(seg, dict) and seg.get("type") == "text"]
             msg["content"] = "\n".join(filter(None, text_parts)) # Join text parts, filter empty
             if not msg["content"]: # Handle case where message only had images/placeholders
                  msg["content"] = "[Visual Content Only]"

    # Filter relevant assistant messages for summary input
    assistant_messages_to_summarize = [
        msg for msg in history_for_summary
        if msg["role"] == "assistant" and isinstance(msg.get("content"), str) and msg.get("content").strip() and not msg.get("content").startswith("[ERROR")
    ]

    # Avoid summarizing if there's nothing useful to summarize
    if not assistant_messages_to_summarize:
        log.info("No relevant assistant messages found to summarize, skipping summarization call.")
        response_count = 0 # Reset counter anyway
        # We still need to reset the history to just the system prompt
        current_system_prompt = chat_history[0] # Get the latest system prompt
        chat_history = [current_system_prompt]
        log.info("History reset to system prompt without summarization.")
        return

    summary_input_messages = [
        {
            "role": "system",
            "content": """
            You are a summarization engine. Condense the below conversation into a concise summary that explains the previous actions taken by the assistant.
            Focus on game progress, goals attempted, and significant events.
            Speak in first person ("I did...", "I went...").
            Be concise, ideally under 300 words. Avoid listing every single button press.
            Do not include any JSON code like {"action": ...}.
            """
        }
    ] + assistant_messages_to_summarize # Add the filtered assistant messages


    # --- Calculate Input Tokens for Summarization ---
    summary_input_tokens = 0
    tokens_per_message = 3 # Standard overhead per message
    tokens_per_role = 1 # Estimated overhead per role tag
    try:
        for message in summary_input_messages:
            summary_input_tokens += tokens_per_message + tokens_per_role
            content = message.get('content', '')
            if isinstance(content, str):
                summary_input_tokens += count_tokens(content)
        summary_input_tokens += 3  # For priming assistant reply
        log.info(f"Summarization estimated input tokens: {summary_input_tokens}")
    except Exception as e:
         log.error(f"Error calculating summary input tokens: {e}", exc_info=True)
         summary_input_tokens = 0


    summary_text = "No summary generated." # Default
    summary_output_tokens = 0
    try:
        summary_resp = client.chat.completions.create(
            model= MODEL,
            messages=summary_input_messages,
            temperature=0.7,
            max_tokens=512,
        )
        if summary_resp.choices and summary_resp.choices[0].message.content:
            summary_text = summary_resp.choices[0].message.content.strip()
            summary_output_tokens = count_tokens(summary_text)
            log.info(f"LLM Summary generated ({summary_output_tokens} output tokens): {summary_text[:150]}...")
        else:
            log.warning("⚠️ LLM Summary: No choices or empty content returned.")
            summary_text = "Summary generation failed."

        # --- Add Summary Tokens to Session Total ---
        total_summary_tokens = summary_input_tokens + summary_output_tokens
        tokens_used_session += total_summary_tokens
        log.info(f"Summarization call used approx. {total_summary_tokens} tokens. Session total: {tokens_used_session}")

    except Exception as e:
        log.error(f"Error during LLM summarization call: {e}", exc_info=True)
        summary_text = "Error generating summary."
        # Don't add tokens if the call failed.

    # --- Update System Prompt and Reset History ---
    # Build the new system prompt with the generated (or error) summary
    prompt = build_system_prompt(summary_text)
    # Update the *first* message in the chat_history (which should be the system prompt)
    if chat_history and chat_history[0]['role'] == 'system':
         chat_history[0]['content'] = prompt
         log.info("System prompt updated with new summary.")
    else:
         log.error("Could not find system prompt at the start of chat history to update.")
         # Fallback: create a new system prompt message
         chat_history.insert(0, {"role": "system", "content": prompt})

    # Reset history to only the updated system prompt
    chat_history = [chat_history[0]] # Keep only the updated system prompt

    log.info("Chat history summarized and reset to system prompt.")
    response_count = 0 # Reset counter


# --- LLM Interaction Function ---
def llm_stream_action(state_data: dict, timeout: float = STREAM_TIMEOUT) -> str:
    """
    Sends the current game state (plus images) to the LLM, streams the response,
    records history, estimates tokens, extracts and returns the action command.
    """
    global response_count, tokens_used_session

    payload = copy.deepcopy(state_data)
    screenshot = payload.pop("screenshot", None)
    minimap    = payload.pop("minimap",    None)

    if not isinstance(payload, dict):
        log.error(f"Invalid state_data structure: {type(state_data)}")
        return None

    # --- Prepare API Message Content ---
    text_segment = {"type": "text", "text": json.dumps(payload)}
    current_content = [text_segment]
    image_count = 0
    has_screenshot = False
    has_minimap = False

    # Add images with full base64 data for the API call
    if screenshot and isinstance(screenshot.get("image_url"), dict):
        current_content.append({"type": "image_url", "image_url": screenshot["image_url"]})
        image_count += 1
        has_screenshot = True
    if minimap and isinstance(minimap.get("image_url"), dict):
         current_content.append({"type": "image_url", "image_url": minimap["image_url"]})
         image_count += 1
         has_minimap = True

    current_user_message = {"role": "user", "content": current_content}
    # Use the current chat_history + the new user message for the API call
    messages_for_api = chat_history + [current_user_message]

    # --- Calculate Input Tokens ---
    call_input_tokens = 0
    tokens_per_message = 3
    tokens_per_role = 1
    try:
        for message in messages_for_api:
            call_input_tokens += tokens_per_message + tokens_per_role
            message_content = message.get('content', '')
            if isinstance(message_content, str):
                call_input_tokens += count_tokens(message_content)
            elif isinstance(message_content, list):
                for item in message_content:
                    if isinstance(item, dict):
                        item_type = item.get('type')
                        if item_type == 'text':
                            call_input_tokens += count_tokens(item.get('text', ''))
                        elif item_type == 'image_url':
                            # Add estimated cost per image
                            call_input_tokens += IMAGE_TOKEN_COST_HIGH_DETAIL # Assuming high detail
        call_input_tokens += 3  # Priming assistant reply
        log.info(f"LLM call estimated input tokens: {call_input_tokens} ({image_count} images)")
    except Exception as e:
        log.error(f"Error calculating input tokens: {e}", exc_info=True)
        call_input_tokens = 0


    log.info(f"Sending request to LLM ({MODEL}). History length: {len(chat_history)} turns.")

    full_output = ""
    action = None
    call_output_tokens = 0

    try:
        response = client.chat.completions.create(
            model       = MODEL,
            messages    = messages_for_api,
            temperature = 1.0,
            max_tokens  = 2048,
            stream      = True
        )

        collected = []
        start_stream = time.time()

        log.info("LLM Stream:")
        print(">>> ", end="")

        for chunk in response:
            if time.time() - start_stream > timeout:
                print("\n[Stream timed out]")
                log.warning(f"LLM stream timed out after {timeout}s")
                break

            delta = chunk.choices[0].delta.content or ""
            finish_reason = chunk.choices[0].finish_reason

            if delta:
                print(delta, end="", flush=True)
                collected.append(delta)

            if finish_reason is not None:
                log.info(f"LLM stream finished. Reason: {finish_reason}")
                break

        print() # Newline after streaming

        full_output = "".join(collected).strip()
        log.info(f"LLM full output received ({len(full_output)} chars).")

        # --- Calculate Output Tokens & Update Session Total ---
        call_output_tokens = count_tokens(full_output)
        total_call_tokens = call_input_tokens + call_output_tokens
        tokens_used_session += total_call_tokens
        log.info(f"LLM call estimated output tokens: {call_output_tokens}. Total for call: {total_call_tokens}. Session total: {tokens_used_session}")


        # --- Add Interaction to History ---
        # User message with placeholders for history
        user_history_content = [text_segment]
        if has_screenshot: user_history_content.append({"type": "text", "text": "[Screenshot Placeholder]"})
        if has_minimap: user_history_content.append({"type": "text", "text": "[Minimap Placeholder]"})
        chat_history.append({"role": "user", "content": user_history_content})
        # Full assistant response
        chat_history.append({"role": "assistant", "content": full_output})

        response_count += 1
        log.info(f"Response count: {response_count}/{CLEANUP_WINDOW}")
        # Check if summarization is due *after* adding the latest response
        if response_count >= CLEANUP_WINDOW:
            summarize_and_reset() # This resets response_count to 0

        # --- Action Extraction ---
        action = None
        # 1) Try JSON pull (more robust regex)
        try:
            json_match = re.search(r'(\{[\s\S]*?\})\s*$', full_output) # Find last json block
            if json_match:
                json_candidate = json_match.group(1)
                try:
                    parsed_json = json.loads(json_candidate)
                    action_from_json = parsed_json.get("action")
                    if isinstance(action_from_json, str) and action_from_json:
                        if ACTION_RE.match(action_from_json):
                             log.info(f"Extracted action via JSON: {action_from_json}")
                             action = action_from_json
                        else:
                             log.warning(f"JSON action '{action_from_json}' failed validation regex.")
                    # Allow empty action string from JSON? Probably not desired.
                    # else:
                    #     log.warning("JSON object found, but 'action' key missing, empty, or not a string.")
                except json.JSONDecodeError as json_err:
                     log.warning(f"Failed to decode potential JSON block: {json_err}. Block: '{json_candidate[:100]}...'")
            # else: log.debug("No JSON object found at the end of LLM output.")

        except Exception as e:
            log.error(f"Error during JSON action extraction: {e}", exc_info=True)

        # 2) Fallback: Last line regex match
        if action is None:
            log.info("JSON action extraction failed or invalid, attempting fallback: Last line regex match.")
            lines = [line.strip() for line in full_output.splitlines() if line.strip()]
            if lines:
                last_line = lines[-1]
                # Avoid using the line if it looks like the failed JSON
                if not last_line.startswith('{') and ACTION_RE.match(last_line):
                    log.info(f"Extracted action via fallback (last line regex): {last_line}")
                    action = last_line
                else:
                    log.warning(f"Fallback failed: Last line '{last_line}' did not match action regex or looked like JSON.")
            else:
                log.warning("Fallback failed: LLM output contained no non-empty lines.")

    except Exception as e:
        log.error(f"Error during LLM API call or streaming: {e}", exc_info=True)
        # Record the error in history?
        # chat_history.append({"role": "user", "content": current_user_message['content']}) # Store the query that failed
        # chat_history.append({"role": "assistant", "content": f"[ERROR DURING LLM CALL: {e}]"})

    if action is None:
         log.error("Failed to extract a valid action from LLM response.")

    return action


# --- Main Execution Loop ---
async def run_auto_loop(sock, state: dict, broadcast_func, interval: float = 8.0):
    """
    Main async loop: capture state, encode images, call LLM, update & broadcast state, send action.
    Logs actions taken to the state update.
    """
    global action_count, tokens_used_session, start_time # Use globals for state updates
    log.info("Starting async auto LLM-driven loop.")
    try:
        while True:
            loop_start_time = time.time()
            current_cycle = action_count + 1 # Cycle number for this iteration
            log.info(f"--- Loop Iteration {current_cycle} ---")
            update_payload = {} # Store changes to broadcast for this iteration

            # 1. Get Game State from mGBA
            log.info("Requesting game state from mGBA...")
            mGBA_state_start = time.time()
            try:
                current_mGBA_state = prep_llm(sock)
                if not current_mGBA_state:
                    log.error("Failed to get state from mGBA (prep_llm returned None). Skipping iteration.")
                    await asyncio.sleep(max(0, interval - (time.time() - loop_start_time)))
                    continue
                log.info(f"Received game state from mGBA (took {time.time() - mGBA_state_start:.2f}s).")
            except socket.error as se:
                 log.error(f"Socket error getting state from mGBA: {se}. Stopping loop.")
                 break
            except Exception as e:
                log.error(f"Error getting state from mGBA: {e}", exc_info=True)
                await asyncio.sleep(max(0, interval - (time.time() - loop_start_time)))
                continue

            # 2. Update Shared State (Part 1 - Before LLM Call)
            state_update_start = time.time()
            log.info("Updating shared state (pre-LLM)...")

            # --- Map mGBA state to shared state structure ---
            # (This logic remains the same as before)
            if 'party' in current_mGBA_state:
                 if json.dumps(current_mGBA_state['party']) != json.dumps(state.get('currentTeam')):
                     state['currentTeam'] = current_mGBA_state['party']
                     update_payload['currentTeam'] = state['currentTeam']
                     log.info("State Update: currentTeam")
            if 'badges' in current_mGBA_state:
                 num_badges = 0
                 badge_data = current_mGBA_state['badges']
                 if isinstance(badge_data, list): num_badges = len(badge_data)
                 elif isinstance(badge_data, int): num_badges = badge_data
                 elif isinstance(badge_data, str):
                     try: num_badges = int(badge_data)
                     except ValueError: log.warning(f"Could not parse badge count from string: {badge_data}")
                 badge_list = [{} for _ in range(num_badges)]
                 if badge_list != state.get('badges'):
                     state['badges'] = badge_list
                     update_payload['badges'] = state['badges']
                     log.info(f"State Update: badges ({len(badge_list)})")

            loc_str = "Unknown"
            if current_mGBA_state.get('position'):
                 x, y = current_mGBA_state['position']
                 map_id_str = current_mGBA_state.get('map_id', 'N/A')
                 map_name = current_mGBA_state.get('map_name', '')
                 pos_str = f"({x}, {y})"
                 loc_str = f"{map_name} (Map {map_id_str}) {pos_str}" if map_name else f"Map {map_id_str} {pos_str}"
            if loc_str != state.get('minimapLocation'):
                 state['minimapLocation'] = loc_str
                 update_payload['minimapLocation'] = state['minimapLocation']
                 log.info(f"State Update: minimapLocation -> {loc_str}")

            # --- Update loop-dependent fields ---
            state['actions'] = current_cycle # Update action count for the current cycle
            update_payload['actions'] = state['actions']

            state['tokensUsed'] = tokens_used_session # Reflect total before this call
            update_payload['tokensUsed'] = state['tokensUsed']

            elapsed = datetime.datetime.now() - start_time
            hours, remainder = divmod(elapsed.total_seconds(), 3600)
            minutes, seconds = divmod(remainder, 60)
            game_status_str = f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
            if game_status_str != state.get('gameStatus'):
                state['gameStatus'] = game_status_str
                update_payload['gameStatus'] = state['gameStatus']

            if MODEL != state.get('modelName'):
                state['modelName'] = MODEL
                update_payload['modelName'] = state['modelName']

            # --- Goal Update Logic ---
            current_badges = len(state.get('badges', []))
            primary_goal = state.get("goals", {}).get("primary", "")
            if current_badges >= 8 and not primary_goal.startswith("Become the Pokemon League Champion"):
                 log.info("***** ALL BADGES OBTAINED - UPDATING PRIMARY GOAL *****")
                 state["goals"] = { # Ensure goals dict exists
                     "primary": "Become the Pokemon League Champion!",
                     "secondary": ["Travel to the Indigo Plateau.", "Defeat the Elite Four."],
                     "tertiary": "Train Pokemon to level 100."
                 }
                 update_payload["goals"] = state["goals"]

            log.info(f"Pre-LLM state update finished (took {time.time() - state_update_start:.2f}s).")


            # 3. Prepare LLM Input (including images)
            prep_llm_start = time.time()
            llm_input_state = copy.deepcopy(current_mGBA_state)
            has_screenshot = False
            screenshot_path = "latest.png"
            if os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0:
                try:
                    with open(screenshot_path, "rb") as f: b64_ss = base64.b64encode(f.read()).decode("utf-8")
                    llm_input_state["screenshot"] = {"image_url": {"url": f"data:image/png;base64,{b64_ss}", "detail": "high"}}
                    has_screenshot = True
                except Exception as e: log.error(f"Error reading/encoding screenshot '{screenshot_path}': {e}")
            if not has_screenshot: llm_input_state["screenshot"] = None

            has_minimap = False
            minimap_path = "minimap.png"
            if os.path.exists(minimap_path) and os.path.getsize(minimap_path) > 0:
                 try:
                     with open(minimap_path, "rb") as f: b64_mm = base64.b64encode(f.read()).decode("utf-8")
                     llm_input_state["minimap"] = {"image_url": {"url": f"data:image/png;base64,{b64_mm}", "detail": "high"}}
                     has_minimap = True
                 except Exception as e: log.error(f"Error reading/encoding minimap '{minimap_path}': {e}")
            if not has_minimap: llm_input_state["minimap"] = None
            log.info(f"LLM input preparation finished (took {time.time() - prep_llm_start:.2f}s). SS:{has_screenshot}, MM:{has_minimap}")


            # 4. Call LLM
            llm_call_start = time.time()
            log.info("Calling LLM for next action...")
            action = llm_stream_action(llm_input_state) # This updates tokens_used_session internally
            log.info(f"LLM call finished (took {time.time() - llm_call_start:.2f}s).")


            # 5. Send Action to mGBA & Update Log Entry
            action_to_send = None
            log_action_text = "No action taken." # Default log text part

            if action:
                action_to_send = action
                log_action_text = f"Action: {action}"
                log.info(f"LLM proposed action: {action}")
                send_action_start = time.time()
                try:
                    sock.sendall((action_to_send + "\n").encode("utf-8"))
                    log.info(f"{log_action_text} sent to mGBA.")
                except socket.error as se:
                    log.error(f"Socket error sending action '{action_to_send}' to mGBA: {se}. Stopping loop.")
                    break
                except Exception as e:
                    log.error(f"Unexpected error sending action '{action_to_send}': {e}", exc_info=True)
                    # Decide if we should break here too
                log.info(f"Action sending finished (took {time.time() - send_action_start:.2f}s).")
            else:
                log.error("No valid action received from LLM. Sending fallback.")

            # --- Update Log Entry with Action Taken ---
            log_id_counter = state.get("log_id_counter", 85650) + 1
            state["log_id_counter"] = log_id_counter
            # Combine cycle info, location, tokens, and the action taken
            log_text = f"[Cycle {current_cycle}] Loc: {loc_str}. {log_action_text}. (Session Tokens: {tokens_used_session})"
            new_log = { "id": log_id_counter, "text": log_text }
            update_payload["log_entry"] = new_log # Add/overwrite log entry for broadcast
            log.info(f"State Update: Adding Log Entry #{log_id_counter} with action.")

            # --- Update Action Count State (Reflects completed cycle) ---
            action_count = current_cycle # Update global counter after successful cycle completion
            state['actions'] = action_count # Ensure state reflects the *completed* action count
            update_payload['actions'] = state['actions'] # Update payload if changed

            # --- Update Token Count State (Reflects tokens *after* LLM call) ---
            if state['tokensUsed'] != tokens_used_session:
                 state['tokensUsed'] = tokens_used_session
                 update_payload['tokensUsed'] = state['tokensUsed'] # Add to payload if updated since start of loop

            # 6. Broadcast State Updates via WebSocket
            broadcast_start = time.time()
            if update_payload:
                log.info(f"Broadcasting state updates ({len(update_payload)} keys): {list(update_payload.keys())}")
                try:
                    # Ensure broadcast is awaited if it's an async function
                    if asyncio.iscoroutinefunction(broadcast_func):
                        await broadcast_func(update_payload)
                    else:
                         # This might block the loop if not truly async
                         broadcast_func(update_payload)
                    log.info(f"Broadcast finished (took {time.time() - broadcast_start:.2f}s).")
                except Exception as e:
                    log.error(f"Error during WebSocket broadcast: {e}", exc_info=True)
            else:
                 log.info("No state changes detected to broadcast this cycle.") # Should be rare now with logs/counters


            # 7. Wait for Next Cycle
            elapsed_loop_time = time.time() - loop_start_time
            wait_time = max(0, interval - elapsed_loop_time)
            log.info(f"Loop cycle {current_cycle} took {elapsed_loop_time:.2f}s. Waiting for {wait_time:.2f}s...")
            await asyncio.sleep(wait_time)

    except asyncio.CancelledError:
        log.info("Auto loop task cancelled.")
    except Exception as e:
        log.error(f"Critical error in auto loop: {e}", exc_info=True)
    finally:
        log.info("Auto loop finished or terminated.")
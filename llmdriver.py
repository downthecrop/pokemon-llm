import os
import json
import time
import base64
import copy
import asyncio
import datetime
import logging
import tiktoken
import socket
import re

from openai import OpenAI, APIError
from dotenv import load_dotenv
from helpers import prep_llm

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger('llmdriver')


ACTION_RE = re.compile(r'^[LRUDABS](?:;[LRUDABS])*(?:;)?$')
ANALYSIS_RE = re.compile(r"<game_analysis>([\s\S]*?)</game_analysis>", re.IGNORECASE)
STREAM_TIMEOUT = 30
CLEANUP_WINDOW = 5
IMAGE_TOKEN_COST_HIGH_DETAIL = 258
IMAGE_TOKEN_COST_LOW_DETAIL = 85
SCREENSHOT_PATH = "latest.png"
MINIMAP_PATH = "minimap.png"


load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
MODE = "GEMINI" # OPENAI or GEMINI
IMAGE_DETAIL = "low" # high or low

client = None
MODEL = None

if MODE == "OPENAI":
    if not OPENAI_KEY:
        log.error("MODE is OPENAI but OPENAI_API_KEY not found in environment variables.")
        raise ValueError("Missing OpenAI API Key")
    client = OpenAI(api_key=OPENAI_KEY)
    MODEL = "gpt-4.1-nano"
    log.info(f"Using OpenAI Mode. Model: {MODEL}")
elif MODE == "GEMINI":
    if not GEMINI_KEY:
        log.error("MODE is GEMINI but GEMINI_API_KEY not found in environment variables.")
        raise ValueError("Missing Gemini API Key")
    client = OpenAI(
        api_key=GEMINI_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )

    MODEL = "gemini-2.5-flash-preview-04-17"
    log.info(f"Using Gemini Mode (via OpenAI client). Model: {MODEL}")
else:
    log.error(f"Invalid MODE selected: {MODE}")
    raise ValueError(f"Invalid MODE: {MODE}")


try:
    encoding = tiktoken.get_encoding("cl100k_base")
    log.info("Tiktoken encoder 'cl100k_base' loaded.")
except Exception as e:
    log.warning(f"Failed to load tiktoken encoder: {e}. Token counts will be approximate (char/4).")
    encoding = None


def count_tokens(text: str) -> int:
    """Estimates token count for a given text using the loaded encoding."""
    if not text: return 0
    if not encoding: return len(text) // 4
    try:
        return len(encoding.encode(text))
    except Exception as e:
        log.warning(f"Tiktoken encoding failed (len {len(text)}): {e}. Using fallback.")
        return len(text) // 4


def build_system_prompt(actionSummary: str) -> str:
    """Constructs the system prompt for the LLM, including the chat history summary."""
    summary_limit = 1500
    truncated_summary = actionSummary
    if len(actionSummary) > summary_limit:
        truncated_summary = actionSummary[:summary_limit] + "... (truncated)"
        log.warning(f"Action summary truncated for system prompt (>{summary_limit} chars).")

    return f"""
        You are an AI agent designed to play Pokémon Red. Your task is to analyze the game state, plan your actions, and provide input commands to progress through the game.

        Your previous actions summary: {truncated_summary}

        General Instructions:

        - If given the option to continue or start a new game, always choose to continue.
        - Speak in the first person as if you were the player. You don't see a screenshots or the screen, you see your surroundings.
        - Do not call it a screenshot or the screen. It's your world.

        1. Analyze the Game State:
        - Examine the screenshot provided in the game state.
        - Check the minimap (if available) to understand your position in the broader game world and the walkability of the terrain.
        - Identify nearby terrain, objects, and NPCs.
        - Use the grid system to determine relative positions. Your character is always at [4,4] on the screen grid (bottom left cell is [0,0]).
        - List out all visible objects, NPCs, and terrain features in the screenshot. Translate them to world coordinates (based on your position).
        - Print any text that appears in the screenshot, including dialogue boxes, signs, or other text.
        - The screenshot is the most accurate representation of the game state. Not the minimap or chat context.

        2. Plan Your Actions:
        - Consider your current goals in the game (e.g., reaching a specific location, interacting with an NPC, progressing the story).
        - Ensure your planned actions don't involve walking into walls, fences, trees, or other obstacles.

        3. Navigation and Interaction:
        - Movement is always relative to the screen space: U (up), D (down), L (left), R (right).
        - WALKABLE gridspaces on the minimap are WHITE, NONWALKABLE are (BLACK).
        - To interact with objects or NPCs, move directly beside them (no diagonal interactions) and press A.
        - Align yourself properly with doors and stairs before attempting to use them.
        - Remember that you can't move through walls or objects.
        - You can only pass ledges by moving DOWN, never UP.
        - If you repeartedly try the same action and it fails, explore other options.
        - When in a city, orange areas on the minimap idenify buildings you can enter.
        - Use the screenshot to ensure your planned actions are not blocked. Verify with the minimap that your path is walkable.
        - You must be perfectly aligned on the grid with orange minimap tiles to enter/exit buildings. Diagonally adjacent is not enough.
        - Exits, stairs, and ladders are ALWAYS marked by a unique tile type.
        - You cannot move when an interface is open, you must close or complete the interaction it first.
        - If you want to leave a building or room you must find the unique exit tile (marked in orange on the minimap).
        - Orange tiles on the minimap are exits, stairs, and ladders. If you are not on an orange tile, you cannot exit the room.
        - Stairs, Doors and Ladders do not require 'A' to interact. You simply walk into them.

        4. Menu Navigation:
        - Press S to open the pause menu.
        - Use U/D/L/R to move the selection cursor, A to confirm, and B to cancel or go back.

        5. Command Chaining:
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

        Example output structure (ALWAYS match this format):

        "
        <game_analysis>
        [Your detailed analysis and planning goes here]
        </game_analysis>

        {{"action":"U;R;R;D;"}}
        "


        Remember:
        - Always use both the screenshot and minimap for navigation is available.
        - Be careful to align properly with doors and entrances/exits.
        - Avoid repeatedly walking into walls or obstacles. If an action yields no result, try a different approach.

        Now, analyze the game state and decide on your next action. Your final output should consist only of the JSON object with the action and should not duplicate or rehash any of the work you did in the thinking block.

        Here is the current game state:
        """


chat_history = [{"role": "system", "content": build_system_prompt("")}]
response_count = 0
action_count = 0
tokens_used_session = 0
start_time = datetime.datetime.now()


def cleanup_image_history():
    """Replaces image_url data with text placeholders in chat history."""
    for msg in chat_history:
        if isinstance(msg.get("content"), list):
            new_content = []
            has_image = False
            for seg in msg["content"]:
                if isinstance(seg, dict) and seg.get("type") == "image_url":
                    if not has_image:
                        new_content.append({"type": "text", "text": "[Image Content Removed]"})
                        has_image = True
                else:
                    new_content.append(seg)
            msg["content"] = new_content

def calculate_prompt_tokens(messages):
    """Estimates token count for a list of messages."""
    tokens = 0
    tokens_per_message = 3
    tokens_per_role = 1
    try:
        for message in messages:
            tokens += tokens_per_message + tokens_per_role
            content = message.get('content', '')
            if isinstance(content, str):
                tokens += count_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get('type')
                        if item_type == 'text':
                            tokens += count_tokens(item.get('text', ''))
                        elif item_type == 'image_url':
                            tokens += IMAGE_TOKEN_COST_HIGH_DETAIL
        tokens += 3
        return tokens
    except Exception as e:
         log.error(f"Error calculating prompt tokens: {e}", exc_info=True)
         return 0

def summarize_and_reset():
    """Condenses history, updates system prompt, resets history, accounts for tokens."""
    global chat_history, response_count, tokens_used_session

    log.info(f"Summarizing chat history ({len(chat_history)} messages)...")


    history_for_summary = []
    for msg in chat_history:
        if msg['role'] == 'assistant':
            history_for_summary.append(msg)


    if not history_for_summary:
        log.info("No relevant assistant messages to summarize, skipping summarization call.")

        current_system_prompt = chat_history[0]
        chat_history = [current_system_prompt]
        response_count = 0
        log.info("History reset to system prompt without summarization.")
        return

    summary_prompt = """
        You are a summarization engine. Condense the below conversation into a concise summary that explains the previous actions taken by the assistant player.
        Focus on game progress, goals attempted, locations visited, and significant events.
        Speak in first person ("I explored...", "I tried to go...", "I obtained...").
        Be concise, ideally under 300 words. Avoid listing every single button press.
        Do not include any JSON code like {"action": ...}. Output only the summary text.
    """
    summary_input_messages = [{"role": "system", "content": summary_prompt}] + history_for_summary

    summary_input_tokens = calculate_prompt_tokens(summary_input_messages)
    log.info(f"Summarization estimated input tokens: {summary_input_tokens}")

    summary_text = "Error generating summary."
    summary_output_tokens = 0
    try:
        summary_resp = client.chat.completions.create(
            model=MODEL,
            messages=summary_input_messages,
            temperature=0.7,
            max_tokens=512,
        )
        if summary_resp.choices and summary_resp.choices[0].message.content:
            summary_text = summary_resp.choices[0].message.content.strip()
            summary_output_tokens = count_tokens(summary_text)
            log.info(f"LLM Summary generated ({summary_output_tokens} tokens): {summary_text[:150]}...")
        else:
            log.warning("LLM Summary: No choices or empty content.")
            summary_text = "Summary generation failed."

        total_summary_tokens = summary_input_tokens + summary_output_tokens
        tokens_used_session += total_summary_tokens
        log.info(f"Summarization call used approx. {total_summary_tokens} tokens. Session total: {tokens_used_session}")

    except APIError as e:
        log.error(f"API error during LLM summarization: {e}")
    except Exception as e:
        log.error(f"Error during LLM summarization call: {e}", exc_info=True)


    new_system_prompt_content = build_system_prompt(summary_text)
    chat_history = [{"role": "system", "content": new_system_prompt_content}]
    response_count = 0
    log.info("Chat history summarized and reset.")



def llm_stream_action(state_data: dict, timeout: float = STREAM_TIMEOUT):
    global response_count, tokens_used_session

    did_cleanup = False

    payload = copy.deepcopy(state_data)
    screenshot = payload.pop("screenshot", None)
    minimap = payload.pop("minimap", None)

    if not isinstance(payload, dict):
        log.error(f"Invalid state_data structure: {type(state_data)}")
        return None, None

    text_segment = {"type": "text", "text": json.dumps(payload)}
    current_content = [text_segment]
    image_parts_for_api = []
    image_placeholders_for_history = []

    if screenshot and isinstance(screenshot.get("image_url"), dict):
        image_parts_for_api.append({"type": "image_url", "image_url": screenshot["image_url"]})
    if minimap and isinstance(minimap.get("image_url"), dict):
        image_parts_for_api.append({"type": "image_url", "image_url": minimap["image_url"]})

    current_content.extend(image_parts_for_api)
    current_user_message_api = {"role": "user", "content": current_content}
    messages_for_api = chat_history + [current_user_message_api]


    call_input_tokens = calculate_prompt_tokens(messages_for_api)
    log.info(f"LLM call estimate: {call_input_tokens} input tokens ({len(image_parts_for_api)} images). History: {len(chat_history)} turns.")

    full_output = ""
    action = None
    analysis_text = None
    call_output_tokens = 0

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages_for_api,
            temperature=1.0,
            max_tokens=2048,
            stream=True
        )

        collected_chunks = []
        stream_start_time = time.time()
        log.info("LLM Stream:")
        print(">>> ", end="", flush=True)

        for chunk in response:
            if time.time() - stream_start_time > timeout:
                print("\n[TIMEOUT]", flush=True)
                log.warning(f"LLM stream timed out after {timeout}s")
                break

            delta_content = chunk.choices[0].delta.content
            finish_reason = chunk.choices[0].finish_reason

            if delta_content:
                print(delta_content, end="", flush=True)
                collected_chunks.append(delta_content)

            if finish_reason:
                print(f"\n[END - {finish_reason}]", flush=True)
                log.info(f"LLM stream finished. Reason: {finish_reason}")
                break
        else:

             print("\n[END - No Finish Reason]", flush=True)
             log.warning("LLM stream finished without a finish_reason.")


        full_output = "".join(collected_chunks).strip()
        log.info(f"LLM raw output ({len(full_output)} chars).")


        call_output_tokens = count_tokens(full_output)
        total_call_tokens = call_input_tokens + call_output_tokens
        tokens_used_session += total_call_tokens
        log.info(f"LLM call tokens: ~{call_output_tokens} output, ~{total_call_tokens} total. Session: {tokens_used_session}")


        user_history_content = [text_segment] + image_placeholders_for_history
        chat_history.append({"role": "user", "content": user_history_content})
        chat_history.append({"role": "assistant", "content": full_output})

        response_count += 1
        log.info(f"Response count: {response_count}/{CLEANUP_WINDOW}")
        if response_count >= CLEANUP_WINDOW:
            summarize_and_reset()
            did_cleanup = True


        analysis_match = ANALYSIS_RE.search(full_output)
        if analysis_match:
            analysis_text = analysis_match.group(1).strip()
            log.info(f"Extracted game analysis ({len(analysis_text)} chars).")
        else:
            log.warning("Could not find <game_analysis> block in LLM output.")
            analysis_text = None


        try:
            json_match = re.search(r'(\{[\s\S]*?\})\s*$', full_output)
            if json_match:
                json_candidate = json_match.group(1)
                try:
                    parsed_json = json.loads(json_candidate)
                    action_from_json = parsed_json.get("action")
                    if isinstance(action_from_json, str) and ACTION_RE.match(action_from_json):
                        action = action_from_json
                        log.info(f"Extracted action via JSON: {action}")

                except json.JSONDecodeError:
                     log.warning(f"Failed to decode potential JSON: '{json_candidate[:100]}...'")


            if action is None:
                lines = [line.strip() for line in full_output.splitlines() if line.strip()]
                if lines:
                    last_line = lines[-1]

                    if not last_line.startswith('<game_analysis>') and not last_line.endswith('</game_analysis>'):
                        if not last_line.startswith('{') and ACTION_RE.match(last_line):
                            action = last_line
                            log.info(f"Extracted action via fallback (last line regex): {action}")



        except Exception as e:
            log.error(f"Error during action extraction: {e}", exc_info=True)

    except APIError as e:
        log.error(f"API error during LLM call: {e}")

        return None, None
    except Exception as e:
        log.error(f"Error during LLM API call/streaming: {e}", exc_info=True)

        return None, None

    if action is None:
         log.error("Failed to extract a valid action from LLM response.")

    return action, analysis_text, did_cleanup



def encode_image_base64(image_path: str) -> str | None:
    """Reads an image file and returns its base64 encoded string."""
    if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
        return None
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        log.error(f"Error reading/encoding image '{image_path}': {e}")
        return None


async def run_auto_loop(sock, state: dict, broadcast_func, interval: float = 8.0):
    """Main async loop: Get state, call LLM, send action, update/broadcast state."""
    global action_count, tokens_used_session, start_time
    log.info(f"Starting async auto LLM loop (Interval: {interval}s). Mode: {MODE}, Model: {MODEL}")

    while True:
        loop_start_time = time.time()
        current_cycle = action_count + 1
        log.info(f"--- Loop Cycle {current_cycle} ---")

        update_payload = {}
        action_payload = {}
        did_cleanup_payload = {}

        try:
            log.debug("Requesting game state from mGBA...")
            current_mGBA_state = prep_llm(sock)
            if not current_mGBA_state:
                log.error("Failed to get state from mGBA (prep_llm returned None). Skipping.")
                await asyncio.sleep(max(0, interval - (time.time() - loop_start_time)))
                continue
            log.debug("Received game state from mGBA.")
        except socket.timeout:
             log.error("Socket timeout getting state from mGBA. Stopping loop.")
             break
        except socket.error as se:
             log.error(f"Socket error getting state from mGBA: {se}. Stopping loop.")
             break
        except Exception as e:
            log.error(f"Error getting state from mGBA: {e}", exc_info=True)
            await asyncio.sleep(max(0, interval - (time.time() - loop_start_time)))
            continue


        llm_input_state = copy.deepcopy(current_mGBA_state)
        state_update_start = time.time()


        new_team = current_mGBA_state.get('party')
        if new_team is not None and json.dumps(new_team) != json.dumps(state.get('currentTeam')):
            state['currentTeam'] = new_team
            update_payload['currentTeam'] = state['currentTeam']
            log.info("State Update: currentTeam")


        badge_data = current_mGBA_state.get('badges')
        num_badges = 0
        if isinstance(badge_data, (list, int, str)):
             try: num_badges = int(badge_data) if isinstance(badge_data, (int, str)) else len(badge_data)
             except ValueError: log.warning(f"Invalid badge data format: {badge_data}")
        new_badges_list = [{} for _ in range(num_badges)]
        if new_badges_list != state.get('badges'):
             state['badges'] = new_badges_list
             update_payload['badges'] = state['badges']
             log.info(f"State Update: badges ({len(new_badges_list)})")


        pos = current_mGBA_state.get('position')
        map_id = current_mGBA_state.get('map_id', 'N/A')
        map_name = current_mGBA_state.get('map_name', '')
        loc_str = "Unknown"
        if pos:
            loc_str = f"{map_name} (Map {map_id}) ({pos[0]}, {pos[1]})" if map_name else f"Map {map_id} ({pos[0]}, {pos[1]})"
        if loc_str != state.get('minimapLocation'):
            state['minimapLocation'] = loc_str
            update_payload['minimapLocation'] = state['minimapLocation']
            log.info(f"State Update: minimapLocation -> {loc_str}")


        current_badges_count = len(state.get('badges', []))
        primary_goal = state.get("goals", {}).get("primary", "")
        if current_badges_count >= 8 and not primary_goal.startswith("Become the Pokemon League Champion"):
            log.info("***** ALL BADGES OBTAINED - UPDATING PRIMARY GOAL *****")
            state["goals"] = {
                "primary": "Become the Pokemon League Champion!",
                "secondary": ["Travel to the Indigo Plateau.", "Defeat the Elite Four."],
                "tertiary": "Train Pokemon to level 100."
            }
            update_payload["goals"] = state["goals"]
        else:
            log.info("***** Debug UPDATING PRIMARY GOAL *****")
            state["goals"] = {
                "primary": "Become the Pokemon League Champion!",
                "secondary": ["Travel to the Indigo Plateau.", "Defeat the Elite Four."],
                "tertiary": "Train Pokemon to level 100."
            }
            update_payload["goals"] = state["goals"]


        b64_ss = encode_image_base64(SCREENSHOT_PATH)
        if b64_ss: llm_input_state["screenshot"] = {"image_url": {"url": f"data:image/png;base64,{b64_ss}", "detail": IMAGE_DETAIL}}
        else: llm_input_state["screenshot"] = None

        b64_mm = encode_image_base64(MINIMAP_PATH)
        if b64_mm: llm_input_state["minimap"] = {"image_url": {"url": f"data:image/png;base64,{b64_mm}", "detail": IMAGE_DETAIL}}
        else: llm_input_state["minimap"] = None
        log.debug(f"Pre-LLM state update & image prep took {time.time() - state_update_start:.2f}s. SS:{bool(b64_ss)}, MM:{bool(b64_mm)}")


        llm_call_start = time.time()

        log_id_counter = state.get("log_id_counter", 0) + 1
        state["log_id_counter"] = log_id_counter

        action, game_analysis, did_cleanup = llm_stream_action(llm_input_state)

        if did_cleanup:
            did_cleanup_payload["log_entry"] = { "id": log_id_counter, "text": "🔎 Chat history cleaned up." }
            await broadcast_func(did_cleanup_payload)

        log.debug(f"LLM call finished (took {time.time() - llm_call_start:.2f}s).")


        action_to_send = None
        log_action_text = "No action taken (LLM failed)."

        if action:
            action_to_send = action
            log_action_text = f"Action: {action}"
            log.info(f"LLM proposed action: {action}")
            try:
                sock.sendall((action_to_send + "\n").encode("utf-8"))
                log.debug(f"Action '{action_to_send}' sent to mGBA.")
            except socket.error as se:
                log.error(f"Socket error sending action '{action_to_send}': {se}. Stopping loop.")
                break
            except Exception as e:
                log.error(f"Unexpected error sending action '{action_to_send}': {e}", exc_info=True)

        else:
            log.error("No valid action from LLM. Cannot send command.")

        action_count = current_cycle
        if state.get('actions') != action_count:
             state['actions'] = action_count
             update_payload['actions'] = action_count

        if state.get('tokensUsed') != tokens_used_session:
            state['tokensUsed'] = tokens_used_session
            update_payload['tokensUsed'] = tokens_used_session

        elapsed = datetime.datetime.now() - start_time
        game_status_str = f"{int(elapsed.total_seconds() // 3600)}h {int((elapsed.total_seconds() % 3600) // 60)}m {int(elapsed.total_seconds() % 60)}s"
        if state.get('gameStatus') != game_status_str:
            state['gameStatus'] = game_status_str
            update_payload['gameStatus'] = game_status_str

        if state.get('modelName') != MODEL:
            state['modelName'] = MODEL
            update_payload['modelName'] = MODEL



        analysis_log_part = f"{game_analysis.strip()}\n" if game_analysis and game_analysis.strip() else None

        if analysis_log_part:
            update_payload["log_entry"] = { "id": log_id_counter, "text": analysis_log_part }
        if action:
            action_payload["log_entry"] = { "id": log_id_counter, "text": log_action_text }

        log.info(f"Log Entry #{log_id_counter}: {log_action_text} (Analysis included in state log)")

        if update_payload:
            log.debug(f"Broadcasting {len(update_payload)} state updates: {list(update_payload.keys())}")
            try:
                await broadcast_func(update_payload)
                await broadcast_func(action_payload)
            except Exception as e:
                log.error(f"Error during WebSocket broadcast: {e}", exc_info=True)


        elapsed_loop_time = time.time() - loop_start_time
        wait_time = max(0, interval - elapsed_loop_time)
        log.info(f"Cycle {current_cycle} took {elapsed_loop_time:.2f}s. Waiting {wait_time:.2f}s...")
        await asyncio.sleep(wait_time)


    log.info("Auto loop terminated.")
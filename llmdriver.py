import os
import json
import time
import base64
import copy
import asyncio
import datetime
import logging
import socket
import math
import re
import concurrent.futures
import functools

from helpers import prep_llm, touch_controls_path_find, parse_optional_fenced_json
from prompts import build_system_prompt, get_summary_prompt
from client_setup import setup_llm_client, DEFAULT_MODE
from token_coutner import count_tokens, calculate_prompt_tokens
from benchmark import Benchmark

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger('llmdriver')


ACTION_RE = re.compile(r'^[LRUDABS](?:;[LRUDABS])*(?:;)?$')
COORD_RE = re.compile(r'^([0-9]),([0-8])$')
ANALYSIS_RE = re.compile(r"<game_analysis>([\s\S]*?)</game_analysis>", re.IGNORECASE)
IS_LOCAL = DEFAULT_MODE == "LMSTUDIO" or DEFAULT_MODE == "OLLAMA"

if(IS_LOCAL):
    # Often slow inference
    STREAM_TIMEOUT = 120
else:
    STREAM_TIMEOUT = 60

CLEANUP_WINDOW = 10

SCREENSHOT_PATH = "latest.png"
MINIMAP_PATH = "minimap.png"

client, MODEL, IMAGE_DETAIL, supports_reasoning = setup_llm_client()
chat_history = []
response_count = 0
action_count = 0
tokens_used_session = 0
start_time = datetime.datetime.now()


# ─── Constants ────────────────────────────────────────────────────────────────
LLM_TOTAL_TIMEOUT = STREAM_TIMEOUT + 10     # e.g. 70 s / 130 s

# ─── Helper ───────────────────────────────────────────────────────────────────
async def call_llm_with_timeout(state_data: dict,
                                llm_timeout: float = STREAM_TIMEOUT,
                                total_timeout: float = LLM_TOTAL_TIMEOUT):
    """
    Run `llm_stream_action` in a worker thread and abort the whole thing
    (token‑counting, API call, streaming, parsing…) after `total_timeout` s.
    """
    loop = asyncio.get_running_loop()
    fn   = functools.partial(llm_stream_action, state_data, llm_timeout)

    try:
        # run blocking LLM code in a thread, wait with an asyncio timeout
        return await asyncio.wait_for(loop.run_in_executor(None, fn),
                                      timeout=total_timeout)
    except asyncio.TimeoutError:
        log.error(f"llm_stream_action exceeded {total_timeout}s – skipping cycle.")
        return None, None, None

def summarize_and_reset(benchmark: Benchmark = None):
    """Condenses history, updates system prompt, resets history, accounts for tokens."""
    global chat_history, response_count, tokens_used_session

    log.info(f"Summarizing chat history ({len(chat_history)} messages)...")


    history_for_summary = []

    # we convert from 'assistant' to 'user' since many API's don't like multiple 'assistant'
    # messages and will error out.
    for msg in chat_history:
        if msg['role'] == 'assistant':
            history_for_summary.append({
                'role': 'user',
                'content': msg['content']
            })


    if not history_for_summary:
        log.info("No relevant assistant messages to summarize, skipping summarization call.")

        current_system_prompt = chat_history[0]
        chat_history = [current_system_prompt]
        response_count = 0
        log.info("History reset to system prompt without summarization.")
        return None

    summary_prompt = get_summary_prompt()
    summary_input_messages = [{"role": "system", "content": summary_prompt}] + history_for_summary

    logging.info(f"Messages: {summary_input_messages}")

    summary_input_tokens = calculate_prompt_tokens(summary_input_messages)
    log.info(f"Summarization estimated input tokens: {summary_input_tokens}")

    summary_text = "Error generating summary."
    summary_output_tokens = 0
    try:
        summary_resp = client.chat.completions.create(
            model=MODEL,
            messages=summary_input_messages,
            temperature=0.7,
            max_tokens=2048,
        )
        if summary_resp.choices and summary_resp.choices[0].message.content:
            summary_text = summary_resp.choices[0].message.content.strip()
            summary_output_tokens = count_tokens(summary_text)
        else:
            log.warning("LLM Summary: No choices or empty content.")
            summary_text = "Summary generation failed."

        total_summary_tokens = summary_input_tokens + summary_output_tokens
        tokens_used_session += total_summary_tokens
        log.info(f"Summarization call used approx. {total_summary_tokens} tokens. Session total: {tokens_used_session}")

    except Exception as e:
        log.error(f"Error during LLM summarization call: {e}", exc_info=True)

    json_object = parse_optional_fenced_json(summary_text)
    
    log.info(f"LLM Summary generated ({summary_output_tokens} tokens): {str(json_object)}")

    benchInstructions = ""
    if(benchmark != None):
        benchInstructions = benchmark.instructions

    new_system_prompt_content = build_system_prompt(summary_text, benchInstructions)
    chat_history = [{"role": "system", "content": new_system_prompt_content}]
    response_count = 0
    log.info("Chat history summarized and reset.")
    return json_object


def next_with_timeout(iterator, timeout: float):
    """Attempt to pull the first chunk from `iterator` within `timeout` seconds."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: next(iterator))
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"No chunk received in {timeout}s")


def llm_stream_action(state_data: dict, timeout: float = STREAM_TIMEOUT, benchmark: Benchmark = None):
    global response_count, tokens_used_session

    summary_json = None
    payload = copy.deepcopy(state_data)
    screenshot = payload.pop("screenshot", None)
    minimap = payload.pop("minimap", None)

    if not isinstance(payload, dict):
        log.error(f"Invalid state_data structure: {type(state_data)}")
        return None, None, False

    # build the user message
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

    # token accounting
    call_input_tokens = calculate_prompt_tokens(messages_for_api)
    log.info(f"LLM call estimate: {call_input_tokens} input tokens; history turns: {len(chat_history)}")

    full_output = ""
    action = None
    analysis_text = None

    # common args for all models
    base_kwargs = {
        "model": MODEL,
        "messages": messages_for_api,
        "temperature": 1.0,
        "max_tokens": 2048,
        "stream": True,
    }

    # only add reasoning_effort if supported
    if supports_reasoning:
        base_kwargs["reasoning_effort"] = "low"

    try:
        response = client.chat.completions.create(**base_kwargs)

        iterator = iter(response)
        collected_chunks = []
        stream_start = time.time()
        log.info("LLM Stream starting…")
        print(">>> ", end="", flush=True)

        # --- first‐chunk timeout
        try:
            chunk = next_with_timeout(iterator, timeout)
        except:
            log.warning("TIMEOUT")
            return None, None, None

        # process first chunk
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
            collected_chunks.append(delta)
        if chunk.choices[0].finish_reason:
            print(f"\n[END - {chunk.choices[0].finish_reason}]", flush=True)
            log.info(f"LLM stream finished immediately: {chunk.choices[0].finish_reason}")
        else:
            # continue until finish or total timeout
            for chunk in iterator:
                if time.time() - stream_start > timeout:
                    print("\n[TIMEOUT]", flush=True)
                    log.warning(f"LLM stream timed out after {timeout}s total")
                    raise TimeoutError(f"Stream timed out after {timeout}s")

                delta = chunk.choices[0].delta.content
                if delta:
                    print(delta, end="", flush=True)
                    collected_chunks.append(delta)

                if chunk.choices[0].finish_reason:
                    print(f"\n[END - {chunk.choices[0].finish_reason}]", flush=True)
                    log.info(f"LLM stream finished: {chunk.choices[0].finish_reason}")
                    break

        # assemble and record
        full_output = "".join(collected_chunks).strip()
        log.info(f"LLM raw output length: {len(full_output)} chars")

        # token accounting
        output_tokens = count_tokens(full_output)
        tokens_used_session += call_input_tokens + output_tokens
        log.info(f"Used ~{output_tokens} output tokens; session total: {tokens_used_session}")

        # push into history
        user_hist = [text_segment] + image_placeholders_for_history
        chat_history.append({"role": "user", "content": user_hist})
        chat_history.append({"role": "assistant", "content": full_output})

        # cleanup threshold
        response_count += 1
        if response_count >= CLEANUP_WINDOW:
            summary_json = summarize_and_reset(benchmark)
            time.sleep(5)

        # extract analysis section
        match = ANALYSIS_RE.search(full_output)
        if match:
            analysis_text = match.group(1).strip()

        # extract action JSON or fallback
        json_match = re.search(r'(\{[\s\S]*?\})\s*$', full_output)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                act = parsed.get("action")
                touch = parsed.get("touch")
                if isinstance(act, str) and ACTION_RE.match(act):
                    action = act
                elif isinstance(touch, str) and COORD_RE.match(touch):
                    x, y = state_data["position"]
                    action = touch_controls_path_find(state_data["map_id"], [x, y],
                                                      [int(i) for i in touch.split(",")])
                    chat_history.append({'role': 'user', 'content': action})
            except json.JSONDecodeError:
                log.warning("Failed to parse trailing JSON for action.")
        if action is None:
            # fallback: last line matching ACTION_RE
            lines = [l.strip() for l in full_output.splitlines() if l.strip()]
            if lines and ACTION_RE.match(lines[-1]) and not lines[-1].startswith('{'):
                action = lines[-1]

    except Exception as e:
        log.error(f"Error during LLM streaming: {e}", exc_info=True)
        return None, None, None

    if action is None:
        log.error("No valid action extracted from LLM output.")

    return action, analysis_text, summary_json



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


async def run_auto_loop(sock, state: dict, broadcast_func, interval: float = 8.0, max_loops = math.inf, benchmark: Benchmark = None):
    """Main async loop: Get state, call LLM, send action, update/broadcast state."""
    global action_count, tokens_used_session, start_time, chat_history

    benchInstructions = ""
    if(benchmark != None):
        benchInstructions = benchmark.instructions
        logging.info(f"Added bench instructions: {benchInstructions}")
    chat_history = [{"role": "system", "content": build_system_prompt("", benchInstructions)}]

    while action_count < max_loops:
        loop_start_time = time.time()
        current_cycle = action_count + 1
        log.info(f"--- Loop Cycle {current_cycle} ---")

        update_payload = {}
        action_payload = {}

        try:
            log.debug("Requesting game state from mGBA...")
            current_mGBA_state = prep_llm(sock)

            if(benchmark != None):
                # check if we complted the bench
                if(benchmark.validation(current_mGBA_state)):
                    break

            #print(str(current_mGBA_state))
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

        #logging.info(f"History: {chat_history}")


        new_team = current_mGBA_state.get('party')
        if new_team is not None and json.dumps(new_team) != json.dumps(state.get('currentTeam')):
            state['currentTeam'] = new_team
            update_payload['currentTeam'] = state['currentTeam']
            log.info("State Update: currentTeam")


        badge_data = current_mGBA_state.get('badges')
        current_state_badges = state.get('badges')

        # Compare the new list with the stored list
        if badge_data != current_state_badges:
            log.info(f"State Update: Badges changed from {current_state_badges} to {badge_data}")
            state['badges'] = badge_data
            update_payload['badges'] = badge_data


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

        action, game_analysis, summary_json = await call_llm_with_timeout(llm_input_state)

        if summary_json is not None:
            tmp = {"log_entry": {"id": log_id_counter, "text": "🔎 Chat history cleaned up."}}
            await broadcast_func(tmp)

            required = ("primayGoal", "secondaryGoal", "tertiaryGoal", "otherNotes")

            if isinstance(summary_json, dict):
                # summary_json is dict, safe to check for keys
                missing = [k for k in required if k not in summary_json]
                if not missing:
                    state["goals"] = {
                        "primary":   summary_json["primayGoal"],
                        "secondary": summary_json["secondaryGoal"],
                        "tertiary":  summary_json["tertiaryGoal"],
                    }
                    state["otherGoals"] = summary_json["otherNotes"]
                    update_payload["goals"] = state["goals"]
                    update_payload["otherGoals"] = state["otherGoals"]
                else:
                    logging.error(f"Missing required goal keys in summary_json: {missing!r}")
            else:
                logging.error(f"Expected summary_json to be dict, but got {type(summary_json).__name__!r}")


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
    if(benchmark != None):
        benchmark.finalize(current_mGBA_state, MODEL)
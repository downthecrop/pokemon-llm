# --- websocket_service.py ---
import asyncio
import websockets
import json
import logging

WEBSOCKET_PORT = 8765

connected_clients = set()
log = logging.getLogger("websocket_service")

async def broadcast_message(message):
    """Sends a JSON message to all connected clients."""
    if not connected_clients:
        return

    message_json = json.dumps(message)
    # Use create_task for better concurrency handling if many clients exist
    send_tasks = [asyncio.create_task(client.send(message_json)) for client in connected_clients]
    if not send_tasks:
        return

    results = await asyncio.gather(*send_tasks, return_exceptions=True)

    disconnected_clients = set()
    clients_list = list(connected_clients) # Create stable list for indexing
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # Ensure index is valid before accessing clients_list
            if i < len(clients_list):
                client = clients_list[i]
                log.warning(f"WS: Failed to send to client {client.remote_address}: {result}. Removing.")
                disconnected_clients.add(client)
            else:
                 # This case should theoretically not happen with gather but added defensively
                 log.error(f"WS: Index {i} out of bounds for clients list of size {len(clients_list)} during error handling.")

    connected_clients.difference_update(disconnected_clients)


async def _send_full_state(websocket, current_app_state):
    """Sends the complete current state to a newly connected client."""
    try:
        # Ensure state is JSON serializable before sending
        state_copy = json.loads(json.dumps(current_app_state)) # Simple deep copy via JSON
        await websocket.send(json.dumps(state_copy))
        log.info(f"WS: Sent full initial state to {websocket.remote_address}")
    except websockets.exceptions.ConnectionClosed:
        log.warning(f"WS: Failed to send initial state to {websocket.remote_address}, client disconnected before send completed.")
    except TypeError as e:
         log.error(f"WS: State is not JSON serializable: {e}. State: {current_app_state}", exc_info=True)
    except Exception as e:
         log.error(f"WS: Error sending full state to {websocket.remote_address}: {e}", exc_info=True)


async def _actual_handler_code(websocket, current_app_state):
    """Core logic for handling a new WebSocket connection."""
    log.info(f"WS: Client connected: {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        await _send_full_state(websocket, current_app_state)
        # Keep connection open, listen for messages (currently ignored)
        async for message in websocket:
            log.info(f"WS: Received message from {websocket.remote_address}: {message} (ignored)")
            # Future: Handle client commands here (e.g., request state refresh, send input?)
    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"WS: Client {websocket.remote_address} disconnected gracefully.")
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning(f"WS: Client {websocket.remote_address} connection closed with error: {e}")
    except Exception as e:
        log.error(f"WS: Error in handler for {websocket.remote_address}: {e}", exc_info=True)
    finally:
        connected_clients.discard(websocket)
        log.info(f"WS: Client disconnected: {websocket.remote_address}. Remaining clients: {len(connected_clients)}")

async def run_server_forever(app_state_dict):
    """Starts the WebSocket server and keeps it running indefinitely."""
    
    # Define the handler that websockets.serve will call.
    # It captures app_state_dict from the outer scope (closure).
    async def handler_entrypoint(websocket):
        await _actual_handler_code(websocket, app_state_dict)

    async with websockets.serve(handler_entrypoint, "localhost", WEBSOCKET_PORT):
        log.info(f"WebSocket server running on ws://localhost:{WEBSOCKET_PORT}")
        await asyncio.Future() # Keep server running until cancelled
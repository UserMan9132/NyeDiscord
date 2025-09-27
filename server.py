# server.py
"""
Rottecord chat server - Railway / local friendly version.

Features:
- Uses PORT = int(os.environ.get("PORT", 50001)) so it works both locally and on PaaS.
- Handshake: username -> public_key length -> public_key bytes
- Stores per-client color sent once after handshake
- Broadcasts text messages and image payloads (JSON with type="image", data=base64)
- Sends player_list updates after join/leave
- Robust newline-delimited JSON framing (buffering)
- No interactive stdin thread (safe for hosted environments)
"""

import os
import socket
import threading
import json
import logging
from cryptography.hazmat.primitives import serialization

# -------------------------
# Config
# -------------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 50001))  # Railway / Render will set $PORT; fallback for local run

# Data stores
clients = {}               # username -> socket
clients_info = {}          # username -> metadata dict (e.g. {"color": (r,g,b)})
clients_public_keys = {}   # username -> public key PEM bytes

# Logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")


# -------------------------
# Helpers
# -------------------------
def get_local_ip():
    """Return a plausible local IP for logging purposes (doesn't send data)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "127.0.0.1"
    finally:
        try:
            s.close()
        except:
            pass
    return local_ip


def broadcast(message: str, sender_username=None):
    """
    Send `message` (string) + newline to all connected clients.
    If sending fails to a client, clean it up.
    """
    dead = []
    for user, conn in list(clients.items()):
        try:
            conn.sendall((message + "\n").encode("utf-8"))
        except Exception as e:
            logging.error(f"Broadcast to {user} failed: {e}")
            try:
                conn.close()
            except:
                pass
            dead.append(user)

    for d in dead:
        clients.pop(d, None)
        clients_public_keys.pop(d, None)
        clients_info.pop(d, None)


def broadcast_player_list():
    """Send the current players and their colors to everyone."""
    players = []
    for username in clients.keys():
        info = clients_info.get(username, {})
        players.append({
            "username": username,
            "color": info.get("color", (200, 200, 200))
        })
    msg = json.dumps({"type": "player_list", "players": players})
    broadcast(msg, sender_username=None)
    logging.debug(f"Broadcasted player list: {players}")


# -------------------------
# Client-handling
# -------------------------
def client_handshake(conn: socket.socket, addr):
    """
    Perform the handshake:
      - send username_request
      - receive username (UTF-8)
      - send public_key_request
      - receive 4-byte length, then public key PEM bytes of that length
      - receive a JSON color packet line (one-time)
      - broadcast join message and player list
      - start handle_client thread
    """
    try:
        logging.info(f"[{addr}] Starting handshake")

        # NOTE: Previously we tried to detect HTTP probes here (to satisfy web health checks).
        # That sometimes caused valid connections to be rejected when a proxy altered the stream.
        # For now we *do not* peek and reject — if you need to handle HTTP probes, enable
        # a small check here that detects "GET " / "HEAD " and replies a 200 then closes.

        # Username
        conn.send(b"username_request")
        username_raw = conn.recv(1024)
        if not username_raw:
            conn.close()
            return
        username = username_raw.decode("utf-8").strip()
        if not username:
            conn.close()
            return

        # store socket immediately so player_list can include them after color arrives
        clients[username] = conn
        logging.info(f"[{username}] ({addr}) connected (handshake step 1)")

        # Public key handshake: server asks client to send 4-byte length then pem bytes
        conn.send(b"public_key_request")

        pem_len_bytes = conn.recv(4)
        if not pem_len_bytes or len(pem_len_bytes) < 4:
            logging.error(f"No public key length from {username}")
            conn.close()
            clients.pop(username, None)
            return
        pem_len = int.from_bytes(pem_len_bytes, "big")
        logging.debug(f"Expecting public key length {pem_len} from {username}")

        chunks = []
        bytes_read = 0
        while bytes_read < pem_len:
            chunk = conn.recv(min(4096, pem_len - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)

        pub_pem = b"".join(chunks)
        if len(pub_pem) != pem_len:
            logging.error(f"Public key bytes mismatch for {username} (got {len(pub_pem)} expected {pem_len})")
            conn.close()
            clients.pop(username, None)
            return

        try:
            serialization.load_pem_public_key(pub_pem)
            clients_public_keys[username] = pub_pem
            logging.info(f"Public key for {username} loaded successfully")
        except Exception as e:
            logging.error(f"Invalid PEM from {username}: {e}")
            conn.close()
            clients.pop(username, None)
            return

        # Request color from client (one-time JSON line)
        try:
            color_line = conn.recv(4096).decode("utf-8")
            color_payload = json.loads(color_line)
            if "color" in color_payload:
                clients_info[username] = {"color": tuple(color_payload["color"])}
                logging.info(f"Stored color for {username}: {clients_info[username]['color']}")
            else:
                clients_info[username] = {"color": (200, 200, 200)}
        except Exception as e:
            logging.warning(f"Could not get color from {username}: {e}")
            clients_info[username] = {"color": (200, 200, 200)}

        # Broadcast join message and player list
        broadcast(json.dumps({
            "username": "Server",
            "color": (255, 0, 0),
            "message": f"{username} has joined the chat."
        }), sender_username=None)

        broadcast_player_list()

        # Start the per-client message loop
        threading.Thread(target=handle_client, args=(conn, addr, username), daemon=True).start()

    except Exception as e:
        logging.error(f"Handshake failed {addr}: {e}")
        try:
            conn.close()
        except:
            pass
        # cleanup if partially registered
        if 'username' in locals():
            clients.pop(username, None)
            clients_public_keys.pop(username, None)
            clients_info.pop(username, None)


def handle_client(conn: socket.socket, addr, username: str):
    """
    Read newline-delimited JSON messages from client and broadcast them.
    Handles image payloads (type="image") and normal text messages.
    """
    buffer = ""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                logging.info(f"[{username}] connection closed by peer")
                break

            buffer += data.decode("utf-8", errors="ignore")

            # process all complete newline-terminated messages
            while "\n" in buffer:
                raw_msg, buffer = buffer.split("\n", 1)
                raw_msg = raw_msg.strip()
                if not raw_msg:
                    continue

                try:
                    payload = json.loads(raw_msg)
                except json.JSONDecodeError as e:
                    logging.error(f"Failed to parse msg from {username}: {e}")
                    continue

                # Image type forwarding (clients will handle base64->image)
                if payload.get("type") == "image":
                    logging.info(f"[{username}] sent image {payload.get('filename')}")
                    broadcast(json.dumps(payload), sender_username=username)
                else:
                    # standard message
                    logging.info(f"[{username}] says: {payload.get('message')}")
                    broadcast(json.dumps(payload), sender_username=username)

    except Exception as e:
        logging.error(f"Error in handle_client for {username}: {e}")

    # cleanup after client disconnects
    try:
        conn.close()
    except:
        pass
    clients.pop(username, None)
    clients_public_keys.pop(username, None)
    clients_info.pop(username, None)
    logging.info(f"[{username}] disconnected")

    # broadcast left message + updated player list
    broadcast(json.dumps({
        "username": "Server",
        "color": (255, 0, 0),
        "message": f"{username} has left the chat."
    }), sender_username=None)
    broadcast_player_list()


# -------------------------
# Server main
# -------------------------
def start():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    local_ip = get_local_ip()
    logging.info("Server started!")
    logging.info(f"Running on {local_ip}:{PORT}, HOST: {HOST}")

    # Helpful hint for externally reachable hostname. Railway sets RAILWAY_STATIC_URL for web services,
    # but for TCP services you will be given a proxy host/port in the Railway dashboard (use that).
    external_hint = os.environ.get("RAILWAY_STATIC_URL") or os.environ.get("PUBLIC_HOST") or "check your Railway/TCP service dashboard"
    logging.info("======================================")
    logging.info(" Connect your client with:")
    logging.info(f"   {external_hint}:{PORT}")
    logging.info("======================================")

    # NOTE: We do not spawn the interactive server_commands thread in hosted environments.
    # (If you want it locally, you can implement an isatty() check or re-enable it here.)
    while True:
        conn, addr = server.accept()
        logging.info(f"[DEBUG] Accepted connection from {addr}")
        threading.Thread(target=client_handshake, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    start()

import os, base64, json, logging, threading, socket
from cryptography.hazmat.primitives import serialization

# =========================
# Config
# =========================
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 50001))  # ✅ Railway assigns a port if deployed

clients = {}
clients_info = {}
clients_public_keys = {}

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def broadcast_player_list():
    players = []
    for username in clients.keys():
        info = clients_info.get(username, {})
        players.append({
            "username": username,
            "color": info.get("color", (200, 200, 200))
        })
    message = json.dumps({"type": "player_list", "players": players})
    broadcast(message, sender_username=None)


def start():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    logging.info("Server started!")
    logging.info(f"Running on 0.0.0.0:{PORT}")

    external_host = os.environ.get("RAILWAY_STATIC_URL", "your-public-hostname")
    logging.info("======================================")
    logging.info(" Connect your client with:")
    logging.info(f"   {external_host}:{PORT}")
    logging.info("======================================")

    while True:
        conn, addr = server.accept()
        logging.info(f"[DEBUG] Accepted connection from {addr}")
        threading.Thread(target=client_handshake, args=(conn, addr), daemon=True).start()


def client_handshake(conn, addr):
    try:
        logging.info(f"[DEBUG] Starting handshake with {addr}")

        # === Removed HTTP health check for now ===
        # first = conn.recv(16, socket.MSG_PEEK).decode("utf-8", errors="ignore")
        # if first.startswith("GET") or first.startswith("HEAD") or first.startswith("POST"):
        #     logging.warning(f"Ignoring HTTP/health-check from {addr}: {first.strip()}")
        #     try:
        #         conn.send(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nChat server running")
        #     except Exception:
        #         pass
        #     conn.close()
        #     return

        # ============================
        # Username handshake
        # ============================
        conn.send(b"username_request")
        username = conn.recv(1024).decode("utf-8").strip()
        if not username:
            conn.close()
            return

        clients[username] = conn
        logging.info(f"[{username}] ({addr}) connected")

        # ============================
        # Public key
        # ============================
        conn.send(b"public_key_request")
        pem_len_bytes = conn.recv(4)
        if not pem_len_bytes:
            logging.error(f"No public key length from {username}")
            conn.close()
            clients.pop(username, None)
            return
        pem_len = int.from_bytes(pem_len_bytes, "big")

        chunks, bytes_read = [], 0
        while bytes_read < pem_len:
            chunk = conn.recv(min(4096, pem_len - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)

        pub_pem = b"".join(chunks)
        try:
            serialization.load_pem_public_key(pub_pem)
            clients_public_keys[username] = pub_pem
            logging.info(f"Public key for {username} loaded successfully")
        except Exception as e:
            logging.error(f"Invalid PEM from {username}: {e}")
            conn.close()
            clients.pop(username, None)
            return

        # ============================
        # Broadcast join message
        # ============================
        broadcast(json.dumps({
            "username": "Server",
            "color": (255, 0, 0),
            "message": f"{username} has joined the chat."
        }), sender_username=None)

        # ============================
        # Store color
        # ============================
        try:
            color_msg = conn.recv(1024).decode("utf-8")
            color_payload = json.loads(color_msg)
            if "color" in color_payload:
                clients_info[username] = {"color": tuple(color_payload["color"])}
            else:
                clients_info[username] = {"color": (200, 200, 200)}
        except Exception as e:
            logging.warning(f"Could not get color from {username}: {e}")
            clients_info[username] = {"color": (200, 200, 200)}

        # ============================
        # Send updated player list
        # ============================
        broadcast_player_list()

        # ============================
        # Start client handler
        # ============================
        threading.Thread(target=handle_client, args=(conn, addr, username), daemon=True).start()

    except Exception as e:
        logging.error(f"Handshake failed {addr}: {e}")
        try:
            conn.close()
        except:
            pass


def handle_client(conn, addr, username):
    buffer = ""
    while True:
        try:
            data = conn.recv(4096)
            if not data:
                break

            buffer += data.decode("utf-8", errors="ignore")
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

                if payload.get("type") == "image":
                    logging.info(f"[{username}] sent image {payload['filename']}")
                    broadcast(json.dumps(payload), sender_username=username)
                else:
                    logging.info(f"[{username}] says: {payload.get('message')}")
                    broadcast(json.dumps(payload), sender_username=username)

        except Exception as e:
            logging.error(f"Error in handle_client for {username}: {e}")
            break

    conn.close()
    clients.pop(username, None)
    clients_public_keys.pop(username, None)
    clients_info.pop(username, None)

    logging.info(f"[{username}] disconnected")
    broadcast(json.dumps({
        "username": "Server",
        "color": (255, 0, 0),
        "message": f"{username} has left the chat."
    }), sender_username=None)

    broadcast_player_list()


def broadcast(message, sender_username):
    for user, conn in list(clients.items()):
        try:
            conn.sendall((message + "\n").encode("utf-8"))
        except Exception as e:
            logging.error(f"Broadcast to {user} failed: {e}")
            try:
                conn.close()
            except:
                pass
            clients.pop(user, None)
            clients_public_keys.pop(user, None)


if __name__ == "__main__":
    start()

import os, base64, json, logging, threading, socket
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

HOST = "0.0.0.0"
PORT = 50001

clients = {}
clients_info = {}  # store metadata like color for each client
clients_public_keys = {}

logging.basicConfig(
    level=logging.DEBUG,  # DEBUG level to show everything
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "127.0.0.1"
    finally:
        s.close()
    return local_ip

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
   def start():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    # Only start server_commands if running locally
    if os.environ.get("RAILWAY_ENVIRONMENT") is None:  # not on Railway
        threading.Thread(target=server_commands, daemon=True).start()
    else:
        logging.info("Skipping interactive server_commands (running in hosted environment)")

    local_ip = get_local_ip()
    logging.info("Server started!")
    logging.info(f"Running on {local_ip}:{PORT}, HOST: {HOST}")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=client_handshake, args=(conn, addr), daemon=True).start()





def client_handshake(conn, addr):
    try:
        # ============================
        # Username
        # ============================
        conn.send(b"username_request")
        username = conn.recv(1024).decode("utf-8").strip()
        logging.debug(f"Handshake username from {addr}: {username!r}")
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
        logging.debug(f"Expecting public key length {pem_len} from {username}")

        chunks, bytes_read = [], 0
        while bytes_read < pem_len:
            chunk = conn.recv(min(4096, pem_len - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)

        pub_pem = b"".join(chunks)
        logging.debug(f"Received public key bytes ({len(pub_pem)} bytes) from {username}")

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
        # Request color from client (one-time)
        # ============================
        try:
            color_msg = conn.recv(1024).decode("utf-8")
            color_payload = json.loads(color_msg)
            if "color" in color_payload:
                clients_info[username] = {"color": tuple(color_payload["color"])}
                logging.info(f"Stored color for {username}: {clients_info[username]['color']}")
            else:
                clients_info[username] = {"color": (200, 200, 200)}
        except Exception as e:
            logging.warning(f"Could not get color from {username}: {e}")
            clients_info[username] = {"color": (200, 200, 200)}

        # ============================
        # Send updated player list to everyone
        # ============================
        broadcast_player_list()

        # ============================
        # Start client thread
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
                print(f"[INFO] Connection closed by {username}")
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
                    print(f"[ERROR] failed to parse msg from {username}: {e}")
                    continue

                # === Handle message ===
                if payload.get("type") == "image":
                    print(f"[INFO] received image {payload['filename']} from {username}")
                    broadcast(json.dumps(payload, separators=(',', ':')), sender_username=username)
                else:
                    print(f"[INFO] {username}: {payload.get('message')}")
                    broadcast(json.dumps(payload, separators=(',', ':')), sender_username=username)

        except Exception as e:
            print(f"[ERROR] error in handle_client for {username}: {e}")
            break

    # === Cleanup on disconnect ===
    conn.close()
    clients.pop(username, None)
    clients_public_keys.pop(username, None)
    clients_info.pop(username, None)

    print(f"[INFO] {username} disconnected")

    broadcast(json.dumps({
        "username": "Server",
        "color": (255, 0, 0),
        "message": f"{username} has left the chat."
    }), sender_username=None)

    broadcast_player_list()




def broadcast(message, sender_username):
    logging.debug(f"Broadcasting: {message!r}")
    for user, conn in list(clients.items()):
        try:
            conn.sendall((message + "\n").encode("utf-8"))
            logging.debug(f"Sent to {user}")
        except Exception as e:
            logging.error(f"Broadcast to {user} failed: {e}")
            try:
                conn.close()
            except:
                pass
            clients.pop(user, None)
            clients_public_keys.pop(user, None)



def server_commands():
    while True:
        cmd = input("> ").strip()
        if cmd == "/list":
            logging.info("Connected clients:")
            for user in clients.keys():
                logging.info(f" - {user}")
        elif cmd.startswith("/kick "):
            kick_user = cmd.split(" ", 1)[1]
            conn = clients.get(kick_user)
            if conn:
                logging.info(f"Kicking {kick_user}")
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                conn.close()
                clients.pop(kick_user, None)
                clients_public_keys.pop(kick_user, None)
        elif cmd.startswith("/broadcast "):
            broadcast_msg = cmd.split(" ", 1)[1]
            broadcast(json.dumps({"username": "Server", "message": broadcast_msg}), None)


if __name__ == "__main__":
    start()

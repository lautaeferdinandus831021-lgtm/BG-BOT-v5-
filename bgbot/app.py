from flask import Flask, render_template
from flask_socketio import SocketIO
import threading
import time

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

@app.route("/")
def index():
    return render_template("index.html")

def bot_loop():
    while True:
        data = {"price": 12345}  # dummy data
        socketio.emit("market", data)
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=bot_loop).start()
    socketio.run(app, host="0.0.0.0", port=5000)

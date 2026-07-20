#!/usr/bin/env python3
"""Health check for gauss-agentd daemon."""
import socket, json, sys

def check(host="127.0.0.1", port=19876):
    try:
        s = socket.socket()
        s.settimeout(3)
        s.connect((host, port))
        s.send(json.dumps({"type":"request","id":"hc","method":"ping","params":{}}).encode() + b"\n")
        resp = json.loads(s.recv(4096).decode())
        s.close()
        if resp.get("result", {}).get("pong"):
            print("OK: daemon responding")
            return 0
        print("FAIL: unexpected response")
        return 1
    except Exception as e:
        print(f"FAIL: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(check())

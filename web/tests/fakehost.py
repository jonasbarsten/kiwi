"""A stand-in for mod-host's command socket: one client at a time, lockstep replies."""
import socket
import threading


class FakeHost:
    def __init__(self, params=None, close_after=None):
        self.params = dict(params or {})
        self.patch = {}
        self.log = []
        self.chunks = []
        self.cpu = 12.5
        self.close_after = close_after
        self.server = socket.create_server(('127.0.0.1', 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                buffer = b''
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.chunks.append(chunk)
                    buffer += chunk
                    while b'\0' in buffer:
                        raw, buffer = buffer.split(b'\0', 1)
                        if self.close_after is not None and len(self.log) >= self.close_after:
                            return
                        conn.sendall(self.handle(raw.decode()).encode() + b'\0')

    def handle(self, line):
        self.log.append(line)
        parts = line.split()
        if parts[0] == 'param_get':
            key = (int(parts[1]), parts[2])
            return f'resp 0 {self.params[key]:.4f}' if key in self.params else 'resp -103'
        if parts[0] == 'param_set':
            key = (int(parts[1]), parts[2])
            if key not in self.params:
                return 'resp -103'
            self.params[key] = float(parts[3])
            return 'resp 0'
        if parts[0] == 'patch_set':
            self.patch[(int(parts[1]), parts[2])] = float(parts[3])
            return 'resp 0'
        if parts[0] == 'cpu_load':
            return f'resp 0 {self.cpu:.4f}'
        return 'resp -1'

    def close(self):
        self.server.close()

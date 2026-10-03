"""A stand-in for mod-host's command socket: one client at a time, lockstep replies."""
import socket
import threading


class FakeHost:
    def __init__(self, params=None, close_after=None):
        self.params = dict(params or {})
        self.patch = {}
        self.instances = {}
        self.connections = []
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
            # Pre-seeded symbols are the only ones known, unless the instance was
            # added through this socket (then any symbol is accepted, like a real plugin).
            if key not in self.params and key[0] not in self.instances:
                return 'resp -103'
            self.params[key] = float(parts[3])
            return 'resp 0'
        if parts[0] == 'patch_set':
            self.patch[(int(parts[1]), parts[2])] = float(parts[3])
            return 'resp 0'
        if parts[0] == 'cpu_load':
            return f'resp 0 {self.cpu:.4f}'
        if parts[0] == 'add':
            instance = int(parts[2])
            self.instances[instance] = parts[1]
            return f'resp {instance}'
        if parts[0] == 'remove':
            instance = int(parts[1])
            self.instances.pop(instance, None)
            self.params = {k: v for k, v in self.params.items() if k[0] != instance}
            self.connections = [c for c in self.connections if f'effect_{instance}:' not in c[0] + c[1]]
            return 'resp 0'
        if parts[0] == 'connect':
            self.connections.append((parts[1], parts[2]))
            return 'resp 0'
        return 'resp -1'

    def close(self):
        self.server.close()

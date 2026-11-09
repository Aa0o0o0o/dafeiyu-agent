"""Loopback proxy pins every upstream connection to a verified public DNS answer."""

import asyncio
from urllib.parse import urlsplit


class PublicProxy:
    def __init__(self, policy):
        self.policy = policy
        self.server = None
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", 0, limit=65536
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def pipe(self, reader, writer):
        try:
            while chunk := await asyncio.wait_for(reader.read(65536), 45):
                writer.write(chunk)
                await writer.drain()
        except (OSError, TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(header) > 32768:
                raise ValueError()
            lines = header.decode("latin1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            if method == "CONNECT":
                host, sep, port = target.rpartition(":")
                port = int(port)
                if not sep or port != 443:
                    raise ValueError()
            else:
                self.policy.url(target)
                u = urlsplit(target)
                host = u.hostname
                port = u.port or 80
                if u.scheme != "http" or method not in {
                    "GET",
                    "HEAD",
                    "POST",
                    "OPTIONS",
                }:
                    raise ValueError()
            ip = await self.policy.resolve(host, port)
            remote, upstream = await asyncio.wait_for(
                asyncio.open_connection(ip, port), 12
            )
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                path = (u.path or "/") + ("?" + u.query if u.query else "")
                headers = [
                    line
                    for line in lines[1:]
                    if line and not line.lower().startswith(("proxy-", "connection:"))
                ]
                headers.append("Connection: close")
                upstream.write(
                    (
                        method
                        + " "
                        + path
                        + " HTTP/1.1\r\n"
                        + "\r\n".join(headers)
                        + "\r\n\r\n"
                    ).encode("latin1")
                )
                await upstream.drain()
            await asyncio.gather(self.pipe(reader, upstream), self.pipe(remote, writer))
        except (Exception, asyncio.CancelledError):
            if not writer.is_closing():
                try:
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    await writer.drain()
                except OSError:
                    pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

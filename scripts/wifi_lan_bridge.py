#!/usr/bin/env python3
"""
Wi-Fi / LAN TCP Port Bridge for Windows Host.

WSL2 and Docker Desktop bind container ports to localhost / IPv6 loopback (::1).
This lightweight user-mode daemon listens on IPv4 0.0.0.0:8080 on the Windows host
and transparently relays traffic to the local Docker container on [::1]:8080.

This enables:
1. Accessing the web dashboard from the host via http://localhost:8080 or Wi-Fi IP (e.g. http://10.10.10.21:8080)
2. Accessing the central platform from other workstation PCs connected to the same Wi-Fi / LAN
No administrator privileges or complex Hyper-V / netsh configuration needed.
"""

import asyncio
import logging
import socket
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("wifi_lan_bridge")

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
TARGET_HOST = "::1"
TARGET_PORT = 8080
BUFFER_SIZE = 65536


def get_local_ips():
    """Retrieve all active IPv4 addresses of this machine."""
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Bidirectional byte streaming between sockets."""
    try:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        logger.debug(f"Pipe stream error: {e}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    """Handle incoming client connection and bridge to local container."""
    client_addr = client_writer.get_extra_info("peername")
    try:
        target_reader, target_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception as err:
        logger.warning(f"Failed to connect to target [{TARGET_HOST}]:{TARGET_PORT}: {err}")
        try:
            # Fallback to IPv4 127.0.0.1
            target_reader, target_writer = await asyncio.open_connection("127.0.0.1", TARGET_PORT)
        except Exception as fallback_err:
            logger.error(f"Cannot forward connection from {client_addr}: {fallback_err}")
            client_writer.close()
            return

    # Run bidirectional transfer
    t1 = asyncio.create_task(pipe(client_reader, target_writer))
    t2 = asyncio.create_task(pipe(target_reader, client_writer))
    await asyncio.gather(t1, t2, return_exceptions=True)


async def main():
    try:
        server = await asyncio.start_server(
            handle_client,
            LISTEN_HOST,
            LISTEN_PORT,
            reuse_address=True,
        )
    except Exception as e:
        logger.error(f"Failed to bind {LISTEN_HOST}:{LISTEN_PORT}: {e}")
        sys.exit(1)

    ips = get_local_ips()
    logger.info("=================================================================")
    logger.info(f" Wi-Fi / LAN TCP Bridge Active on port {LISTEN_PORT}")
    logger.info(f" Forwarding to: [{TARGET_HOST}]:{TARGET_PORT}")
    logger.info(" Reachable addresses:")
    logger.info(f"   - Local: http://localhost:{LISTEN_PORT}/dashboard")
    for ip in ips:
        logger.info(f"   - Wi-Fi/LAN: http://{ip}:{LISTEN_PORT}/dashboard")
    logger.info("=================================================================")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bridge stopped by user.")

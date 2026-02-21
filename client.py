#!/usr/bin/env python3
"""
Node Monitor Client - Collects and sends node status data via RNS
"""

import RNS
import json
import time
import threading
import configparser
import os
import subprocess
import psutil
import zlib
import struct
import logging
import platform
import glob
import base64
import io
import socket
import socketserver
import uuid
import random
import queue

from datetime import datetime
from http.server import BaseHTTPRequestHandler

# Try to import gpsd for GPS data collection
try:
    import gpsd
    GPSD_AVAILABLE = True
except ImportError:
    GPSD_AVAILABLE = False

class HTTPProxyHandler(BaseHTTPRequestHandler):
    """HTTP proxy handler that forwards requests through RNS"""

    def __init__(self, *args, node_monitor=None, **kwargs):
        self.node_monitor = node_monitor
        # Track pending proxy requests: request_id -> response_data
        if not hasattr(HTTPProxyHandler, 'pending_requests'):
            HTTPProxyHandler.pending_requests = {}
            HTTPProxyHandler.pending_requests_lock = threading.Lock()
        # Track active CONNECT tunnel sessions: session_id -> {data_queue, close_event}
        if not hasattr(HTTPProxyHandler, 'tunnel_sessions'):
            HTTPProxyHandler.tunnel_sessions = {}
            HTTPProxyHandler.tunnel_sessions_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        """Override to use our logger"""
        if self.node_monitor:
            self.node_monitor.logger.info(f"Proxy: {format % args}")

    def do_GET(self):
        """Handle GET requests"""
        self._proxy_request('GET')

    def do_POST(self):
        """Handle POST requests"""
        self._proxy_request('POST')

    def do_HEAD(self):
        """Handle HEAD requests"""
        self._proxy_request('HEAD')

    def do_PUT(self):
        """Handle PUT requests"""
        self._proxy_request('PUT')

    def do_DELETE(self):
        """Handle DELETE requests"""
        self._proxy_request('DELETE')

    def do_CONNECT(self):
        """Handle HTTP CONNECT to create a TCP tunnel through RNS to the server."""
        try:
            host, _, port_str = self.path.rpartition(':')
            port = int(port_str) if port_str else 443
            session_id = str(uuid.uuid4())
            request_id = session_id

            data_queue = queue.Queue()
            close_event = threading.Event()

            with HTTPProxyHandler.tunnel_sessions_lock:
                HTTPProxyHandler.tunnel_sessions[session_id] = {
                    'data_queue': data_queue,
                    'close_event': close_event,
                }

            # Register a pending slot so the ack can be delivered
            connect_event = threading.Event()
            with HTTPProxyHandler.pending_requests_lock:
                HTTPProxyHandler.pending_requests[request_id] = {
                    'event': connect_event,
                    'response': None,
                }

            # Ask the server to open a TCP connection to host:port
            message = {
                'type': 'proxy_connect',
                'request_id': request_id,
                'session_id': session_id,
                'node_id': self.node_monitor.node_id if self.node_monitor else 'unknown',
                'host': host,
                'port': port,
            }
            if self.node_monitor:
                self.node_monitor._send_proxy_request(
                    json.dumps(message, separators=(',', ':')).encode('utf-8')
                )

            # Wait for the server to confirm the TCP connection is open
            if not connect_event.wait(timeout=30):
                self.send_error(504, "CONNECT timeout - no response from RNS server")
                with HTTPProxyHandler.tunnel_sessions_lock:
                    HTTPProxyHandler.tunnel_sessions.pop(session_id, None)
                return

            with HTTPProxyHandler.pending_requests_lock:
                ack = HTTPProxyHandler.pending_requests.pop(request_id, {}).get('response')

            if not ack or not ack.get('success'):
                err = ack.get('error', 'Unknown') if ack else 'No response'
                self.send_error(502, f"CONNECT failed: {err}")
                with HTTPProxyHandler.tunnel_sessions_lock:
                    HTTPProxyHandler.tunnel_sessions.pop(session_id, None)
                return

            # Tell the agent the tunnel is open
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()

            if self.node_monitor:
                self.node_monitor.logger.info(
                    f"CONNECT tunnel open: {host}:{port} (session {session_id})"
                )

            # Relay thread: read from agent socket → chunk over RNS to server → target
            nm = self.node_monitor

            def relay_client_to_server():
                try:
                    self.connection.settimeout(1.0)
                    while not close_event.is_set():
                        try:
                            chunk = self.connection.recv(4096)
                            if not chunk:
                                break
                            msg = {
                                'type': 'proxy_tunnel_data',
                                'session_id': session_id,
                                'node_id': nm.node_id if nm else 'unknown',
                                'data': base64.b64encode(chunk).decode('utf-8'),
                            }
                            if nm:
                                nm._send_proxy_request(
                                    json.dumps(msg, separators=(',', ':')).encode('utf-8')
                                )
                        except socket.timeout:
                            continue
                        except Exception:
                            break
                finally:
                    close_event.set()
                    # Tell the server the client side closed
                    try:
                        close_msg = {
                            'type': 'proxy_tunnel_close',
                            'session_id': session_id,
                            'node_id': nm.node_id if nm else 'unknown',
                        }
                        if nm:
                            nm._send_proxy_request(
                                json.dumps(close_msg, separators=(',', ':')).encode('utf-8')
                            )
                    except Exception:
                        pass

            c2s_thread = threading.Thread(target=relay_client_to_server, daemon=True)
            c2s_thread.start()

            # Main thread: drain server data from the queue and write back to agent
            try:
                while not close_event.is_set():
                    try:
                        chunk = data_queue.get(timeout=1.0)
                        if chunk is None:  # sentinel: server closed the tunnel
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except queue.Empty:
                        continue
            except Exception:
                pass
            finally:
                close_event.set()
                with HTTPProxyHandler.tunnel_sessions_lock:
                    HTTPProxyHandler.tunnel_sessions.pop(session_id, None)
                c2s_thread.join(timeout=5)
                self.close_connection = True

        except Exception as e:
            if self.node_monitor:
                self.node_monitor.logger.error(f"CONNECT handler error: {e}")
            try:
                self.send_error(500, f"CONNECT error: {str(e)}")
            except Exception:
                pass

    def _proxy_request(self, method):
        """Forward request through RNS tunnel"""
        try:
            # Generate unique request ID
            request_id = str(uuid.uuid4())

            # Read request body if present
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else b''

            # Prepare proxy request message
            message = {
                'type': 'proxy_request',
                'request_id': request_id,
                'node_id': self.node_monitor.node_id if self.node_monitor else 'unknown',
                'method': method,
                'url': self.path,
                'headers': dict(self.headers),
                'body': base64.b64encode(body).decode('utf-8') if body else None
            }

            # Create response event for this request
            response_event = threading.Event()
            with HTTPProxyHandler.pending_requests_lock:
                HTTPProxyHandler.pending_requests[request_id] = {
                    'event': response_event,
                    'response': None
                }

            # Queue the request for delivery over the monitoring link
            if self.node_monitor:
                enqueue_time = time.time()
                self.node_monitor.queue_proxy_request(message)
                if self.node_monitor:
                    self.node_monitor.logger.info(f"[PROXY] Waiting for response: "
                                                  f"request_id={request_id} method={method} url={self.path}")
            else:
                with HTTPProxyHandler.pending_requests_lock:
                    HTTPProxyHandler.pending_requests.pop(request_id, None)
                self.send_error(500, "No node monitor available")
                return

            # Wait for the server's HTTP response to arrive over RNS.
            # LoRa round-trip: client→server upload (30-60 s) + server→client
            # upload (30-60 s) = 60-120 s minimum.  Using 60 s caused the
            # browser to receive 504 and retry, creating more threads.
            if response_event.wait(timeout=120):
                elapsed = time.time() - enqueue_time
                if self.node_monitor:
                    self.node_monitor.logger.info(f"[PROXY] Response received in {elapsed:.1f}s "
                                                  f"for request_id={request_id}")
                with HTTPProxyHandler.pending_requests_lock:
                    response_data = HTTPProxyHandler.pending_requests[request_id]['response']
                    del HTTPProxyHandler.pending_requests[request_id]

                if response_data:
                    # Send response back to client
                    self.send_response(response_data['status_code'])
                    for header, value in response_data['headers'].items():
                        # Skip problematic headers
                        if header.lower() not in ['transfer-encoding', 'connection']:
                            self.send_header(header, value)
                    self.end_headers()

                    if response_data['body']:
                        body_bytes = base64.b64decode(response_data['body'])
                        self.wfile.write(body_bytes)
                else:
                    self.send_error(500, "No response from RNS server")
            else:
                # Timeout
                elapsed = time.time() - enqueue_time
                if self.node_monitor:
                    self.node_monitor.logger.error(f"[PROXY] 120s timeout waiting for response "
                                                   f"request_id={request_id} elapsed={elapsed:.1f}s")
                with HTTPProxyHandler.pending_requests_lock:
                    if request_id in HTTPProxyHandler.pending_requests:
                        del HTTPProxyHandler.pending_requests[request_id]
                self.send_error(504, "Gateway timeout - no response from RNS server")

        except Exception as e:
            if self.node_monitor:
                self.node_monitor.logger.error(f"Proxy request error: {e}")
            self.send_error(500, f"Proxy error: {str(e)}")

class ManagementServer:
    """Receives and processes commands and file transfer requests from the server"""

    def __init__(self, config_path="client.conf", node_monitor=None):
        # Configure logging
        logging.basicConfig(level=logging.INFO,
                          format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

        # Load configuration
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        # Reference to NodeMonitor for sending responses
        self.node_monitor = node_monitor

        # Node configuration
        self.node_id = self.config.get('node', 'node_id', fallback='node_001')

        # File transfer state
        self.file_transfers = {}  # transfer_id -> {chunks, file_path, etc}

        self.logger.info(f"Management server initialized for node {self.node_id}")

    def process_message(self, message_data):
        """Process incoming messages from server"""
        try:
            msg_type = message_data.get('type')

            if msg_type == 'command':
                self.handle_command(message_data)
            elif msg_type == 'file_request':
                self.handle_file_request(message_data)
            elif msg_type == 'file_upload':
                self.handle_file_upload(message_data)
            elif msg_type == 'proxy_response':
                self.handle_proxy_response(message_data)
            elif msg_type == 'proxy_connect_ack':
                self.handle_proxy_connect_ack(message_data)
            elif msg_type == 'proxy_tunnel_data':
                self.handle_proxy_tunnel_data(message_data)
            elif msg_type == 'proxy_tunnel_close':
                self.handle_proxy_tunnel_close(message_data)
            else:
                self.logger.warning(f"Unknown message type: {msg_type}")

        except Exception as e:
            self.logger.error(f"Error processing message: {e}")

    def handle_command(self, message_data):
        """Spawn a daemon thread to execute the command.

        Running subprocess.run() directly on the RNS resource-concluded callback
        thread blocks RNS from accepting any further incoming resources for the
        full duration of the command.  By returning immediately and doing the
        work in a background thread we keep the RNS callback thread free.
        """
        t = threading.Thread(
            target=self._execute_command,
            args=(message_data,),
            daemon=True,
            name=f"cmd-{message_data.get('command_id', 'unknown')}",
        )
        t.start()

    def _execute_command(self, message_data):
        """Execute a shell command and queue the result (runs in a daemon thread)."""
        command_id = message_data.get('command_id')
        command = message_data.get('command')
        timeout = message_data.get('timeout', 300)

        self.logger.info(f"Executing command {command_id}: {command} (timeout: {timeout}s)")

        # Check if this is a proxy control command
        if command.startswith('proxy:'):
            self.handle_proxy_command(command_id, command)
            return

        start_time = time.time()
        stdout = ''
        stderr = ''
        exit_code = -1
        error = None

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.returncode

        except subprocess.TimeoutExpired:
            error = f"Command timed out after {timeout} seconds"
            stderr = error
        except Exception as e:
            error = str(e)
            stderr = error

        execution_time = time.time() - start_time

        result_data = {
            'type': 'command_result',
            'node_id': self.node_id,
            'command_id': command_id,
            'stdout': stdout,
            'stderr': stderr,
            'exit_code': exit_code,
            'execution_time': execution_time,
            'error': error
        }

        self.send_response(result_data)

        if exit_code == 0:
            self.logger.info(f"Command {command_id} completed successfully")
        else:
            self.logger.error(f"Command {command_id} failed with exit code {exit_code}")

    def handle_file_request(self, message_data):
        """Handle file download request from server"""
        transfer_id = message_data.get('transfer_id')
        file_path = message_data.get('file_path')
        transfer_type = message_data.get('transfer_type')

        self.logger.info(f"File request {transfer_id}: {transfer_type} {file_path}")

        try:
            if transfer_type == 'download':
                # Read file from client and send to server
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"File not found: {file_path}")

                with open(file_path, 'rb') as f:
                    file_data = f.read()

                # Split into chunks (1MB)
                chunk_size = 1024 * 1024
                chunks = [file_data[i:i+chunk_size] for i in range(0, len(file_data), chunk_size)]

                # Send chunks
                for chunk_num, chunk_data in enumerate(chunks):
                    chunk_message = {
                        'type': 'file_chunk',
                        'node_id': self.node_id,
                        'transfer_id': transfer_id,
                        'chunk_number': chunk_num,
                        'chunk_total': len(chunks),
                        'chunk_data': base64.b64encode(chunk_data).decode('utf-8')
                    }
                    self.send_response(chunk_message)
                    self.logger.info(f"Sent chunk {chunk_num+1}/{len(chunks)} for transfer {transfer_id}")

                # Send completion message
                complete_message = {
                    'type': 'file_complete',
                    'node_id': self.node_id,
                    'transfer_id': transfer_id,
                    'success': True
                }
                self.send_response(complete_message)
                self.logger.info(f"File download {transfer_id} completed")

        except Exception as e:
            error_message = {
                'type': 'file_complete',
                'node_id': self.node_id,
                'transfer_id': transfer_id,
                'success': False,
                'error': str(e)
            }
            self.send_response(error_message)
            self.logger.error(f"File download {transfer_id} failed: {e}")

    def handle_file_upload(self, message_data):
        """Handle file upload chunk from server"""
        transfer_id = message_data.get('transfer_id')
        chunk_number = message_data.get('chunk_number')
        chunk_total = message_data.get('chunk_total')
        chunk_data_b64 = message_data.get('chunk_data')
        file_path = message_data.get('file_path')

        try:
            # Decode chunk data
            chunk_data = base64.b64decode(chunk_data_b64)

            # Initialize transfer state on first chunk
            if transfer_id not in self.file_transfers:
                self.logger.info(
                    f"[FILE] Transfer {transfer_id} started: {file_path} "
                    f"({chunk_total} chunk(s) expected)")
                self.file_transfers[transfer_id] = {
                    'file_path': file_path,
                    'chunks': {},
                    'chunk_total': chunk_total
                }

            # Store chunk
            self.file_transfers[transfer_id]['chunks'][chunk_number] = chunk_data
            received = len(self.file_transfers[transfer_id]['chunks'])
            self.logger.info(
                f"[FILE] Transfer {transfer_id}: received chunk {chunk_number} "
                f"({received}/{chunk_total} chunks, {len(chunk_data)} bytes)")

            # Check if all chunks received
            if received == chunk_total:
                self.logger.info(
                    f"[FILE] Transfer {transfer_id}: all {chunk_total} chunk(s) received, "
                    f"reassembling and writing to {file_path}")

                # Reassemble file in chunk-number order
                file_data = b''
                for i in range(chunk_total):
                    file_data += self.file_transfers[transfer_id]['chunks'][i]

                # Write file
                os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
                with open(file_path, 'wb') as f:
                    f.write(file_data)

                self.logger.info(
                    f"[FILE] Transfer {transfer_id}: wrote {len(file_data)} bytes to {file_path}")

                # Queue completion confirmation (retried on send failure)
                complete_message = {
                    'type': 'file_complete',
                    'node_id': self.node_id,
                    'transfer_id': transfer_id,
                    'success': True
                }
                self.send_response(complete_message)
                self.logger.info(
                    f"[FILE] Transfer {transfer_id}: file_complete queued for delivery")

                # Cleanup in-memory state
                del self.file_transfers[transfer_id]
            else:
                missing = chunk_total - received
                self.logger.info(
                    f"[FILE] Transfer {transfer_id}: waiting for {missing} more chunk(s)")

        except Exception as e:
            self.logger.error(f"[FILE] Transfer {transfer_id}: error handling chunk {chunk_number}: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            error_message = {
                'type': 'file_complete',
                'node_id': self.node_id,
                'transfer_id': transfer_id,
                'success': False,
                'error': str(e)
            }
            self.send_response(error_message)

    def handle_proxy_response(self, message_data):
        """Handle proxy response from server"""
        try:
            request_id = message_data.get('request_id')

            # Find the pending request and set its response
            with HTTPProxyHandler.pending_requests_lock:
                if request_id in HTTPProxyHandler.pending_requests:
                    HTTPProxyHandler.pending_requests[request_id]['response'] = {
                        'status_code': message_data.get('status_code'),
                        'headers': message_data.get('headers', {}),
                        'body': message_data.get('body')
                    }
                    HTTPProxyHandler.pending_requests[request_id]['event'].set()
                    self.logger.info(f"Received proxy response for request {request_id}")
                else:
                    self.logger.warning(f"Received proxy response for unknown request {request_id}")

        except Exception as e:
            self.logger.error(f"Error handling proxy response: {e}")

    def handle_proxy_connect_ack(self, message_data):
        """Deliver a proxy_connect_ack to the waiting do_CONNECT thread."""
        try:
            request_id = message_data.get('request_id')
            with HTTPProxyHandler.pending_requests_lock:
                if request_id in HTTPProxyHandler.pending_requests:
                    HTTPProxyHandler.pending_requests[request_id]['response'] = message_data
                    HTTPProxyHandler.pending_requests[request_id]['event'].set()
                    self.logger.info(f"CONNECT ack for session {message_data.get('session_id')}: "
                                     f"success={message_data.get('success')}")
                else:
                    self.logger.warning(f"proxy_connect_ack for unknown request {request_id}")
        except Exception as e:
            self.logger.error(f"Error handling proxy_connect_ack: {e}")

    def handle_proxy_tunnel_data(self, message_data):
        """Push a data chunk from the server into the correct tunnel session queue."""
        try:
            session_id = message_data.get('session_id')
            chunk_b64 = message_data.get('data', '')
            chunk = base64.b64decode(chunk_b64) if chunk_b64 else b''
            if hasattr(HTTPProxyHandler, 'tunnel_sessions'):
                with HTTPProxyHandler.tunnel_sessions_lock:
                    session = HTTPProxyHandler.tunnel_sessions.get(session_id)
                if session and chunk:
                    session['data_queue'].put(chunk)
                elif not session:
                    self.logger.warning(f"proxy_tunnel_data for unknown session {session_id}")
        except Exception as e:
            self.logger.error(f"Error handling proxy_tunnel_data: {e}")

    def handle_proxy_tunnel_close(self, message_data):
        """Signal the tunnel session that the server-side connection has closed."""
        try:
            session_id = message_data.get('session_id')
            if hasattr(HTTPProxyHandler, 'tunnel_sessions'):
                with HTTPProxyHandler.tunnel_sessions_lock:
                    session = HTTPProxyHandler.tunnel_sessions.get(session_id)
                if session:
                    session['close_event'].set()
                    session['data_queue'].put(None)  # wake the main relay loop
                    self.logger.info(f"CONNECT tunnel closed by server: {session_id}")
        except Exception as e:
            self.logger.error(f"Error handling proxy_tunnel_close: {e}")

    def handle_proxy_command(self, command_id, command):
        """Handle proxy control commands (enable/disable/status)"""
        try:
            parts = command.split(':')
            action = parts[1] if len(parts) > 1 else ''

            if action == 'status':
                # Return current proxy status
                status_data = {
                    'enabled': self.node_monitor.proxy_enabled if self.node_monitor else False,
                    'port': self.node_monitor.proxy_port if self.node_monitor else None
                }
                stdout = json.dumps(status_data)
                stderr = ''
                exit_code = 0
                error = None

            elif action == 'enable':
                # Enable proxy with specified port
                port = int(parts[2]) if len(parts) > 2 else 8080

                if self.node_monitor:
                    if self.node_monitor.proxy_enabled:
                        stdout = 'Proxy already enabled'
                        stderr = ''
                        exit_code = 0
                    else:
                        self.node_monitor.proxy_port = port
                        self.node_monitor.start_proxy_server()
                        if self.node_monitor.proxy_enabled:
                            stdout = f'Proxy enabled on port {port}'
                            stderr = ''
                            exit_code = 0
                        else:
                            stdout = ''
                            stderr = 'Failed to start proxy server'
                            exit_code = 1
                else:
                    stdout = ''
                    stderr = 'No node monitor reference'
                    exit_code = 1

                error = stderr if stderr else None

            elif action == 'disable':
                # Disable proxy
                if self.node_monitor:
                    if not self.node_monitor.proxy_enabled:
                        stdout = 'Proxy already disabled'
                        stderr = ''
                        exit_code = 0
                    else:
                        self.node_monitor.stop_proxy_server()
                        self.node_monitor.proxy_enabled = False
                        stdout = 'Proxy disabled'
                        stderr = ''
                        exit_code = 0
                else:
                    stdout = ''
                    stderr = 'No node monitor reference'
                    exit_code = 1

                error = stderr if stderr else None

            else:
                stdout = ''
                stderr = f'Unknown proxy command: {action}'
                exit_code = 1
                error = stderr

            # Send result back
            result_data = {
                'type': 'command_result',
                'node_id': self.node_id,
                'command_id': command_id,
                'stdout': stdout,
                'stderr': stderr,
                'exit_code': exit_code,
                'execution_time': 0.0,
                'error': error
            }

            self.send_response(result_data)

        except Exception as e:
            # Send error result
            error_result = {
                'type': 'command_result',
                'node_id': self.node_id,
                'command_id': command_id,
                'stdout': '',
                'stderr': str(e),
                'exit_code': 1,
                'execution_time': 0.0,
                'error': str(e)
            }
            self.send_response(error_result)
            self.logger.error(f"Error handling proxy command: {e}")

    def send_response(self, data):
        """Queue response to be sent by the monitoring loop"""
        if self.node_monitor:
            try:
                self.logger.info(f"Queueing response: type={data.get('type')}, command_id={data.get('command_id')}")

                # Add to outgoing queue instead of sending immediately
                with self.node_monitor.queue_lock:
                    self.node_monitor.outgoing_queue.append(data)
                    self.logger.info(f"Response queued (queue size: {len(self.node_monitor.outgoing_queue)})")

                # Wake the monitor loop immediately so the response goes out without
                # waiting for the next health-data cycle (up to 44 seconds away).
                # Without this, command results sit in the queue until the interval
                # timer fires, causing the server's stuck-command retry to collide
                # with the delayed upload and preventing delivery.
                self.node_monitor.proxy_wake_event.set()

            except Exception as e:
                self.logger.error(f"Failed to queue response: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
        else:
            self.logger.error("No node_monitor reference to send response")

        


class NodeMonitor:
    def __init__(self, config_path="client.conf"):
        # Configure logging
        logging.basicConfig(level=logging.INFO, 
                          format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        
        # Load configuration
        self.config = configparser.ConfigParser()
        self.config.read(config_path)
        
        # Initialize RNS
        RNS.Reticulum()
        
        # Create identity or load existing
        identity_path = self.config.get('node', 'identity_path', fallback='./node_identity')
        if os.path.exists(identity_path):
            self.identity = RNS.Identity.from_file(identity_path)
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(identity_path)
        
        # Node configuration
        self.node_id = self.config.get('node', 'node_id', fallback='node_001')
        self.interval = self.config.getint('monitoring', 'interval', fallback=60)
        self.server_destination = self.config.get('server', 'destination')
        self.packet_size_limit = int(self.config.get('rns', 'packet_size_limit', fallback='400'))
        self.link_packet_size = int(self.config.get('rns', 'link_packet_size', fallback='1000'))
        self.link_timeout = int(self.config.get('rns', 'link_timeout', fallback='30'))

        # Data collection settings
        self.collect_gps = self.config.getboolean('data', 'collect_gps', fallback=True)
        self.collect_services = self.config.getboolean('data', 'collect_services', fallback=True)
        self.collect_storage = self.config.getboolean('data', 'collect_storage', fallback=True)
        self.collect_network = self.config.getboolean('data', 'collect_network', fallback=True)
        self.collect_users = self.config.getboolean('data', 'collect_users', fallback=True)
        self.collect_power = self.config.getboolean('data', 'collect_power', fallback=True)
        self.collect_system = self.config.getboolean('data', 'collect_system', fallback=True)
        self.collect_rns = self.config.getboolean('data', 'collect_rns', fallback=True)
        
        # Services to monitor
        self.monitored_services = self.config.get('monitoring', 'services',
                                                fallback='sshd,systemd-resolved,NetworkManager').split(',')

        # Get monitoring destination hash
        self.destination_hash = bytes.fromhex(self.server_destination)

        # Proxy destination hash is no longer used — proxy traffic goes over the monitoring link

        # Initialize GPS connection if enabled
        '''
        if self.collect_gps:
            try:
                gpsd.connect()
                self.logger.info("Connected to gpsd")
            except Exception as e:
                self.logger.warning(f"Failed to connect to gpsd: {e}")
                self.collect_gps = False
        '''

        # Check if we know a path to the monitoring destination
        if not RNS.Transport.has_path(self.destination_hash):
            RNS.log('Monitoring destination is not yet known. Requesting path and waiting for announce to arrive...')
            RNS.log(f'Looking for monitoring destination: {self.destination_hash.hex()}')
            RNS.Transport.request_path(self.destination_hash)

            # Wait up to 60 seconds for path to be discovered
            path_timeout = time.time() + 60
            while not RNS.Transport.has_path(self.destination_hash):
                if time.time() > path_timeout:
                    RNS.log('ERROR: Could not find path to monitoring server after 60 seconds')
                    RNS.log('Please verify:')
                    RNS.log('  1. Server is running and announcing')
                    RNS.log('  2. RNS interfaces are configured correctly')
                    RNS.log('  3. Client and server are on the same RNS network')
                    RNS.log(f'  4. Server destination hash matches: {self.destination_hash.hex()}')
                    raise TimeoutError("Could not establish path to monitoring server")
                time.sleep(0.1)

        # Recall the server identity for monitoring
        server_identity = RNS.Identity.recall(self.destination_hash)

        # Inform the user that we'll begin connecting
        RNS.log('Beginning connection with monitoring server...')

        # Create RNS destination for server monitoring communication
        self.server_dest = RNS.Destination(
            server_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            'rnsmonitor',
            'client',
            'beacon'
        )

        # Proxy traffic uses the monitoring link — no separate proxy link needed
        self.proxy_server_dest = None

        self.running = False
        self.monitor_thread = None

        # Monitoring link (for health data, commands, file transfers, and proxy)
        self.active_link = None
        self.link_transfer_count = 0  # Track transfers on current link
        self.max_transfers_per_link = 10  # Recreate link after this many transfers
        self.send_lock = threading.Lock()  # Prevent simultaneous sends
        self.force_link_recreation = False  # Flag to force link recreation before next send

        # Queue for outgoing messages (command responses, file chunks, proxy requests, etc.)
        self.outgoing_queue = []
        self.queue_lock = threading.Lock()

        # Wake the monitor loop immediately when a proxy request is queued
        self.proxy_wake_event = threading.Event()

        # Initialize ManagementServer for handling commands
        self.management_server = ManagementServer(config_path, node_monitor=self)

        # Start HTTP proxy server
        self.proxy_port = self.config.getint('proxy', 'port', fallback=8080)
        self.proxy_enabled = self.config.getboolean('proxy', 'enabled', fallback=True)
        self.proxy_server = None
        self.proxy_thread = None

        if self.proxy_enabled:
            self.start_proxy_server()

    def start_proxy_server(self):
        """Start the HTTP proxy server"""
        try:
            # Create a custom handler class with access to node_monitor
            node_monitor = self

            class ProxyHandlerWithMonitor(HTTPProxyHandler):
                # Close idle HTTP keep-alive connections after 30 s.
                # Without this, BaseHTTPRequestHandler loops forever on
                # rfile.readline() for the next request, leaving a thread
                # alive per browser connection indefinitely — the primary
                # cause of the thread count explosion on the Pi.
                timeout = 30

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, node_monitor=node_monitor, **kwargs)

            # Create threaded TCP server
            socketserver.TCPServer.allow_reuse_address = True
            self.proxy_server = socketserver.ThreadingTCPServer(
                ('0.0.0.0', self.proxy_port),
                ProxyHandlerWithMonitor
            )
            # Make per-connection handler threads daemon threads so they don't
            # prevent clean process exit and are visible as non-critical in
            # thread dumps.
            self.proxy_server.daemon_threads = True

            # Start proxy server in a separate thread
            self.proxy_thread = threading.Thread(
                target=self.proxy_server.serve_forever,
                daemon=True
            )
            self.proxy_thread.start()

            self.logger.info(f"HTTP proxy server started on 0.0.0.0:{self.proxy_port}")
            self.logger.info(f"Use: curl -x http://<client-ip>:{self.proxy_port} http://example.com")

        except Exception as e:
            self.logger.error(f"Failed to start proxy server: {e}")
            self.proxy_enabled = False

    def stop_proxy_server(self):
        """Stop the HTTP proxy server"""
        if self.proxy_server:
            self.logger.info("Stopping proxy server...")
            self.proxy_server.shutdown()
            self.proxy_server.server_close()
            if self.proxy_thread:
                self.proxy_thread.join(timeout=5)
            self.logger.info("Proxy server stopped")

    def get_gps_data(self):
        """Get GPS location data from gpsd, with fallback to placeholder coordinates"""
        if not self.collect_gps:
            self.logger.debug("GPS collection is disabled in config")
            return None

        # Placeholder coordinates (fallback)
        placeholder_coords = {
            'latitude': 29.192077,
            'longitude': -82.086728,
            'source': 'placeholder'
        }

        # Try to get real GPS data from GPSD
        if GPSD_AVAILABLE:
            self.logger.debug("GPSD library is available, attempting to connect...")
            try:
                # Connect to gpsd
                self.logger.debug("Calling gpsd.connect()...")
                gpsd.connect()
                self.logger.debug("Successfully connected to GPSD daemon")

                # Get current GPS data
                self.logger.debug("Calling gpsd.get_current()...")
                packet = gpsd.get_current()

                # Get values - these might be methods or properties
                def get_value(obj, attr):
                    """Safely get attribute value, calling it if it's a method"""
                    val = getattr(obj, attr, None)
                    if callable(val):
                        return val()
                    return val

                mode = get_value(packet, 'mode')
                lat = get_value(packet, 'lat')
                lon = get_value(packet, 'lon')

                self.logger.debug(f"Received GPS packet - mode: {mode}, lat: {lat}, lon: {lon}")
                self.logger.debug(f"GPS packet type - mode: {type(mode)}, lat: {type(lat)}, lon: {type(lon)}")

                # Check if we have a valid fix (mode >= 2 means 2D fix or better)
                # Mode: 0=no mode, 1=no fix, 2=2D fix, 3=3D fix
                self.logger.debug(f"GPS fix mode: {mode} (0=no mode, 1=no fix, 2=2D fix, 3=3D fix)")

                if mode >= 2:
                    gps_data = {
                        'latitude': float(lat),
                        'longitude': float(lon),
                        'source': 'gpsd',
                        'mode': int(mode)
                    }

                    # Add optional fields if available (ensure JSON serializable)
                    alt = get_value(packet, 'alt')
                    if alt is not None and alt != 'n/a':
                        try:
                            gps_data['altitude'] = float(alt)
                            self.logger.debug(f"Altitude: {alt}")
                        except (ValueError, TypeError):
                            self.logger.debug(f"Altitude not numeric: {alt}")

                    speed = get_value(packet, 'speed')
                    if speed is not None and speed != 'n/a':
                        try:
                            gps_data['speed'] = float(speed)
                            self.logger.debug(f"Speed: {speed}")
                        except (ValueError, TypeError):
                            self.logger.debug(f"Speed not numeric: {speed}")

                    track = get_value(packet, 'track')
                    if track is not None and track != 'n/a':
                        try:
                            gps_data['track'] = float(track)
                            self.logger.debug(f"Track: {track}")
                        except (ValueError, TypeError):
                            self.logger.debug(f"Track not numeric: {track}")

                    time_val = get_value(packet, 'time')
                    if time_val is not None and time_val != 'n/a':
                        try:
                            gps_data['timestamp'] = str(time_val)
                            self.logger.debug(f"GPS timestamp: {time_val}")
                        except (ValueError, TypeError):
                            self.logger.debug(f"Time not convertible: {time_val}")

                    self.logger.info(f"GPS data collected from GPSD: {gps_data['latitude']}, {gps_data['longitude']} (mode={mode})")
                    return gps_data
                else:
                    self.logger.warning(f"GPSD: No valid GPS fix (mode={mode}, need mode>=2), using placeholder")
                    self.logger.debug(f"GPS packet details: lat={lat}, lon={lon}")
                    return placeholder_coords

            except Exception as e:
                self.logger.error(f"GPSD connection/read failed: {type(e).__name__}: {e}")
                import traceback
                self.logger.debug(f"GPSD error traceback:\n{traceback.format_exc()}")
                return placeholder_coords
        else:
            self.logger.warning("GPSD library (gpsd-py3) not available - install with: pip install gpsd-py3")
            self.logger.debug("Using placeholder coordinates")
            return placeholder_coords
    
    def get_service_status(self):
        """Get status of specified Linux services"""
        if not self.collect_services:
            return None
            
        services = {}
        for service in self.monitored_services:
            service = service.strip()
            try:
                result = subprocess.run(
                    ['systemctl', 'is-active', service],
                    capture_output=True,
                    text=True
                )
                services[service] = result.stdout.strip()
            except Exception as e:
                services[service] = f"error: {e}"
        return services
    
    def get_storage_info(self):
        """Get storage space information"""
        if not self.collect_storage:
            return None
            
        storage = {}
        try:
            # Get disk usage for root partition
            usage = psutil.disk_usage('/')
            storage['root'] = {
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': round((usage.used / usage.total) * 100, 2)
            }
            
            # Get additional mount points
            partitions = psutil.disk_partitions()
            for partition in partitions:
                if partition.mountpoint != '/':
                    try:
                        usage = psutil.disk_usage(partition.mountpoint)
                        storage[partition.mountpoint] = {
                            'total': usage.total,
                            'used': usage.used,
                            'free': usage.free,
                            'percent': round((usage.used / usage.total) * 100, 2)
                        }
                    except PermissionError:
                        continue
        except Exception as e:
            self.logger.warning(f"Storage info collection failed: {e}")
        return storage
    
    def get_network_interfaces(self):
        """Get network interface information"""
        if not self.collect_network:
            return None
            
        interfaces = {}
        try:
            net_if_addrs = psutil.net_if_addrs()
            net_if_stats = psutil.net_if_stats()
            
            for interface, addrs in net_if_addrs.items():
                if interface in net_if_stats:
                    stats = net_if_stats[interface]
                    interfaces[interface] = {
                        'is_up': stats.isup,
                        'speed': stats.speed,
                        'mtu': stats.mtu,
                        'addresses': []
                    }
                    
                    for addr in addrs:
                        interfaces[interface]['addresses'].append({
                            'family': str(addr.family),
                            'address': addr.address,
                            'netmask': addr.netmask,
                            'broadcast': addr.broadcast
                        })
        except Exception as e:
            self.logger.warning(f"Network interface collection failed: {e}")
        return interfaces
    
    def get_logged_users(self):
        """Get information about logged in users"""
        if not self.collect_users:
            return None
            
        users = []
        try:
            for user in psutil.users():
                users.append({
                    'name': user.name,
                    'terminal': user.terminal,
                    'host': user.host,
                    'started': user.started,
                    'pid': user.pid
                })
        except Exception as e:
            self.logger.warning(f"User info collection failed: {e}")
        return users
    
    def get_power_data(self):
        """Get battery and power status"""
        if not self.collect_power:
            return None
            
        power_info = {}
        try:
            # Get battery information
            battery = psutil.sensors_battery()
            if battery:
                power_info['battery'] = {
                    'percent': battery.percent,
                    'power_plugged': battery.power_plugged,
                    'seconds_left': battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else None
                }
            
            # Try to get additional power info from /sys/class/power_supply/
            power_supplies = []
            power_supply_path = '/sys/class/power_supply'
            if os.path.exists(power_supply_path):
                for supply in os.listdir(power_supply_path):
                    supply_path = os.path.join(power_supply_path, supply)
                    supply_info = {'name': supply}
                    
                    # Read common power supply attributes
                    attributes = ['type', 'status', 'capacity', 'voltage_now', 'current_now', 'power_now']
                    for attr in attributes:
                        attr_path = os.path.join(supply_path, attr)
                        try:
                            if os.path.exists(attr_path):
                                with open(attr_path, 'r') as f:
                                    supply_info[attr] = f.read().strip()
                        except:
                            continue
                    
                    power_supplies.append(supply_info)
            
            if power_supplies:
                power_info['power_supplies'] = power_supplies
                
        except Exception as e:
            self.logger.warning(f"Power data collection failed: {e}")
            
        return power_info if power_info else None
    
    def get_system_info(self):
        """Get system uptime and OS information"""
        if not self.collect_system:
            return None
            
        system_info = {}
        try:
            # System uptime
            uptime_seconds = time.time() - psutil.boot_time()
            system_info['uptime_seconds'] = int(uptime_seconds)
            system_info['boot_time'] = datetime.fromtimestamp(psutil.boot_time()).isoformat()
            
            # OS and kernel information
            system_info['os'] = {
                'system': platform.system(),
                'release': platform.release(),
                'version': platform.version(),
                'machine': platform.machine(),
                'processor': platform.processor(),
                'platform': platform.platform()
            }
            
            # Try to get distribution info
            try:
                with open('/etc/os-release', 'r') as f:
                    os_release = {}
                    for line in f:
                        if '=' in line:
                            key, value = line.strip().split('=', 1)
                            os_release[key] = value.strip('"')
                    system_info['os']['distribution'] = os_release
            except:
                pass
                
        except Exception as e:
            self.logger.warning(f"System info collection failed: {e}")
            
        return system_info if system_info else None


    def get_rns_status(self):
        """Get RNS interface status and configuration"""
        if not self.collect_rns:
            return None
            
        rns_info = {}
        try:
            # Get RNS interface information
            interfaces = []
            
            # Access RNS Transport to get interface info
            if hasattr(RNS.Transport, 'interfaces'):
                for interface in RNS.Transport.interfaces:
                    interface_info = {
                        'name': getattr(interface, 'name', 'unknown'),
                        'mode': getattr(interface, 'mode', 'unknown'),
                        'bitrate': getattr(interface, 'bitrate', None),
                        'online': getattr(interface, 'online', False),
                    }
                    
                    # Try to get additional interface stats if available
                    if hasattr(interface, 'rxb'):
                        interface_info['rx_bytes'] = interface.rxb
                    if hasattr(interface, 'txb'):
                        interface_info['tx_bytes'] = interface.txb
                    
                    interfaces.append(interface_info)
            
            rns_info['interfaces'] = interfaces
            
            # Get RNS configuration
            rns_config = {}
            
            # Try to read RNS config file
            rns_config_paths = [
                os.path.expanduser('~/.reticulum/config'),
                '/etc/reticulum/config',
                './config'
            ]
            
            for config_path in rns_config_paths:
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r') as f:
                            rns_config['config_file'] = config_path
                            rns_config['config_content'] = f.read()
                        break
                    except Exception as e:
                        self.logger.warning(f"Failed to read RNS config from {config_path}: {e}")
            
            if rns_config:
                rns_info['config'] = rns_config
                
        except Exception as e:
            self.logger.warning(f"RNS status collection failed: {e}")
            
        return rns_info if rns_info else None
    
    def get_client_config(self):
        """Get the contents of the client configuration file"""
        config_paths = [
            '/usr/local/RNSNodeMonitor/client.conf',
            './client.conf',
            'client.conf'
        ]
        
        for config_path in config_paths:
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as f:
                        return {
                            'config_file': config_path,
                            'config_content': f.read()
                        }
                except Exception as e:
                    self.logger.warning(f"Failed to read client config from {config_path}: {e}")
        
        return None
    
    def collect_node_data(self):
        """Collect all node status data"""
        data = {
            'type': 'health_data',  # Add message type
            'node_id': self.node_id,
            # Note: timestamp will be set by server using server's local time
            'gps': self.get_gps_data(),
            'services': self.get_service_status(),
            'storage': self.get_storage_info(),
            'network': self.get_network_interfaces(),
            'users': self.get_logged_users(),
            'power': self.get_power_data(),
            'system': self.get_system_info(),
            'rns': self.get_rns_status(),
            'client_config': self.get_client_config()
        }
        return data
    
    def chunk_data(self, data_bytes, chunk_size):
        """Split data into chunks for transmission"""
        chunks = []
        for i in range(0, len(data_bytes), chunk_size):
            chunks.append(data_bytes[i:i + chunk_size])
        return chunks
    
    def send_data(self, data, interruptible=False):
        """Send data to the server. Returns True on success, False on failure.

        interruptible=True makes the underlying resource wait abortable when
        proxy_wake_event fires (used for health data only).
        """
        try:
            json_data = json.dumps(data, separators=(',', ':'))
            data_bytes = json_data.encode('utf-8')

            self.logger.info("Using link for data transmission")
            success = self._send_via_link_resource(data_bytes, interruptible=interruptible)

            if success:
                self.logger.info(f"Data sent for node {self.node_id} ({len(data_bytes)} bytes)")
            else:
                self.logger.warning(f"Data send FAILED for node {self.node_id} "
                                    f"({len(data_bytes)} bytes, type={data.get('type', 'unknown')})")
            return success

        except Exception as e:
            self.logger.error(f"Failed to send data: {e}")
            return False
    
    def resource_concluded_callback(self, resource):

        self.logger.info("Resource transfer complete")


    def _safe_teardown_link(self, link, label="link"):
        """Tear down a link and wait for its internal RNS threads to drain."""
        if link is None:
            return
        try:
            link.teardown()
        except:
            pass
        # Give RNS resource/watchdog threads time to exit before we create
        # new links. Without this, threads pile up on resource-constrained
        # devices (e.g. Raspberry Pi) and eventually hit the OS thread limit.
        time.sleep(2)
        thread_count = threading.active_count()
        if thread_count > 50:
            self.logger.warning(f"High thread count after {label} teardown: {thread_count}")

    def _is_link_healthy(self):
        """Check if the current link is healthy and usable"""
        if self.active_link is None:
            return False

        # Check if link recreation was forced (e.g., after command response)
        if self.force_link_recreation:
            self.logger.info("Link recreation forced, tearing down link")
            self._safe_teardown_link(self.active_link, "monitoring")
            self.active_link = None
            self.link_transfer_count = 0
            self.force_link_recreation = False
            return False

        # Force link recreation after max transfers to prevent encryption issues
        if self.link_transfer_count >= self.max_transfers_per_link:
            self.logger.info(f"Link reached max transfers ({self.link_transfer_count}), recreating")
            self._safe_teardown_link(self.active_link, "monitoring")
            self.active_link = None
            self.link_transfer_count = 0
            return False

        # Check link status - only ACTIVE links are usable
        status = self.active_link.status
        if status == RNS.Link.ACTIVE:
            return True

        # Link is in unusable state (CLOSED, STALE, PENDING, etc.)
        self.logger.warning(f"Link is unhealthy - status: {status}")

        # Clean up the old link
        self._safe_teardown_link(self.active_link, "monitoring")
        self.active_link = None
        self.link_transfer_count = 0
        return False

    def _send_via_link_resource(self, data_bytes, interruptible=False):
        # Use lock to prevent simultaneous sends that cause timing errors
        # interruptible=True: abort the resource wait if proxy_wake_event fires
        # (used for health data so a proxy request can preempt it)
        resource = None
        with self.send_lock:
            try:
                # Check if we need to establish a new link
                if not self._is_link_healthy():
                    self.logger.info("Establishing new RNS Link...")
                    self.active_link = RNS.Link(self.server_dest)

                    link_timeout = time.time() + self.link_timeout
                    while (self.active_link.status == RNS.Link.PENDING and
                           time.time() < link_timeout):
                        time.sleep(0.1)

                    if self.active_link.status != RNS.Link.ACTIVE:
                        self.logger.error("Failed to establish RNS Link")
                        self.active_link = None
                        return

                    self.logger.info("RNS Link established")

                    if self.active_link.status == RNS.Link.ACTIVE:
                        self.active_link.identify(self.identity)

                        # Set up callbacks for bidirectional communication
                        self.logger.info("Setting up link callbacks on new link")
                        self.active_link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
                        self.active_link.set_resource_callback(self.server_resource_callback)
                        self.active_link.set_resource_concluded_callback(self.server_resource_concluded_callback)
                        self.active_link.set_link_closed_callback(self.link_closed_callback)
                        self.logger.info("Link callbacks registered on new link")

                # Verify link is active before sending
                if not self.active_link or self.active_link.status != RNS.Link.ACTIVE:
                    self.logger.error(f"Cannot send - link status: {self.active_link.status if self.active_link else 'None'}")
                    return

                # Delay to avoid resource conflicts - increased for LoRa timing
                # Add random jitter to prevent synchronized collisions
                delay = 1.5 + random.uniform(0, 0.5)
                time.sleep(delay)

                self.logger.debug(f"Creating resource transfer ({len(data_bytes)} bytes)")
                resource = RNS.Resource(data_bytes, self.active_link, callback=self.resource_concluded_callback)

                resource.advertise()
                self.link_transfer_count += 1
                self.logger.debug(f"Resource advertised (transfer {self.link_transfer_count}/{self.max_transfers_per_link})")

            except Exception as e:
                self.logger.error(f"Failed to send via link resource: {e}")
                # Reset link on error, with thread drain
                self._safe_teardown_link(self.active_link, "monitoring")
                self.active_link = None

        # Wait for resource to complete OUTSIDE the lock to avoid blocking other sends
        if resource:
            resource_timeout = time.time() + 60  # 60 second timeout
            while resource.status < RNS.Resource.COMPLETE and time.time() < resource_timeout:
                # If this is an interruptible send (e.g. health data) and a proxy
                # request has just been queued, abort so the proxy can go out immediately.
                if interruptible and self.proxy_wake_event.is_set():
                    self.logger.info("Interruptible send aborted — proxy request arrived, tearing down link")
                    with self.send_lock:
                        self._safe_teardown_link(self.active_link, "interrupted-health")
                        self.active_link = None
                        self.link_transfer_count = 0
                    return False
                time.sleep(0.1)

            if resource.status != RNS.Resource.COMPLETE:
                self.logger.warning(f"Resource transfer did not complete (status: {resource.status})")
                # Failed transfer — tear down link so stale RNS threads can exit
                with self.send_lock:
                    self._safe_teardown_link(self.active_link, "monitoring-failed-xfer")
                    self.active_link = None
                    self.link_transfer_count = 0
                return False
            else:
                self.logger.debug("Resource transfer completed successfully")
                return True
        return False

    def queue_proxy_request(self, message_data):
        """Queue a proxy message for delivery over the monitoring link.
        Wakes the monitor loop immediately so the request goes out without
        waiting for the next regular health-data cycle."""
        with self.queue_lock:
            self.outgoing_queue.append(message_data)
            queue_size = len(self.outgoing_queue)
        self.logger.info(f"[PROXY] Queued message: type={message_data.get('type')} "
                         f"request_id={message_data.get('request_id', 'N/A')} "
                         f"session_id={message_data.get('session_id', 'N/A')} "
                         f"queue_size={queue_size}")
        self.proxy_wake_event.set()

    def _send_proxy_request(self, data_bytes):
        """Compatibility shim used by the CONNECT tunnel relay threads.
        Decodes the JSON payload and queues it for delivery via the monitoring link."""
        try:
            message_data = json.loads(data_bytes.decode('utf-8'))
            self.queue_proxy_request(message_data)
            return True
        except Exception as e:
            self.logger.error(f"Failed to queue proxy message: {e}")
            return False

    def proxy_resource_callback(self, resource):
        """Handle incoming proxy resources from server"""
        self.logger.debug(f"Incoming proxy resource from server - size: {resource.size if hasattr(resource, 'size') else 'unknown'}")
        return True

    def proxy_resource_concluded_callback(self, resource):
        """Handle concluded proxy resource transfers from server"""
        self.logger.debug(f"Proxy resource from server concluded - status: {resource.status}")

        if resource.status == RNS.Resource.COMPLETE:
            try:
                # Decode resource data
                data = resource.data.read()
                self.logger.debug(f"Proxy resource data size: {len(data)} bytes")

                json_data = data.decode("utf-8")
                message_data = json.loads(json_data)

                self.logger.info(f"Received proxy response from server: type={message_data.get('type')}")

                # Process message through ManagementServer
                self.management_server.process_message(message_data)

            except Exception as e:
                self.logger.error(f"Error processing proxy server message: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
        else:
            self.logger.warning(f"Proxy server message transfer failed with status: {resource.status}")

    def proxy_link_closed_callback(self, link):
        """Handle proxy link closure (legacy - proxy traffic now uses the monitoring link)"""
        self.logger.warning("Proxy link to server has been closed")

    def link_closed_callback(self, link):
        """Handle link closure by server or due to timeout"""
        self.logger.warning("Link to server has been closed")
        if self.active_link == link:
            self.active_link = None
            self.logger.info("Active link cleared, will re-establish on next send")

    def server_resource_callback(self, resource):
        """Handle incoming resources from server"""
        self.logger.debug(f"Incoming resource from server - size: {resource.size if hasattr(resource, 'size') else 'unknown'}, total: {resource.total_size if hasattr(resource, 'total_size') else 'unknown'}")
        return True

    def server_resource_concluded_callback(self, resource):
        """Handle concluded resource transfers from server"""
        self.logger.debug(f"Resource from server concluded - status: {resource.status}")

        if resource.status == RNS.Resource.COMPLETE:
            try:
                # Decode resource data
                data = resource.data.read()
                self.logger.debug(f"Resource data size: {len(data)} bytes")

                json_data = data.decode("utf-8")
                message_data = json.loads(json_data)

                self.logger.info(f"Received message from server: type={message_data.get('type')}, command_id={message_data.get('command_id', 'N/A')}")

                # Process message through ManagementServer
                self.management_server.process_message(message_data)

            except Exception as e:
                self.logger.error(f"Error processing server message: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
        else:
            self.logger.warning(f"Server message transfer failed with status: {resource.status}")


    def monitor_loop(self):
        """Main monitoring loop.

        Sleeps for up to `interval` seconds but wakes immediately when
        proxy_wake_event is set (i.e. a proxy request has been queued).
        Health data is only sent when the full interval has elapsed so
        that proxy requests don't have to queue behind an 8 KB health
        data transfer every time.
        """
        last_health_time = 0.0
        while self.running:
            try:
                # Proactively check and cleanup unhealthy links
                self._is_link_healthy()

                # Sleep up to `interval` seconds; wake early if proxy traffic arrives
                time_since_health = time.time() - last_health_time
                remaining = max(0.0, self.interval - time_since_health)
                self.proxy_wake_event.wait(timeout=remaining)
                self.proxy_wake_event.clear()

                # Process outgoing queue (proxy requests, command responses, file chunks)
                messages_to_send = []
                with self.queue_lock:
                    if self.outgoing_queue:
                        messages_to_send = self.outgoing_queue.copy()
                        self.outgoing_queue.clear()

                # Message types that must eventually reach the server — retry on failure.
                # Proxy messages are time-sensitive (browser is waiting with a timeout)
                # and must NOT be re-queued: sending them twice confuses the server.
                RETRIABLE_TYPES = {'command_result', 'file_chunk', 'file_complete'}

                failed_messages = []
                for queued_data in messages_to_send:
                    qtype = queued_data.get('type', 'unknown')
                    self.logger.info(f"[PROXY] Dequeuing message: type={qtype} "
                                     f"request_id={queued_data.get('request_id', 'N/A')} "
                                     f"command_id={queued_data.get('command_id', 'N/A')}")
                    success = self.send_data(queued_data)

                    if qtype in ('proxy_request', 'proxy_connect',
                                 'proxy_tunnel_data', 'proxy_tunnel_close'):
                        # After a proxy send (success or failure):
                        # 1. Reset link_transfer_count so the link is not torn down
                        #    before the server can send the response back.
                        # 2. Defer health data so the 8 KB health upload does not
                        #    occupy the channel while the server is replying.
                        with self.send_lock:
                            self.link_transfer_count = 0
                        last_health_time = time.time()
                        self.logger.info(f"[PROXY] Reset link_transfer_count and deferred "
                                         f"health data after {qtype}")

                        if not success and qtype == 'proxy_request':
                            # Allow a single retry for proxy_request (resource failed
                            # before the server could receive it, e.g. status 7 link
                            # closed). Retrying once gives the proxy a second chance
                            # before the browser's timeout fires.
                            retry_count = queued_data.get('_retry_count', 0)
                            if retry_count < 1:
                                retry_msg = dict(queued_data)
                                retry_msg['_retry_count'] = retry_count + 1
                                failed_messages.append(retry_msg)
                                self.logger.warning(
                                    f"[PROXY] proxy_request failed (status 7?) — "
                                    f"will retry once (attempt {retry_count + 1})")
                            else:
                                self.logger.warning(
                                    f"[PROXY] proxy_request failed after 1 retry — "
                                    f"giving up (browser will receive 504)")
                    elif not success and qtype in RETRIABLE_TYPES:
                        # The send failed (channel busy, link dropped, etc.).
                        # Re-queue so the next monitor loop pass retries rather than
                        # waiting for the server's stuck-command timeout (minutes).
                        failed_messages.append(queued_data)
                        self.logger.warning(f"Send failed for {qtype} "
                                            f"command_id={queued_data.get('command_id', 'N/A')} "
                                            f"— will retry next loop pass")

                if failed_messages:
                    with self.queue_lock:
                        # Prepend so retries go out before any newly queued messages
                        self.outgoing_queue = failed_messages + self.outgoing_queue
                    self.proxy_wake_event.set()  # wake immediately to retry

                # Send health data only when the interval has elapsed
                now = time.time()
                if now - last_health_time >= self.interval:
                    # Skip health data if proxy messages are already queued so the
                    # proxy can go out immediately on the next loop pass.
                    PROXY_TYPES = {'proxy_request', 'proxy_connect',
                                   'proxy_tunnel_data', 'proxy_tunnel_close'}
                    with self.queue_lock:
                        has_pending_proxy = any(
                            m.get('type') in PROXY_TYPES for m in self.outgoing_queue
                        )
                    if has_pending_proxy:
                        self.logger.info("[PROXY] Deferring health data — proxy messages are pending in queue")
                    else:
                        data = self.collect_node_data()
                        # interruptible=True: if a proxy request arrives mid-send,
                        # abort the health transfer and let the proxy go first.
                        self.send_data(data, interruptible=True)
                    # Always advance last_health_time so we wait a full interval
                    # before the next health attempt (whether we sent or skipped).
                    last_health_time = now

                # Log thread count periodically for diagnostics
                thread_count = threading.active_count()
                self.logger.debug(f"Active threads: {thread_count}")
                if thread_count > 100:
                    self.logger.warning(f"Thread count is high: {thread_count}")

            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                if self.active_link:
                    self.logger.warning("Resetting link due to error")
                    self._safe_teardown_link(self.active_link, "monitoring-loop-error")
                    self.active_link = None
                time.sleep(5)
    
    def start(self):
        """Start the monitoring service"""
        if self.running:
            return
            
        self.running = True
        self.monitor_thread = threading.Thread(target=self.monitor_loop)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
        self.logger.info(f"Node monitor started for {self.node_id}")
    
    def stop(self):
        """Stop the monitoring service"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join()
        
        # Close active link if exists
        if self.active_link and self.active_link.status == RNS.Link.ACTIVE:
            self.active_link.teardown()
            self.active_link = None
            
        self.logger.info("Node monitor stopped")
    
    def update_interval(self, new_interval):
        """Update monitoring interval"""
        self.interval = new_interval
        self.config.set('monitoring', 'interval', str(new_interval))
        with open('node_monitor.conf', 'w') as f:
            self.config.write(f)
        self.logger.info(f"Monitoring interval updated to {new_interval} seconds")

if __name__ == "__main__":
    import signal
    import sys
   
    monitor = NodeMonitor()
    
    def signal_handler(sig, frame):
        print('\nShutting down...')
        monitor.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    monitor.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()

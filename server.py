#!/usr/bin/env python3
"""
Monitoring Server - Receives node status data and stores in SQLite database
"""

import RNS
import json
import sqlite3
import threading
import logging
import os
import time
import io
import base64
from datetime import datetime
import configparser
import urllib.request
import urllib.error
from urllib.parse import urlparse
import random
import socket

'''
class ManagementClient:
    def __init__(self):
        # Configure logging
        logging.basicConfig(level=logging.INFO, 
                          format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        
        # Initialize RNS
        RNS.Reticulum()
        
        # Create identity for server
        identity_path = './server_identity'
        if os.path.exists(identity_path):
            self.server_identity = RNS.Identity.from_file(identity_path)
        else:
            self.server_identity = RNS.Identity()
            self.server_identity.to_file(identity_path)
        
        # Create destination for client connections
        self.destination = RNS.Destination(
            self.server_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "remote_admin",
            "server"
        )
        
        self.destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        self.destination.set_link_established_callback(self.client_connected)
        
        # Store active links and jobs
        self.client_links = {}
        self.pending_jobs = {}
        self.db_lock = threading.Lock()
        
        self.logger.info(f"Server destination: {RNS.prettyhexrep(self.destination.hash)}")
        
    
    def send_command(self, command):
        data = {'command': command}

        try:
            json_data = json.dumps(data, separators=(',', ':'))
            data_bytes = json_data.encode('utf-8')
            
            self.logger.info("Using link for data transmission")
            self._send_via_link_resource(data_bytes)
                
            self.logger.info(f"Data sent for node {self.node_id} ({len(data_bytes)} bytes)")
                
        except Exception as e:
            self.logger.error(f"Failed to send data: {e}")


    def client_connected(self, link):
        """Handle new client connection"""
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_started_callback(self.resource_started)
        link.set_resource_callback(self.resource_callback)
        link.set_resource_concluded_callback(self.resource_concluded)
        link.set_remote_identified_callback(self.link_identified)
        
        # Store link
        link_id = str(uuid.uuid4())
        self.client_links[link_id] = {
            'link': link,
            'node_id': None,
            'connected_at': datetime.now()
        }
        
        self.logger.info(f"New client connected: {link_id}")
        return True
    
    def link_identified(self, link, identity):
        """Handle client identity"""
        node_id = RNS.prettyhexrep(identity.hash)
        
        # Update link with node_id
        for link_id, link_info in self.client_links.items():
            if link_info['link'] == link:
                link_info['node_id'] = node_id
                break
        
        self.logger.info(f"Client identified: {node_id}")
    
    def resource_started(self, resource):
        """Handle resource transfer start"""
        self.logger.info("Resource transfer started")
    
    def resource_callback(self, resource):
        """Handle incoming resource"""
        return True
    
    def resource_concluded(self, resource):
        """Handle completed resource transfer"""
        if resource.status == RNS.Resource.COMPLETE:
            try:
                data = resource.data.read()
                message = json.loads(data.decode('utf-8'))

                msg_type = message.get('type')
                self.logger.info(f"Received message from client: type={msg_type}")

                if msg_type == 'health_check':
                    self.store_health_data(message)
                elif msg_type == 'command_result':
                    # Extract parameters from message
                    command_id = message.get('command_id')
                    stdout = message.get('stdout', '')
                    stderr = message.get('stderr', '')
                    exit_code = message.get('exit_code', -1)
                    execution_time = message.get('execution_time', 0.0)
                    error = message.get('error')

                    self.logger.info(f"Processing command result for command {command_id}")
                    self.store_command_result(command_id, stdout, stderr, exit_code, execution_time, error)
                elif msg_type == 'file_result':
                    self.store_file_result(message)

            except Exception as e:
                self.logger.error(f"Error processing resource: {e}")
                import traceback
                self.logger.error(traceback.format_exc())


    def _send_via_link_resource(self, data_bytes):

        try:
            # Establish link if needed
            if self.active_link is None or self.active_link.status != RNS.Link.ACTIVE:
                self.logger.debug("Establishing RNS Link...")
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

            resource = RNS.Resource(data_bytes, self.active_link, callback=self.resource_concluded_callback)

            resource.advertise()

        except Exception as e:
            self.logger.error(f"Failed to send via link resource: {e}")
            # Reset link on error
            if self.active_link:
                try:
                    self.active_link.teardown()
                except:
                    pass
                self.active_link = None
'''


class MonitoringServer:
    def __init__(self, config_path="server.conf"):
        # Configure logging
        logging.basicConfig(level=logging.INFO, 
                          format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        
        # Load configuration
        self.config = configparser.ConfigParser()
        self.config.read(config_path)
        
        # Initialize RNS
        RNS.Reticulum()
        
        # Create or load identity
        identity_path = self.config.get('server', 'identity_path', fallback='./server_identity')
        if os.path.exists(identity_path):
            self.identity = RNS.Identity.from_file(identity_path)
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(identity_path)
        
        # Database configuration
        self.db_path = self.config.get('database', 'path', fallback='node_monitoring.db')
        
        # Create RNS destination for monitoring traffic
        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "rnsmonitor",
            "client",
            "beacon"
        )

        self.destination.set_proof_strategy(RNS.Destination.PROVE_ALL)

        # Set up link request callback for larger data transfers
        self.destination.set_link_established_callback(self.link_established)

        # Create separate RNS destination for proxy traffic (low-latency)
        self.proxy_destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "rnsmonitor",
            "proxy",
            "tunnel"
        )

        self.proxy_destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        self.proxy_destination.set_link_established_callback(self.proxy_link_established)
        
        # Initialize database
        self.init_database()
        
        # Database lock for thread safety
        self.db_lock = threading.Lock()

        # Send lock to prevent concurrent resource sends
        self.send_locks = {}  # node_id -> Lock mapping for per-node send serialization

        # Track active links with node_id mapping
        self.active_links = {}  # link_hash -> link mapping
        self.node_links = {}  # node_id -> link mapping
        self.link_to_node = {}  # link -> node_id reverse mapping
        self.identity_to_node = {}  # identity_hash (hex str) -> node_id; persists across reconnects
        self.link_to_identity = {}  # link -> identity object; populated by link_identified_callback

        # Track proxy links separately
        self.proxy_links = {}  # link_hash -> link mapping
        self.node_proxy_links = {}  # node_id -> proxy link mapping
        self.proxy_link_to_node = {}  # proxy link -> node_id reverse mapping

        # Track active CONNECT tunnel sessions: session_id -> {socket, node_id, close_event, send_lock}
        self.tunnel_sessions = {}
        self.tunnel_sessions_lock = threading.Lock()

        # Configure announce interval (in seconds)
        self.announce_interval = self.config.getint('server', 'announce_interval', fallback=120)

        # Start background thread for polling pending commands/transfers
        self.running = True
        self.poller_thread = threading.Thread(target=self.poll_pending_jobs, daemon=True)
        self.poller_thread.start()

        # Start announce thread
        self.announce_thread = threading.Thread(target=self.announce_loop, daemon=True)
        self.announce_thread.start()

        # Do initial announce for both destinations
        self.destination.announce()
        self.proxy_destination.announce()
        self.logger.info(f"Initial announces sent")

        self.logger.info(f"Monitoring server started")
        self.logger.info(f"Monitoring destination: {self.destination.hash.hex()}")
        self.logger.info(f"Proxy destination: {self.proxy_destination.hash.hex()}")
        self.logger.info(f"Announce interval: {self.announce_interval} seconds")
    
    def init_database(self):
        """Initialize SQLite database with required tables"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)

        # Enable WAL mode for better concurrency
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=60000')  # 60 second busy timeout
        conn.execute('PRAGMA synchronous=NORMAL')  # Faster writes with WAL

        cursor = conn.cursor()

        # Create nodes table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT UNIQUE,
                last_seen TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create updated node_status table with new fields
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS node_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT,
                timestamp TIMESTAMP,
                gps_data TEXT,
                services_data TEXT,
                storage_data TEXT,
                network_data TEXT,
                users_data TEXT,
                power_data TEXT,
                system_data TEXT,
                rns_data TEXT,
                client_config_data TEXT,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (node_id) REFERENCES nodes (node_id)
            )
        ''')

        # Create commands table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT,
                command TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                timeout INTEGER DEFAULT 300,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_by TEXT,
                FOREIGN KEY (node_id) REFERENCES nodes (node_id)
            )
        ''')

        # Create command_results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS command_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id INTEGER,
                stdout TEXT,
                stderr TEXT,
                exit_code INTEGER,
                execution_time REAL,
                error TEXT,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (command_id) REFERENCES commands (id)
            )
        ''')

        # Create file_transfers table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT,
                transfer_type TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER,
                status TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                chunks_total INTEGER,
                chunks_received INTEGER DEFAULT 0,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_by TEXT,
                FOREIGN KEY (node_id) REFERENCES nodes (node_id)
            )
        ''')

        # Create file_chunks table for storing file data
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_id INTEGER,
                chunk_number INTEGER,
                chunk_data BLOB,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (transfer_id) REFERENCES file_transfers (id)
            )
        ''')

        # Create logs table for general system logs and errors
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT,
                log_type TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (node_id) REFERENCES nodes (node_id)
            )
        ''')

        # Check if new columns exist and add them if they don't (for database migration)
        cursor.execute("PRAGMA table_info(node_status)")
        columns = [column[1] for column in cursor.fetchall()]

        new_columns = {
            'power_data': 'TEXT',
            'system_data': 'TEXT',
            'rns_data': 'TEXT',
            'client_config_data': 'TEXT'
        }

        for column_name, column_type in new_columns.items():
            if column_name not in columns:
                cursor.execute(f'ALTER TABLE node_status ADD COLUMN {column_name} {column_type}')
                self.logger.info(f"Added column {column_name} to node_status table")

        # Check if timeout column exists in commands table and add if it doesn't
        cursor.execute("PRAGMA table_info(commands)")
        commands_columns = [column[1] for column in cursor.fetchall()]
        if 'timeout' not in commands_columns:
            cursor.execute('ALTER TABLE commands ADD COLUMN timeout INTEGER DEFAULT 300')
            self.logger.info("Added timeout column to commands table")

        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_node_status_node_id ON node_status(node_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_node_status_timestamp ON node_status(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_commands_node_id ON commands(node_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_transfers_node_id ON file_transfers(node_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_transfers_status ON file_transfers(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_node_id ON logs(node_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs(created_at)')

        conn.commit()
        conn.close()

        self.logger.info("Database initialized")
    
    def link_identified_callback(self, link, identity):
        identity_hex = identity.hash.hex()
        self.logger.info(f"Received identity for monitoring link: {identity_hex}")

        # Store link→identity so link_resource_concluded_callback can populate
        # identity_to_node without accessing the (non-existent) remote_identity attr.
        self.link_to_identity[link] = identity

        # If we've seen this identity before, we know which node it is.
        # Update node_links immediately so the polling thread doesn't capture
        # the old dead link before the first message arrives on this new link.
        node_id = self.identity_to_node.get(identity_hex)
        if node_id:
            self.node_links[node_id] = link
            self.link_to_node[link] = node_id
            self.logger.info(f"Restored node_links for {node_id} from identity on reconnect")

    def proxy_link_identified_callback(self, link, identity):

        self.logger.info(f"Received identity for proxy link: {identity.hash.hex()}")

    def proxy_resource_callback(self, resource):
        """Handle incoming proxy resources over RNS Link"""
        self.logger.debug(f"Incoming proxy resource - hash: {resource.get_hash().hex()}, size: {resource.size if hasattr(resource, 'size') else 'unknown'}")
        return True

    def proxy_resource_concluded_callback(self, resource):
        """Handle concluded proxy resource transfers"""
        self.logger.debug(f"Proxy resource concluded - status: {resource.status}")

        if resource.status == RNS.Resource.COMPLETE:
            try:
                # Decode resource data
                data = resource.data.read()
                self.logger.debug(f"Proxy resource data size: {len(data)} bytes")

                json_data = data.decode("utf-8")
                message_data = json.loads(json_data)

                msg_type = message_data.get('type')
                self.logger.info(f"Received proxy message type: {msg_type}")

                # Update node_id → proxy link mapping for any client message
                node_id = message_data.get('node_id')
                if node_id and hasattr(resource, 'link') and resource.link:
                    if resource.link.status == RNS.Link.ACTIVE:
                        self.node_proxy_links[node_id] = resource.link
                        self.proxy_link_to_node[resource.link] = node_id

                if msg_type == 'proxy_request':
                    self.handle_proxy_request(message_data)
                elif msg_type == 'proxy_connect':
                    self.handle_proxy_connect(message_data)
                elif msg_type == 'proxy_tunnel_data':
                    self.handle_proxy_tunnel_data(message_data)
                elif msg_type == 'proxy_tunnel_close':
                    self.handle_proxy_tunnel_close(message_data)
                else:
                    self.logger.warning(f"Unknown proxy message type: {msg_type}")

            except Exception as e:
                self.logger.error(f"Error processing proxy resource data: {e}")
        else:
            self.logger.warning(f"Proxy resource transfer failed with status: {resource.status}")

    def proxy_link_closed_callback(self, link):
        """Handle proxy link closure - clean up mappings"""
        self.logger.info("Proxy link closed, cleaning up mappings")

        # Find node_id associated with this proxy link
        node_id = self.proxy_link_to_node.get(link)

        if node_id:
            # Remove from both mappings
            if node_id in self.node_proxy_links:
                del self.node_proxy_links[node_id]
                self.logger.info(f"Removed proxy link mapping for node {node_id}")

            if link in self.proxy_link_to_node:
                del self.proxy_link_to_node[link]

        # Remove from proxy_links
        links_to_remove = [link_hash for link_hash, l in self.proxy_links.items() if l == link]
        for link_hash in links_to_remove:
            del self.proxy_links[link_hash]
            self.logger.debug(f"Removed link from proxy_links: {link_hash}")

        # Close any open CONNECT tunnel sessions belonging to this node
        if node_id:
            with self.tunnel_sessions_lock:
                stale = [sid for sid, s in self.tunnel_sessions.items() if s.get('node_id') == node_id]
            for sid in stale:
                with self.tunnel_sessions_lock:
                    session = self.tunnel_sessions.pop(sid, None)
                if session:
                    session['close_event'].set()
                    try:
                        session['socket'].close()
                    except Exception:
                        pass
                    self.logger.info(f"Closed tunnel session {sid} after proxy link drop for {node_id}")

    def link_closed_callback(self, link):
        """Handle link closure - clean up mappings"""
        self.logger.info("Link closed, cleaning up mappings")

        # Find node_id associated with this link
        node_id = self.link_to_node.get(link)

        if node_id:
            # Remove from both mappings
            if node_id in self.node_links:
                del self.node_links[node_id]
                self.logger.info(f"Removed link mapping for node {node_id}")

            if link in self.link_to_node:
                del self.link_to_node[link]

            # Remove send lock for this node
            if node_id in self.send_locks:
                del self.send_locks[node_id]

        # Remove from active_links
        links_to_remove = [link_hash for link_hash, l in self.active_links.items() if l == link]
        for link_hash in links_to_remove:
            del self.active_links[link_hash]
            self.logger.debug(f"Removed link from active_links: {link_hash}")

    def resource_started_callback(self, resource):

        self.logger.info("Began transferring resource")

    def link_established(self, link):
        """Handle new RNS Link establishment for monitoring traffic"""

        # Set up link callbacks
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_started_callback(self.resource_started_callback)
        link.set_resource_callback(self.link_resource_callback)
        link.set_resource_concluded_callback(self.link_resource_concluded_callback)
        link.set_remote_identified_callback(self.link_identified_callback)
        link.set_link_closed_callback(self.link_closed_callback)

        self.logger.info(f"New monitoring Link established")

        self.logger.info("Awaiting link identity")

        # Store link reference using link hash as key
        link_hash = link.link_id.hex() if hasattr(link, 'link_id') else str(id(link))
        self.active_links[link_hash] = link
        self.logger.debug(f"Stored monitoring link with hash: {link_hash}")

    def proxy_link_established(self, link):
        """Handle new RNS Link establishment for proxy traffic"""

        # Set up link callbacks for proxy
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_started_callback(self.resource_started_callback)
        link.set_resource_callback(self.proxy_resource_callback)
        link.set_resource_concluded_callback(self.proxy_resource_concluded_callback)
        link.set_remote_identified_callback(self.proxy_link_identified_callback)
        link.set_link_closed_callback(self.proxy_link_closed_callback)

        self.logger.info(f"New proxy Link established")

        self.logger.info("Awaiting proxy link identity")

        # Store proxy link reference
        link_hash = link.link_id.hex() if hasattr(link, 'link_id') else str(id(link))
        self.proxy_links[link_hash] = link
        self.logger.debug(f"Stored proxy link with hash: {link_hash}")

    def link_resource_callback(self, resource):
        """Handle incoming resources over RNS Link"""
        self.logger.debug(f"Incoming resource - hash: {resource.get_hash().hex()}, size: {resource.size if hasattr(resource, 'size') else 'unknown'}")
        return True

    def link_resource_concluded_callback(self, resource):
        self.logger.debug(f"Resource concluded - status: {resource.status}, size: {resource.size if hasattr(resource, 'size') else 'unknown'}")

        """Handle concluded resource transfers"""
        if resource.status == RNS.Resource.COMPLETE:
            try:
                # Decode resource data
                data = resource.data.read()
                self.logger.debug(f"Resource data size: {len(data)} bytes")

                json_data = data.decode("utf-8")

                node_data = json.loads(data)

                msg_type = node_data.get('type', 'health_data')
                node_id = node_data.get('node_id', 'unknown')
                self.logger.info(f"Received message type: {msg_type} from node: {node_id}")

                # Always update node→link mapping for every message type.
                # This is critical: if the client recreates the monitoring link between
                # sending health_data and a proxy_request, we must record the new link
                # here so send_proxy_response sends on the correct (live) link.
                if node_id and node_id != 'unknown':
                    if hasattr(resource, 'link') and resource.link:
                        if resource.link.status == RNS.Link.ACTIVE:
                            prev_link = self.node_links.get(node_id)
                            if prev_link != resource.link:
                                self.logger.info(f"[PROXY] node_links updated for {node_id} "
                                                 f"(type={msg_type}, link changed)")
                            self.node_links[node_id] = resource.link
                            self.link_to_node[resource.link] = node_id
                            # Record identity→node mapping so link_identified_callback can
                            # update node_links immediately on the NEXT reconnect, before
                            # any message arrives.  Use link_to_identity (populated by the
                            # identified callback) rather than resource.link.remote_identity
                            # which doesn't exist in this RNS version.
                            identity = self.link_to_identity.get(resource.link)
                            if identity:
                                self.identity_to_node[identity.hash.hex()] = node_id
                        else:
                            self.logger.warning(f"[PROXY] Resource link for {node_id} not active "
                                                f"(status: {resource.link.status}, type={msg_type})")
                    else:
                        # Fallback: find any active link
                        self.logger.warning(f"[PROXY] Resource has no link attr for {node_id} "
                                            f"(type={msg_type}), searching active links")
                        for link_key, link in self.active_links.items():
                            if link.status == RNS.Link.ACTIVE:
                                self.node_links[node_id] = link
                                self.link_to_node[link] = node_id
                                self.logger.info(f"[PROXY] node_links updated for {node_id} via fallback")
                                break

                if msg_type == 'health_data':
                    self.logger.info(f"Received health data from node: {node_id}")
                    self.store_node_data(node_data)

                elif msg_type == 'command_result':
                    self.logger.info(f"Received command result from node: {node_data.get('node_id', 'unknown')}")
                    self.handle_command_result(node_data)
                elif msg_type == 'file_chunk':
                    self.logger.info(f"Received file chunk from node: {node_data.get('node_id', 'unknown')}")
                    self.handle_file_chunk(node_data)
                elif msg_type == 'file_complete':
                    self.logger.info(f"Received file completion from node: {node_data.get('node_id', 'unknown')}")
                    self.handle_file_complete(node_data)
                elif msg_type == 'proxy_request':
                    self.logger.info(f"Received proxy request from node: {node_data.get('request_id', 'unknown')}")
                    self.handle_proxy_request(node_data)
                elif msg_type == 'proxy_connect':
                    self.logger.info(f"Received proxy CONNECT from node: {node_data.get('node_id', 'unknown')}")
                    self.handle_proxy_connect(node_data)
                elif msg_type == 'proxy_tunnel_data':
                    self.handle_proxy_tunnel_data(node_data)
                elif msg_type == 'proxy_tunnel_close':
                    self.handle_proxy_tunnel_close(node_data)
                else:
                    self.logger.warning(f"Unknown message type: {msg_type}")

            except Exception as e:
                self.logger.error(f"Error processing resource data: {e}")
        else:
            self.logger.warning(f"Resource transfer failed with status: {resource.status}")
    
    def store_node_data(self, data):
        """Store node data in the database"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    node_id = data.get('node_id')
                    # Use server's timestamp for consistency across time zones
                    timestamp = datetime.now().isoformat()

                    # Insert or update node record
                    cursor.execute('''
                        INSERT OR REPLACE INTO nodes (node_id, last_seen)
                        VALUES (?, ?)
                    ''', (node_id, timestamp))

                    # Insert status record with all data fields
                    cursor.execute('''
                        INSERT INTO node_status (
                            node_id, timestamp, gps_data, services_data,
                            storage_data, network_data, users_data,
                            power_data, system_data, rns_data, client_config_data
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        node_id,
                        timestamp,
                        json.dumps(data.get('gps')) if data.get('gps') else None,
                        json.dumps(data.get('services')) if data.get('services') else None,
                        json.dumps(data.get('storage')) if data.get('storage') else None,
                        json.dumps(data.get('network')) if data.get('network') else None,
                        json.dumps(data.get('users')) if data.get('users') else None,
                        json.dumps(data.get('power')) if data.get('power') else None,
                        json.dumps(data.get('system')) if data.get('system') else None,
                        json.dumps(data.get('rns')) if data.get('rns') else None,
                        json.dumps(data.get('client_config')) if data.get('client_config') else None
                    ))

                    conn.commit()
                    conn.close()

                    self.logger.info(f"Stored data for node {node_id}")
                    break

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))  # Exponential backoff: 0.1s, 0.2s, 0.4s
                    continue
                else:
                    self.logger.error(f"Database error after {attempt + 1} attempts: {e}")
                    raise
            except Exception as e:
                self.logger.error(f"Database error: {e}")
                raise
    
    def get_all_nodes(self):
        """Get all nodes from the database"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT node_id, last_seen, created_at FROM nodes
            ORDER BY last_seen DESC
        ''')

        nodes = cursor.fetchall()
        conn.close()

        return nodes
    
    def get_node_latest_status(self, node_id):
        """Get the latest status for a specific node"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM node_status
            WHERE node_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (node_id,))

        status = cursor.fetchone()
        conn.close()

        return status
    
    def get_node_history(self, node_id, limit=100):
        """Get status history for a specific node"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM node_status
            WHERE node_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (node_id, limit))

        history = cursor.fetchall()
        conn.close()

        return history
    
    def cleanup_old_data(self, days_to_keep=30):
        """Clean up old data from the database"""
        with self.db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60.0)
            conn.execute('PRAGMA busy_timeout=60000')
            cursor = conn.cursor()

            cursor.execute('''
                DELETE FROM node_status
                WHERE received_at < datetime('now', '-{} days')
            '''.format(days_to_keep))

            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            self.logger.info(f"Cleaned up {deleted} old records")
    
    def get_database_stats(self):
        """Get database statistics"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM nodes')
        node_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM node_status')
        status_count = cursor.fetchone()[0]

        cursor.execute('''
            SELECT MIN(received_at), MAX(received_at) FROM node_status
        ''')
        date_range = cursor.fetchone()

        conn.close()

        return {
            'node_count': node_count,
            'status_records': status_count,
            'oldest_record': date_range[0],
            'newest_record': date_range[1]
        }

    # Command Management Methods

    def create_command(self, node_id, command, created_by='web'):
        """Create a new command for a node"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    cursor.execute('''
                        INSERT INTO commands (node_id, command, status, created_by)
                        VALUES (?, ?, 'pending', ?)
                    ''', (node_id, command, created_by))

                    command_id = cursor.lastrowid
                    conn.commit()
                    conn.close()

                    self.logger.info(f"Created command {command_id} for node {node_id}: {command}")
                    return command_id

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error creating command after {attempt + 1} attempts: {e}")
                    return None
            except Exception as e:
                self.logger.error(f"Error creating command: {e}")
                return None

    def get_pending_commands(self, node_id):
        """Get all pending commands for a node"""
        with self.db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60.0)
            conn.execute('PRAGMA busy_timeout=60000')
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, command, created_at, timeout FROM commands
                WHERE node_id = ? AND status = 'pending'
                ORDER BY created_at ASC
            ''', (node_id,))

            commands = cursor.fetchall()
            conn.close()

            return commands

    def get_stuck_commands(self, node_id):
        """Get commands stuck in 'sent' status for longer than their configured timeout.
        Each command's own timeout value is used plus a 60-second LoRa transfer buffer
        (to account for the time to download the command and upload the result over
        the half-duplex radio link).  A 30s default command with timeout=30 won't
        be retried until 90s; a 5-minute nmap scan with timeout=300 won't be retried
        until 360s, avoiding spurious retries that flood the channel."""
        with self.db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60.0)
            conn.execute('PRAGMA busy_timeout=60000')
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, command, timeout FROM commands
                WHERE node_id = ? AND status = 'sent'
                AND sent_at IS NOT NULL
                AND datetime(sent_at) < datetime('now', '-' || (timeout + 60) || ' seconds')
                ORDER BY created_at ASC
            ''', (node_id,))

            commands = cursor.fetchall()
            conn.close()

            return commands

    def update_command_status(self, command_id, status, sent_at=None):
        """Update command status"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    if sent_at:
                        cursor.execute('''
                            UPDATE commands
                            SET status = ?, sent_at = ?
                            WHERE id = ?
                        ''', (status, sent_at, command_id))
                    else:
                        cursor.execute('''
                            UPDATE commands
                            SET status = ?
                            WHERE id = ?
                        ''', (status, command_id))

                    conn.commit()
                    conn.close()

                    self.logger.info(f"Updated command {command_id} status to {status}")
                    break

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error updating command status after {attempt + 1} attempts: {e}")
                    raise
            except Exception as e:
                self.logger.error(f"Error updating command status: {e}")
                raise

    def store_command_result(self, command_id, stdout, stderr, exit_code, execution_time, error=None):
        """Store command execution result"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    # Check if result already exists for this command
                    cursor.execute('''
                        SELECT id FROM command_results WHERE command_id = ?
                    ''', (command_id,))

                    existing_result = cursor.fetchone()

                    if existing_result:
                        self.logger.warning(f"Result already exists for command {command_id}, skipping duplicate")
                        conn.close()
                        return

                    # Store result
                    cursor.execute('''
                        INSERT INTO command_results (command_id, stdout, stderr, exit_code, execution_time, error)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (command_id, stdout, stderr, exit_code, execution_time, error))

                    # Update command status
                    cursor.execute('''
                        UPDATE commands
                        SET status = ?, completed_at = ?
                        WHERE id = ?
                    ''', ('completed' if exit_code == 0 and not error else 'failed',
                          datetime.now().isoformat(), command_id))

                    conn.commit()
                    conn.close()

                    self.logger.info(f"Stored result for command {command_id}")
                    break

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error storing command result after {attempt + 1} attempts: {e}")
                    raise
            except Exception as e:
                self.logger.error(f"Error storing command result: {e}")
                raise

    def get_command_results(self, node_id, limit=50):
        """Get command results for a node"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT c.*, cr.stdout, cr.stderr, cr.exit_code, cr.execution_time,
                   cr.error, cr.received_at as result_received_at
            FROM commands c
            LEFT JOIN command_results cr ON c.id = cr.command_id
            WHERE c.node_id = ?
            ORDER BY c.created_at DESC
            LIMIT ?
        ''', (node_id, limit))

        results = cursor.fetchall()
        conn.close()

        return results

    # File Transfer Methods

    def create_file_transfer(self, node_id, transfer_type, file_path, file_name, file_size=None, created_by='web'):
        """Create a new file transfer request"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    cursor.execute('''
                        INSERT INTO file_transfers (node_id, transfer_type, file_path, file_name, file_size, created_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (node_id, transfer_type, file_path, file_name, file_size, created_by))

                    transfer_id = cursor.lastrowid
                    conn.commit()
                    conn.close()

                    self.logger.info(f"Created {transfer_type} transfer {transfer_id} for node {node_id}: {file_path}")
                    return transfer_id

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error creating file transfer after {attempt + 1} attempts: {e}")
                    return None
            except Exception as e:
                self.logger.error(f"Error creating file transfer: {e}")
                return None

    def get_pending_file_transfers(self, node_id):
        """Get all pending file transfers for a node"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM file_transfers
            WHERE node_id = ? AND status = 'pending'
            ORDER BY created_at ASC
        ''', (node_id,))

        transfers = cursor.fetchall()
        conn.close()

        return transfers

    def update_file_transfer_status(self, transfer_id, status, progress=None, error=None, reset_started_at=False):
        """Update file transfer status.

        reset_started_at=True forces started_at to NOW, restarting the stuck-transfer
        clock. Use this when all upload chunks have been delivered to the client so the
        client gets a fresh 120-second window to write the file and send file_complete
        (rather than inheriting the window that started with the first chunk send).
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    update_fields = ['status = ?']
                    params = [status]

                    if status == 'in_progress' and not error:
                        if reset_started_at:
                            # Explicitly reset the clock (e.g. after all chunks delivered).
                            update_fields.append('started_at = ?')
                        else:
                            # Only stamp started_at the first time; don't reset it on
                            # every chunk progress update (that would break stuck detection).
                            update_fields.append('started_at = COALESCE(started_at, ?)')
                        params.append(datetime.now().isoformat())
                    elif status in ('completed', 'failed'):
                        update_fields.append('completed_at = ?')
                        params.append(datetime.now().isoformat())

                    if progress is not None:
                        update_fields.append('progress = ?')
                        params.append(progress)

                    if error:
                        update_fields.append('error = ?')
                        params.append(error)

                    params.append(transfer_id)

                    cursor.execute(f'''
                        UPDATE file_transfers
                        SET {', '.join(update_fields)}
                        WHERE id = ?
                    ''', params)

                    conn.commit()
                    conn.close()

                    self.logger.info(f"Updated file transfer {transfer_id} status to {status}")
                    break

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error updating file transfer status after {attempt + 1} attempts: {e}")
                    raise
            except Exception as e:
                self.logger.error(f"Error updating file transfer status: {e}")
                raise

    def store_file_chunk(self, transfer_id, chunk_number, chunk_data):
        """Store a file chunk"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    cursor.execute('''
                        INSERT INTO file_chunks (transfer_id, chunk_number, chunk_data)
                        VALUES (?, ?, ?)
                    ''', (transfer_id, chunk_number, chunk_data))

                    # Update chunks received count
                    cursor.execute('''
                        UPDATE file_transfers
                        SET chunks_received = chunks_received + 1
                        WHERE id = ?
                    ''', (transfer_id,))

                    conn.commit()
                    conn.close()
                    break

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    self.logger.warning(f"Database locked on attempt {attempt + 1}, retrying...")
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error storing file chunk after {attempt + 1} attempts: {e}")
                    raise
            except Exception as e:
                self.logger.error(f"Error storing file chunk: {e}")
                raise

    def get_file_transfer(self, transfer_id):
        """Get file transfer details"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM file_transfers WHERE id = ?', (transfer_id,))
        transfer = cursor.fetchone()
        conn.close()

        return transfer

    def get_stuck_file_transfers(self, node_id):
        """Get file transfers stuck in 'in_progress' status for more than 120 seconds"""
        with self.db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60.0)
            conn.execute('PRAGMA busy_timeout=60000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM file_transfers
                WHERE node_id = ? AND status = 'in_progress'
                AND started_at IS NOT NULL
                AND datetime(started_at) < datetime('now', '-120 seconds')
                ORDER BY created_at ASC
            ''', (node_id,))

            transfers = cursor.fetchall()
            conn.close()

            return transfers

    def get_file_chunks(self, transfer_id):
        """Get all chunks for a file transfer"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT chunk_number, chunk_data FROM file_chunks
            WHERE transfer_id = ?
            ORDER BY chunk_number ASC
        ''', (transfer_id,))

        chunks = cursor.fetchall()
        conn.close()

        return chunks

    def get_file_transfers(self, node_id, limit=50):
        """Get file transfers for a node"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM file_transfers
            WHERE node_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        ''', (node_id, limit))

        transfers = cursor.fetchall()
        conn.close()

        return transfers

    # Logging Methods

    def add_log(self, node_id, log_type, level, message, details=None):
        """Add a log entry"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()

                    cursor.execute('''
                        INSERT INTO logs (node_id, log_type, level, message, details)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (node_id, log_type, level, message, details))

                    conn.commit()
                    conn.close()
                    break

            except sqlite3.OperationalError as e:
                if 'locked' in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                else:
                    self.logger.error(f"Error adding log after {attempt + 1} attempts: {e}")
                    raise
            except Exception as e:
                self.logger.error(f"Error adding log: {e}")
                raise

    def get_logs(self, node_id=None, log_type=None, level=None, limit=100):
        """Get logs with optional filters"""
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute('PRAGMA busy_timeout=60000')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = 'SELECT * FROM logs WHERE 1=1'
        params = []

        if node_id:
            query += ' AND node_id = ?'
            params.append(node_id)

        if log_type:
            query += ' AND log_type = ?'
            params.append(log_type)

        if level:
            query += ' AND level = ?'
            params.append(level)

        query += ' ORDER BY created_at DESC LIMIT ?'
        params.append(limit)

        cursor.execute(query, params)
        logs = cursor.fetchall()
        conn.close()

        return logs

    # Message Handlers

    def handle_command_result(self, data):
        """Handle command result from client"""
        try:
            command_id = data.get('command_id')
            stdout = data.get('stdout', '')
            stderr = data.get('stderr', '')
            exit_code = data.get('exit_code', -1)
            execution_time = data.get('execution_time', 0)
            error = data.get('error')

            self.logger.info(f"Handling command result: command_id={command_id}, exit_code={exit_code}, exec_time={execution_time:.2f}s")

            self.store_command_result(command_id, stdout, stderr, exit_code, execution_time, error)

            if error:
                self.add_log(data.get('node_id'), 'command', 'ERROR',
                           f"Command {command_id} failed", error)

        except Exception as e:
            self.logger.error(f"Error handling command result: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

    def handle_file_chunk(self, data):
        """Handle file chunk from client"""
        try:
            transfer_id = data.get('transfer_id')
            chunk_number = data.get('chunk_number')
            chunk_data_b64 = data.get('chunk_data')

            # Decode base64 chunk data
            chunk_data = base64.b64decode(chunk_data_b64)

            self.store_file_chunk(transfer_id, chunk_number, chunk_data)

            # Update progress
            transfer = self.get_file_transfer(transfer_id)
            if transfer and transfer['chunks_total']:
                progress = int((transfer['chunks_received'] + 1) / transfer['chunks_total'] * 100)
                self.update_file_transfer_status(transfer_id, 'in_progress', progress=progress)

        except Exception as e:
            self.logger.error(f"Error handling file chunk: {e}")
            if transfer_id:
                self.update_file_transfer_status(transfer_id, 'failed', error=str(e))

    def handle_file_complete(self, data):
        """Handle file transfer completion from client"""
        try:
            transfer_id = data.get('transfer_id')
            success = data.get('success', False)
            error = data.get('error')

            if success:
                self.update_file_transfer_status(transfer_id, 'completed', progress=100)
                self.add_log(data.get('node_id'), 'file_transfer', 'INFO',
                           f"File transfer {transfer_id} completed successfully")
            else:
                self.update_file_transfer_status(transfer_id, 'failed', error=error)
                self.add_log(data.get('node_id'), 'file_transfer', 'ERROR',
                           f"File transfer {transfer_id} failed", error)

        except Exception as e:
            self.logger.error(f"Error handling file completion: {e}")

    def handle_proxy_request(self, data):
        """Handle HTTP proxy request from client - make the actual web request"""
        request_id = None
        node_id = None

        try:
            request_id = data.get('request_id')
            node_id = data.get('node_id')
            method = data.get('method', 'GET')
            url = data.get('url', '')
            headers = data.get('headers', {})
            body_b64 = data.get('body')

            self.logger.info(f"Proxy request {request_id}: {method} {url}")

            # Decode body if present
            body = base64.b64decode(body_b64) if body_b64 else None

            # Create HTTP request
            req = urllib.request.Request(url, data=body, method=method)

            # Add headers (skip Host header as it will be set automatically)
            for header_name, header_value in headers.items():
                if header_name.lower() not in ['host', 'connection', 'proxy-connection']:
                    req.add_header(header_name, header_value)

            # Make the request
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    response_body = response.read()
                    response_headers = dict(response.headers)
                    status_code = response.status

                    # Send response back to client
                    response_message = {
                        'type': 'proxy_response',
                        'request_id': request_id,
                        'status_code': status_code,
                        'headers': response_headers,
                        'body': base64.b64encode(response_body).decode('utf-8') if response_body else None
                    }

                    self.send_proxy_response(node_id, response_message)
                    self.logger.info(f"Proxy request {request_id} completed: {status_code}")

            except urllib.error.HTTPError as e:
                # Handle HTTP errors (4xx, 5xx)
                error_body = e.read() if hasattr(e, 'read') else b''
                response_message = {
                    'type': 'proxy_response',
                    'request_id': request_id,
                    'status_code': e.code,
                    'headers': dict(e.headers) if hasattr(e, 'headers') else {},
                    'body': base64.b64encode(error_body).decode('utf-8') if error_body else None
                }
                self.send_proxy_response(node_id, response_message)
                self.logger.warning(f"Proxy request {request_id} HTTP error: {e.code}")

            except urllib.error.URLError as e:
                # Handle URL errors (connection failures, DNS, etc.)
                error_message = str(e.reason) if hasattr(e, 'reason') else str(e)
                response_message = {
                    'type': 'proxy_response',
                    'request_id': request_id,
                    'status_code': 502,  # Bad Gateway
                    'headers': {'Content-Type': 'text/plain'},
                    'body': base64.b64encode(f"Proxy Error: {error_message}".encode('utf-8')).decode('utf-8')
                }
                self.send_proxy_response(node_id, response_message)
                self.logger.error(f"Proxy request {request_id} URL error: {error_message}")

        except Exception as e:
            self.logger.error(f"Error handling proxy request: {e}")
            if request_id and node_id:
                # Send error response
                error_response = {
                    'type': 'proxy_response',
                    'request_id': request_id,
                    'status_code': 500,
                    'headers': {'Content-Type': 'text/plain'},
                    'body': base64.b64encode(f"Server Error: {str(e)}".encode('utf-8')).decode('utf-8')
                }
                try:
                    self.send_proxy_response(node_id, error_response)
                except:
                    pass

    def handle_proxy_connect(self, data):
        """Open a TCP connection to the requested target and start relaying tunnel data."""
        request_id = data.get('request_id')
        session_id = data.get('session_id')
        node_id = data.get('node_id')
        host = data.get('host', '')
        port = data.get('port', 443)

        try:
            self.logger.info(f"CONNECT request: {host}:{port} for session {session_id}")
            sock = socket.create_connection((host, port), timeout=30)
            sock.settimeout(1.0)

            session_close = threading.Event()
            session_send_lock = threading.Lock()

            with self.tunnel_sessions_lock:
                self.tunnel_sessions[session_id] = {
                    'socket': sock,
                    'node_id': node_id,
                    'close_event': session_close,
                    'send_lock': session_send_lock,
                }

            # Background thread: read from target TCP socket → send chunks over RNS to client
            def relay_target_to_client():
                try:
                    while not session_close.is_set():
                        try:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            msg = {
                                'type': 'proxy_tunnel_data',
                                'session_id': session_id,
                                'data': base64.b64encode(chunk).decode('utf-8'),
                            }
                            data_bytes = json.dumps(msg, separators=(',', ':')).encode('utf-8')
                            # Serialize sends for this session so RNS resources don't collide
                            with session_send_lock:
                                link = self.node_links.get(node_id)
                                if not link or link.status != RNS.Link.ACTIVE:
                                    self.logger.warning(f"[PROXY] No active monitoring link for {node_id} "
                                                        f"in tunnel relay, closing session {session_id}")
                                    break
                                resource = RNS.Resource(data_bytes, link)
                                resource.advertise()
                                deadline = time.time() + 30
                                while resource.status < RNS.Resource.COMPLETE and time.time() < deadline:
                                    time.sleep(0.05)
                        except socket.timeout:
                            continue
                        except Exception:
                            break
                finally:
                    session_close.set()
                    with self.tunnel_sessions_lock:
                        self.tunnel_sessions.pop(session_id, None)
                    close_msg = {'type': 'proxy_tunnel_close', 'session_id': session_id}
                    try:
                        self.send_proxy_response(node_id, close_msg)
                    except Exception:
                        pass
                    try:
                        sock.close()
                    except Exception:
                        pass
                    self.logger.info(f"CONNECT tunnel {session_id} relay thread exited")

            threading.Thread(target=relay_target_to_client, daemon=True).start()

            # Send ack so the client's do_CONNECT can respond 200 to the agent
            ack = {
                'type': 'proxy_connect_ack',
                'request_id': request_id,
                'session_id': session_id,
                'success': True,
            }
            self.send_proxy_response(node_id, ack)
            self.logger.info(f"CONNECT tunnel established: {host}:{port} session {session_id}")

        except Exception as e:
            self.logger.error(f"CONNECT failed for {host}:{port}: {e}")
            ack = {
                'type': 'proxy_connect_ack',
                'request_id': request_id,
                'session_id': session_id,
                'success': False,
                'error': str(e),
            }
            try:
                self.send_proxy_response(node_id, ack)
            except Exception:
                pass

    def handle_proxy_tunnel_data(self, data):
        """Forward a data chunk from the client to the target TCP socket."""
        session_id = data.get('session_id')
        chunk_b64 = data.get('data', '')
        chunk = base64.b64decode(chunk_b64) if chunk_b64 else b''

        with self.tunnel_sessions_lock:
            session = self.tunnel_sessions.get(session_id)

        if session and chunk:
            try:
                session['socket'].sendall(chunk)
            except Exception as e:
                self.logger.error(f"Tunnel write error for session {session_id}: {e}")
                session['close_event'].set()
                try:
                    session['socket'].close()
                except Exception:
                    pass
                with self.tunnel_sessions_lock:
                    self.tunnel_sessions.pop(session_id, None)
        elif not session:
            self.logger.warning(f"proxy_tunnel_data for unknown session {session_id}")

    def handle_proxy_tunnel_close(self, data):
        """Handle tunnel close notification from the client side."""
        session_id = data.get('session_id')
        with self.tunnel_sessions_lock:
            session = self.tunnel_sessions.pop(session_id, None)
        if session:
            session['close_event'].set()
            try:
                session['socket'].close()
            except Exception:
                pass
            self.logger.info(f"CONNECT tunnel {session_id} closed by client")

    def send_proxy_response(self, node_id, response_message):
        """Send proxy response back to client via the monitoring link."""
        try:
            msg_type = response_message.get('type', 'unknown')
            req_id = response_message.get('request_id',
                      response_message.get('session_id', 'N/A'))
            self.logger.info(f"[PROXY] send_proxy_response: node={node_id} "
                             f"type={msg_type} id={req_id}")

            # Use the monitoring link — proxy traffic shares it to avoid
            # LoRa radio contention from two simultaneous RNS links.
            link = self.node_links.get(node_id)

            if not link:
                self.logger.warning(f"[PROXY] No link mapping for node {node_id} "
                                    f"(known nodes: {list(self.node_links.keys())})")
                return False

            if link.status != RNS.Link.ACTIVE:
                self.logger.warning(f"[PROXY] Link for node {node_id} is not active "
                                    f"(status: {link.status}) — cannot send {msg_type}")
                return False

            json_data = json.dumps(response_message, separators=(',', ':'))
            data_bytes = json_data.encode('utf-8')
            self.logger.info(f"[PROXY] Advertising {len(data_bytes)}B resource to {node_id} "
                             f"for {msg_type}")

            resource = RNS.Resource(data_bytes, link)
            resource.advertise()

            return True

        except Exception as e:
            self.logger.error(f"[PROXY] Error sending proxy response to {node_id}: {e}")
            return False

    # Announce Loop

    def announce_loop(self):
        """Periodically announce server presence for both destinations"""
        self.logger.info("Announce loop started")
        while self.running:
            try:
                time.sleep(self.announce_interval)
                self.destination.announce()
                self.proxy_destination.announce()
                self.logger.debug(f"Announced monitoring destination: {self.destination.hash.hex()}")
                self.logger.debug(f"Announced proxy destination: {self.proxy_destination.hash.hex()}")
            except Exception as e:
                self.logger.error(f"Error in announce loop: {e}")

    # Command and File Transfer Sending

    def poll_pending_jobs(self):
        """Poll database for pending commands and file transfers"""
        while self.running:
            try:
                # Check for pending commands
                for node_id, link in list(self.node_links.items()):
                    if link.status != RNS.Link.ACTIVE:
                        # Remove inactive links
                        del self.node_links[node_id]
                        continue

                    # Get pending commands
                    commands = self.get_pending_commands(node_id)
                    for cmd in commands:
                        command_id, command, created_at, timeout = cmd
                        self.send_command_to_client(node_id, command_id, command, timeout)

                    # Get stuck commands (sent but no response within their timeout + 60s buffer)
                    stuck_commands = self.get_stuck_commands(node_id)
                    for cmd in stuck_commands:
                        command_id, command, timeout = cmd
                        self.logger.warning(f"Retrying stuck command {command_id}")
                        # Reset to pending so it gets resent
                        self.update_command_status(command_id, 'pending')
                        self.send_command_to_client(node_id, command_id, command, timeout)

                    # Get pending file transfers
                    transfers = self.get_pending_file_transfers(node_id)
                    for transfer in transfers:
                        transfer_id = transfer['id']
                        transfer_type = transfer['transfer_type']
                        file_path = transfer['file_path']

                        if transfer_type == 'download':
                            self.send_file_request_to_client(node_id, transfer_id, file_path, transfer_type)
                        elif transfer_type == 'upload':
                            # Get file chunks from database
                            chunks = self.get_file_chunks(transfer_id)
                            if chunks:
                                # Reassemble file data
                                file_data = b''
                                for chunk in chunks:
                                    file_data += chunk[1]  # chunk_data is second element
                                self.send_file_to_client(node_id, transfer_id, file_data, file_path)

                    # Get stuck file transfers (in_progress for >60 seconds without completion)
                    stuck_transfers = self.get_stuck_file_transfers(node_id)
                    for transfer in stuck_transfers:
                        transfer_id = transfer['id']
                        self.logger.warning(f"File transfer {transfer_id} stuck in 'in_progress' for >60s, marking as failed")
                        self.update_file_transfer_status(transfer_id, 'failed',
                                                        error="Transfer timed out waiting for client confirmation")

                time.sleep(5)  # Poll every 5 seconds

            except Exception as e:
                self.logger.error(f"Error in polling thread: {e}")
                time.sleep(5)

    def send_command_to_client(self, node_id, command_id, command, timeout=300):
        """Send command to client via active link"""
        # Get or create lock for this node
        if node_id not in self.send_locks:
            self.send_locks[node_id] = threading.Lock()

        with self.send_locks[node_id]:
            try:
                # Find active link for this node
                link = self.node_links.get(node_id)

                if not link:
                    self.logger.warning(f"No link mapping for node {node_id}")
                    return False

                if link.status != RNS.Link.ACTIVE:
                    self.logger.warning(f"Link for node {node_id} is not active (status: {link.status}), removing stale mapping")
                    # Remove stale link mapping from both dictionaries
                    del self.node_links[node_id]
                    if link in self.link_to_node:
                        del self.link_to_node[link]
                    return False

                # Delay to avoid resource conflicts - increased for LoRa timing
                # Add random jitter to prevent synchronized collisions
                delay = 1.5 + random.uniform(0, 0.5)
                time.sleep(delay)

                # Prepare command message
                message = {
                    'type': 'command',
                    'command_id': command_id,
                    'command': command,
                    'timeout': timeout
                }

                json_data = json.dumps(message, separators=(',', ':'))
                data_bytes = json_data.encode('utf-8')

                # Send via RNS Resource and wait for completion.
                # Previously resource.advertise() returned immediately, leaving the
                # resource transfer in progress in a background RNS thread.  While
                # the server was sending the command (30-60 s on LoRa), the client
                # simultaneously tried to upload health data or a command result.
                # On a half-duplex radio only one side can transmit at a time, so
                # the two concurrent transfers collided and the client's resource
                # stayed QUEUED for the full 60-second timeout.  By waiting here
                # we hold send_locks[node_id] until the channel is clear, giving
                # the client an uncontested TX window for its reply.
                resource = RNS.Resource(data_bytes, link)
                resource.advertise()

                # Mark 'sent' immediately so stuck-command poller won't retry
                # while we are waiting for the transfer to complete.
                self.update_command_status(command_id, 'sent', sent_at=datetime.now().isoformat())

                # Wait up to 120 s for the command resource to be received.
                xfer_timeout = time.time() + 120
                while resource.status < RNS.Resource.COMPLETE and time.time() < xfer_timeout:
                    time.sleep(0.1)

                if resource.status == RNS.Resource.COMPLETE:
                    self.logger.info(f"Command {command_id} delivered to node {node_id}")
                    return True
                else:
                    self.logger.warning(f"Command {command_id} delivery did not complete "
                                        f"(status: {resource.status}) — will retry")
                    # Reset to pending so poll_pending_jobs retries on next cycle.
                    self.update_command_status(command_id, 'pending')
                    return False

            except Exception as e:
                self.logger.error(f"Error sending command: {e}")
                self.update_command_status(command_id, 'failed')
                self.add_log(node_id, 'command', 'ERROR',
                           f"Failed to send command {command_id}", str(e))
                return False

    def send_file_request_to_client(self, node_id, transfer_id, file_path, transfer_type):
        """Send file transfer request to client"""
        # Get or create lock for this node
        if node_id not in self.send_locks:
            self.send_locks[node_id] = threading.Lock()

        with self.send_locks[node_id]:
            try:
                # Find active link for this node
                link = self.node_links.get(node_id)

                if not link:
                    self.logger.warning(f"No link mapping for node {node_id}")
                    return False

                if link.status != RNS.Link.ACTIVE:
                    self.logger.warning(f"Link for node {node_id} is not active (status: {link.status}), removing stale mapping")
                    # Remove stale link mapping from both dictionaries
                    del self.node_links[node_id]
                    if link in self.link_to_node:
                        del self.link_to_node[link]
                    return False

                # Delay to avoid resource conflicts - increased for LoRa timing
                # Add random jitter to prevent synchronized collisions
                delay = 1.5 + random.uniform(0, 0.5)
                time.sleep(delay)

                # Prepare file transfer message
                message = {
                    'type': 'file_request',
                    'transfer_id': transfer_id,
                    'transfer_type': transfer_type,
                    'file_path': file_path
                }

                json_data = json.dumps(message, separators=(',', ':'))
                data_bytes = json_data.encode('utf-8')

                # Send via RNS Resource and wait for completion (same reasoning
                # as send_command_to_client — prevents half-duplex collisions).
                resource = RNS.Resource(data_bytes, link)
                resource.advertise()

                xfer_timeout = time.time() + 120
                while resource.status < RNS.Resource.COMPLETE and time.time() < xfer_timeout:
                    time.sleep(0.1)

                if resource.status == RNS.Resource.COMPLETE:
                    self.update_file_transfer_status(transfer_id, 'sent')
                    self.logger.info(f"Sent file request {transfer_id} to node {node_id}")
                    return True
                else:
                    self.logger.warning(f"File request {transfer_id} delivery did not complete "
                                        f"(status: {resource.status})")
                    return False

            except Exception as e:
                self.logger.error(f"Error sending file request: {e}")
                self.update_file_transfer_status(transfer_id, 'failed', error=str(e))
                return False

    def send_file_to_client(self, node_id, transfer_id, file_data, file_path):
        """Send file to client (upload)"""
        # Get or create lock for this node
        if node_id not in self.send_locks:
            self.send_locks[node_id] = threading.Lock()

        with self.send_locks[node_id]:
            try:
                # Find active link for this node
                link = self.node_links.get(node_id)

                if not link:
                    self.logger.warning(f"No link mapping for node {node_id}")
                    return False

                if link.status != RNS.Link.ACTIVE:
                    self.logger.warning(f"Link for node {node_id} is not active (status: {link.status}), removing stale mapping")
                    # Remove stale link mapping from both dictionaries
                    del self.node_links[node_id]
                    if link in self.link_to_node:
                        del self.link_to_node[link]
                    return False

                # Delay to avoid resource conflicts - increased for LoRa timing
                # Add random jitter to prevent synchronized collisions
                delay = 1.5 + random.uniform(0, 0.5)
                time.sleep(delay)

                # Split file into chunks.
                # LoRa effective throughput is ~640 bytes/s; the client tears down
                # the link if its health_data resource is queued for >60 s. A 1 MB
                # chunk takes ~27 min to transfer — the health_data timeout fires
                # long before the chunk can complete, killing the link.
                # 16 KB at 640 bytes/s ≈ 25 s, comfortably within the 60 s window.
                chunk_size = 16 * 1024
                chunks = [file_data[i:i+chunk_size] for i in range(0, len(file_data), chunk_size)]

                # Update transfer with chunk info
                with self.db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, isolation_level='DEFERRED')
                    conn.execute('PRAGMA busy_timeout=60000')
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE file_transfers
                        SET chunks_total = ?, status = 'sending'
                        WHERE id = ?
                    ''', (len(chunks), transfer_id))
                    conn.commit()
                    conn.close()

                # Send chunks — single attempt per chunk, never abort on individual
                # chunk failure. RNS handles low-level retransmission internally;
                # the client's file_complete ACK is the authoritative success signal.
                # Holding send_locks[node_id] for multiple retry attempts (each up to
                # 60 s) created perpetual half-duplex collisions on LoRa because the
                # client could not send health data while the lock was held.
                for chunk_num, chunk_data in enumerate(chunks):
                    message = {
                        'type': 'file_upload',
                        'transfer_id': transfer_id,
                        'chunk_number': chunk_num,
                        'chunk_total': len(chunks),
                        'chunk_data': base64.b64encode(chunk_data).decode('utf-8'),
                        'file_path': file_path
                    }

                    json_data = json.dumps(message, separators=(',', ':'))
                    data_bytes = json_data.encode('utf-8')

                    resource = RNS.Resource(data_bytes, link)
                    resource.advertise()

                    chunk_timeout = time.time() + 30
                    while resource.status not in [RNS.Resource.COMPLETE, RNS.Resource.FAILED] and time.time() < chunk_timeout:
                        time.sleep(0.1)

                    if resource.status == RNS.Resource.FAILED:
                        self.logger.warning(f"Chunk {chunk_num}/{len(chunks)-1} showed FAILED status (RNS may auto-retry, transfer {transfer_id})")
                    elif resource.status == RNS.Resource.COMPLETE:
                        self.logger.info(f"Chunk {chunk_num}/{len(chunks)-1} delivered (transfer {transfer_id})")
                    else:
                        self.logger.warning(f"Chunk {chunk_num}/{len(chunks)-1} timed out (status: {resource.status}, transfer {transfer_id})")

                    # Update progress unconditionally — chunk may still arrive via RNS retry
                    progress = int((chunk_num + 1) / len(chunks) * 100)
                    self.update_file_transfer_status(transfer_id, 'in_progress', progress=progress)

                # Don't mark as completed here - wait for client's completion message.
                # Reset started_at to NOW so the stuck-transfer clock gives the client
                # a fresh 120 seconds to write the file and send file_complete, rather
                # than counting from when the first chunk was sent (which could be many
                # seconds ago for large files over slow LoRa links).
                self.update_file_transfer_status(transfer_id, 'in_progress', progress=100, reset_started_at=True)
                self.logger.info(f"File upload {transfer_id} to node {node_id} - all chunks sent, waiting for client confirmation")
                return True

            except Exception as e:
                self.logger.error(f"Error sending file to client: {e}")
                self.update_file_transfer_status(transfer_id, 'failed', error=str(e))
                return False

if __name__ == "__main__":
    import signal
    import sys
    import time
   
    server = MonitoringServer()
        
    def signal_handler(sig, frame):
        print('\nShutting down server...')
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

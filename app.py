#!/usr/bin/env python3
"""
Flask Web UI for RNS Node Monitoring System
Simple, clean interface to visualize node status data
"""

from flask import Flask, render_template, jsonify, request, send_file, flash, redirect, url_for, session, Response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import sqlite3
import json
from datetime import datetime, timedelta
import os
import io
import hashlib
import folium
from folium.plugins import MarkerCluster
import pandas as pd

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# Setup Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Simple User class
class User(UserMixin):
    def __init__(self, id):
        self.id = id

# Database path - should match your server.py configuration
DATABASE_PATH = 'node_monitoring.db'

@login_manager.user_loader
def load_user(user_id):
    """Load user for Flask-Login"""
    return User(user_id)

def hash_password(password):
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def init_auth_db():
    """Initialize authentication table with default credentials"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Create auth table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Check if default user exists
    cursor.execute('SELECT username FROM auth WHERE username = ?', ('admin',))
    if not cursor.fetchone():
        # Create default admin user with password "crebain"
        default_hash = hash_password('crebain')
        cursor.execute('INSERT INTO auth (username, password_hash) VALUES (?, ?)',
                      ('admin', default_hash))

    conn.commit()
    conn.close()

def verify_credentials(username, password):
    """Verify username and password"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT password_hash FROM auth WHERE username = ?', (username,))
    result = cursor.fetchone()
    conn.close()

    if result:
        stored_hash = result['password_hash']
        input_hash = hash_password(password)
        return stored_hash == input_hash
    return False

def get_db_connection():
    """Get database connection"""
    conn = sqlite3.connect(DATABASE_PATH, timeout=60.0)
    conn.execute('PRAGMA busy_timeout=60000')
    conn.row_factory = sqlite3.Row
    return conn

def parse_json_field(field_data):
    """Safely parse JSON field data"""
    if field_data:
        try:
            return json.loads(field_data)
        except (json.JSONDecodeError, TypeError):
            return None
    return None

# Authentication Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if verify_credentials(username, password):
            user = User(username)
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        else:
            flash('Invalid username or password', 'error')

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """Logout user"""
    logout_user()
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Change password page"""
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not verify_credentials(current_user.id, current_password):
            flash('Current password is incorrect', 'error')
        elif new_password != confirm_password:
            flash('New passwords do not match', 'error')
        elif len(new_password) < 4:
            flash('Password must be at least 4 characters', 'error')
        else:
            # Update password
            conn = get_db_connection()
            cursor = conn.cursor()
            new_hash = hash_password(new_password)
            cursor.execute('UPDATE auth SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?',
                          (new_hash, current_user.id))
            conn.commit()
            conn.close()
            flash('Password updated successfully', 'success')
            return redirect(url_for('dashboard'))

    return render_template('change_password.html')

def generate_nodes_map():
    """Generate a Folium map with all node locations"""
    conn = get_db_connection()

    # Get latest GPS coordinates for all nodes
    query = '''
        SELECT DISTINCT n.node_id, ns.gps_data, ns.timestamp
        FROM nodes n
        LEFT JOIN node_status ns ON n.node_id = ns.node_id
        WHERE ns.gps_data IS NOT NULL
        AND ns.id IN (
            SELECT id FROM node_status ns2
            WHERE ns2.node_id = n.node_id
            ORDER BY timestamp DESC
            LIMIT 1
        )
    '''

    results = conn.execute(query).fetchall()
    conn.close()

    # Parse GPS data
    node_locations = []
    for row in results:
        gps_data = parse_json_field(row['gps_data'])
        if gps_data and 'latitude' in gps_data and 'longitude' in gps_data:
            node_locations.append({
                'node_id': row['node_id'],
                'lat': gps_data['latitude'],
                'lon': gps_data['longitude'],
                'altitude': gps_data.get('altitude', 'N/A'),
                'timestamp': row['timestamp']
            })

    # Create map centered on average location or default
    if node_locations:
        avg_lat = sum(n['lat'] for n in node_locations) / len(node_locations)
        avg_lon = sum(n['lon'] for n in node_locations) / len(node_locations)
        start_location = [avg_lat, avg_lon]
        zoom_start = 4
    else:
        start_location = [0, 0]
        zoom_start = 2

    # Create Folium map with dark tiles
    m = folium.Map(
        location=start_location,
        zoom_start=zoom_start,
        tiles='https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        max_zoom=19,
        control_scale=True
    )

    # Add markers for each node
    marker_cluster = MarkerCluster().add_to(m)

    for node in node_locations:
        popup_html = f"""
        <div style="font-family: monospace; min-width: 200px;">
            <h5 style="margin: 0 0 10px 0; color: #00ff00;">{node['node_id']}</h5>
            <table style="width: 100%; font-size: 12px;">
                <tr><td><b>Latitude:</b></td><td>{node['lat']:.6f}</td></tr>
                <tr><td><b>Longitude:</b></td><td>{node['lon']:.6f}</td></tr>
                <tr><td><b>Altitude:</b></td><td>{node['altitude']}</td></tr>
                <tr><td><b>Updated:</b></td><td>{node['timestamp']}</td></tr>
            </table>
            <a href="/node/{node['node_id']}" style="color: #00aaff; text-decoration: none;">
                View Details →
            </a>
        </div>
        """

        folium.Marker(
            location=[node['lat'], node['lon']],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=node['node_id'],
            icon=folium.Icon(color='green', icon='wifi', prefix='fa')
        ).add_to(marker_cluster)

    return m

@app.route('/map')
@login_required
def nodes_map():
    """Serve the nodes map"""
    m = generate_nodes_map()
    return Response(m._repr_html_(), mimetype='text/html')

@app.route('/')
@login_required
def dashboard():
    """Main dashboard view"""
    conn = get_db_connection()
    
    # Get all nodes with their latest status (updated query for new fields)
    nodes_query = '''
        SELECT n.node_id, n.last_seen, n.created_at,
               ns.timestamp, ns.gps_data, ns.services_data,
               ns.storage_data, ns.network_data, ns.users_data,
               ns.power_data, ns.system_data, ns.rns_data,
               ns.client_config_data, ns.received_at
        FROM nodes n
        LEFT JOIN node_status ns ON n.node_id = ns.node_id
        WHERE ns.id IN (
            SELECT MAX(id) FROM node_status
            GROUP BY node_id
        ) OR ns.id IS NULL
        ORDER BY n.last_seen DESC
    '''
    
    nodes = conn.execute(nodes_query).fetchall()
    
    # Get database statistics
    stats_query = '''
        SELECT 
            COUNT(DISTINCT node_id) as total_nodes,
            COUNT(*) as total_records,
            MIN(received_at) as oldest_record,
            MAX(received_at) as newest_record
        FROM node_status
    '''
    stats = conn.execute(stats_query).fetchone()
    
    conn.close()
    
    # Process node data
    processed_nodes = []
    online_count = 0
    offline_count = 0
    
    for node in nodes:
        node_data = {
            'node_id': node['node_id'],
            'last_seen': node['last_seen'],
            'created_at': node['created_at'],
            'gps': parse_json_field(node['gps_data']),
            'services': parse_json_field(node['services_data']),
            'storage': parse_json_field(node['storage_data']),
            'network': parse_json_field(node['network_data']),
            'users': parse_json_field(node['users_data']),
            'power': parse_json_field(node['power_data']),
            'system': parse_json_field(node['system_data']),
            'rns': parse_json_field(node['rns_data']),
            'client_config': parse_json_field(node['client_config_data']),
            'received_at': node['received_at']
        }
        
        # Calculate node status (online/offline based on last_seen)
        if node['last_seen']:
            last_seen = datetime.fromisoformat(node['last_seen'].replace('Z', '+00:00') if 'Z' in node['last_seen'] else node['last_seen'])
            time_diff = datetime.now() - last_seen
            node_data['status'] = 'online' if time_diff < timedelta(minutes=10) else 'offline'
            node_data['time_since_last_seen'] = str(time_diff).split('.')[0]  # Remove microseconds
            
            if node_data['status'] == 'online':
                online_count += 1
            else:
                offline_count += 1
        else:
            node_data['status'] = 'unknown'
            node_data['time_since_last_seen'] = 'Never'
        
        processed_nodes.append(node_data)
    
    return render_template('dashboard.html', 
                         nodes=processed_nodes, 
                         stats=stats,
                         online_count=online_count,
                         offline_count=offline_count)

@app.route('/node/<node_id>')
@login_required
def node_detail(node_id):
    """Detailed view for a specific node"""
    conn = get_db_connection()
    
    # Get node info
    node_info = conn.execute(
        'SELECT * FROM nodes WHERE node_id = ?', (node_id,)
    ).fetchone()
    
    if not node_info:
        return "Node not found", 404
    
    # Get recent status history (last 50 records) - updated for new fields
    history_query = '''
        SELECT * FROM node_status 
        WHERE node_id = ? 
        ORDER BY timestamp DESC 
        LIMIT 50
    '''
    history = conn.execute(history_query, (node_id,)).fetchall()
    
    conn.close()
    
    # Process history data
    processed_history = []
    for record in history:
        processed_record = {
            'timestamp': record['timestamp'],
            'received_at': record['received_at'],
            'gps': parse_json_field(record['gps_data']),
            'services': parse_json_field(record['services_data']),
            'storage': parse_json_field(record['storage_data']),
            'network': parse_json_field(record['network_data']),
            'users': parse_json_field(record['users_data']),
            'power': parse_json_field(record['power_data']),
            'system': parse_json_field(record['system_data']),
            'rns': parse_json_field(record['rns_data']),
            'client_config': parse_json_field(record['client_config_data'])
        }
        processed_history.append(processed_record)
    
    return render_template('node_detail.html', node=node_info, history=processed_history)

@app.route('/api/nodes')
@login_required
def api_nodes():
    """API endpoint for node data"""
    conn = get_db_connection()
    
    nodes_query = '''
        SELECT n.node_id, n.last_seen, n.created_at
        FROM nodes n
        ORDER BY n.last_seen DESC
    '''
    
    nodes = conn.execute(nodes_query).fetchall()
    conn.close()
    
    return jsonify([dict(node) for node in nodes])

def format_uptime(seconds):
    """Format uptime seconds into human readable string"""
    if not seconds:
        return "Unknown"
    
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"

@app.template_filter('fromjson')
def fromjson_filter(value):
    """Template filter to parse JSON"""
    return parse_json_field(value)

@app.template_filter('format_uptime')
def format_uptime_filter(seconds):
    """Template filter to format uptime"""
    return format_uptime(seconds)

@app.template_filter('format_bytes')
def format_bytes_filter(bytes_val):
    """Template filter to format bytes into human readable units"""
    if not bytes_val:
        return "0 B"

    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"

# Command Management Routes

@app.route('/node/<node_id>/commands')
@login_required
def node_commands(node_id):
    """View commands for a specific node"""
    conn = get_db_connection()

    # Get node info
    node_info = conn.execute(
        'SELECT * FROM nodes WHERE node_id = ?', (node_id,)
    ).fetchone()

    if not node_info:
        return "Node not found", 404

    # Get commands with results
    commands_query = '''
        SELECT c.*, cr.stdout, cr.stderr, cr.exit_code, cr.execution_time,
               cr.error, cr.received_at as result_received_at
        FROM commands c
        LEFT JOIN command_results cr ON c.id = cr.command_id
        WHERE c.node_id = ?
        ORDER BY c.created_at DESC
        LIMIT 100
    '''
    commands = conn.execute(commands_query, (node_id,)).fetchall()

    conn.close()

    return render_template('commands.html', node=node_info, commands=commands)

@app.route('/node/<node_id>/command/submit', methods=['POST'])
@login_required
def submit_command(node_id):
    """Submit a new command for execution"""
    command = request.form.get('command')
    timeout = request.form.get('timeout', 300)

    if not command:
        return jsonify({'success': False, 'error': 'No command provided'}), 400

    # Validate timeout
    try:
        timeout = int(timeout)
        if timeout < 1 or timeout > 3600:
            return jsonify({'success': False, 'error': 'Timeout must be between 1 and 3600 seconds'}), 400
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid timeout value'}), 400

    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            conn.isolation_level = 'DEFERRED'
            cursor = conn.cursor()

            # Create command in database
            cursor.execute('''
                INSERT INTO commands (node_id, command, status, timeout, created_by)
                VALUES (?, ?, 'pending', ?, 'web')
            ''', (node_id, command, timeout))

            command_id = cursor.lastrowid
            conn.commit()
            conn.close()

            # Note: The command will be picked up and sent by the server.py process
            # when it sees the pending command in the database
            return jsonify({
                'success': True,
                'command_id': command_id,
                'message': 'Command queued successfully. The server will send it to the client when it connects.'
            })

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                import time
                time.sleep(0.1 * (2 ** attempt))
                continue
            else:
                return jsonify({'success': False, 'error': f'Database error after {attempt + 1} attempts: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/node/<node_id>/commands/list')
@login_required
def node_commands_list(node_id):
    """Get commands list as JSON for AJAX updates"""
    conn = get_db_connection()

    commands_query = '''
        SELECT c.*, cr.stdout, cr.stderr, cr.exit_code, cr.execution_time,
               cr.error, cr.received_at as result_received_at
        FROM commands c
        LEFT JOIN command_results cr ON c.id = cr.command_id
        WHERE c.node_id = ?
        ORDER BY c.created_at DESC
        LIMIT 100
    '''
    commands = conn.execute(commands_query, (node_id,)).fetchall()
    conn.close()

    # Convert to list of dicts
    commands_list = [dict(cmd) for cmd in commands]

    return jsonify({'success': True, 'commands': commands_list})

@app.route('/api/node/<node_id>/commands')
@login_required
def api_commands(node_id):
    """API endpoint for command data"""
    conn = get_db_connection()

    commands_query = '''
        SELECT c.*, cr.stdout, cr.stderr, cr.exit_code, cr.execution_time,
               cr.error, cr.received_at as result_received_at
        FROM commands c
        LEFT JOIN command_results cr ON c.id = cr.command_id
        WHERE c.node_id = ?
        ORDER BY c.created_at DESC
        LIMIT 50
    '''
    commands = conn.execute(commands_query, (node_id,)).fetchall()

    conn.close()

    return jsonify([dict(cmd) for cmd in commands])

# File Transfer Routes

@app.route('/node/<node_id>/files')
@login_required
def node_files(node_id):
    """View file transfers for a specific node"""
    conn = get_db_connection()

    # Get node info
    node_info = conn.execute(
        'SELECT * FROM nodes WHERE node_id = ?', (node_id,)
    ).fetchone()

    if not node_info:
        return "Node not found", 404

    # Get file transfers
    transfers = conn.execute('''
        SELECT * FROM file_transfers
        WHERE node_id = ?
        ORDER BY created_at DESC
        LIMIT 100
    ''', (node_id,)).fetchall()

    conn.close()

    return render_template('files.html', node=node_info, transfers=transfers)

@app.route('/node/<node_id>/file/download', methods=['POST'])
@login_required
def request_file_download(node_id):
    """Request file download from client"""
    file_path = request.form.get('file_path')

    if not file_path:
        return jsonify({'success': False, 'error': 'No file path provided'}), 400

    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            conn.isolation_level = 'DEFERRED'
            cursor = conn.cursor()

            # Extract file name from path
            file_name = os.path.basename(file_path)

            # Create file transfer request
            cursor.execute('''
                INSERT INTO file_transfers (node_id, transfer_type, file_path, file_name, created_by)
                VALUES (?, 'download', ?, ?, 'web')
            ''', (node_id, file_path, file_name))

            transfer_id = cursor.lastrowid
            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'transfer_id': transfer_id,
                'message': 'Download request queued. The server will send it to the client when it connects.'
            })

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                import time
                time.sleep(0.1 * (2 ** attempt))
                continue
            else:
                return jsonify({'success': False, 'error': f'Database error after {attempt + 1} attempts: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/node/<node_id>/file/upload', methods=['POST'])
@login_required
def upload_file_to_client(node_id):
    """Upload file to client"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    destination_path = request.form.get('destination_path')

    if not destination_path:
        return jsonify({'success': False, 'error': 'No destination path provided'}), 400

    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Read file data
            file_data = file.read()
            file_name = file.filename

            conn = get_db_connection()
            conn.isolation_level = 'DEFERRED'
            cursor = conn.cursor()

            # Create file transfer record
            cursor.execute('''
                INSERT INTO file_transfers (node_id, transfer_type, file_path, file_name, file_size, created_by)
                VALUES (?, 'upload', ?, ?, ?, 'web')
            ''', (node_id, destination_path, file_name, len(file_data)))

            transfer_id = cursor.lastrowid

            # Store file data as chunks (1MB chunks)
            chunk_size = 1024 * 1024
            chunks = [file_data[i:i+chunk_size] for i in range(0, len(file_data), chunk_size)]

            for chunk_num, chunk_data in enumerate(chunks):
                cursor.execute('''
                    INSERT INTO file_chunks (transfer_id, chunk_number, chunk_data)
                    VALUES (?, ?, ?)
                ''', (transfer_id, chunk_num, chunk_data))

            # Update chunks_total
            cursor.execute('''
                UPDATE file_transfers
                SET chunks_total = ?
                WHERE id = ?
            ''', (len(chunks), transfer_id))

            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'transfer_id': transfer_id,
                'message': 'File upload queued. The server will send it to the client when it connects.'
            })

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                import time
                time.sleep(0.1 * (2 ** attempt))
                # Reset file pointer for retry
                file.seek(0)
                continue
            else:
                return jsonify({'success': False, 'error': f'Database error after {attempt + 1} attempts: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/file/retrieve/<int:transfer_id>')
@login_required
def retrieve_downloaded_file(transfer_id):
    """Retrieve a file that was downloaded from a client"""
    conn = get_db_connection()

    # Get transfer info
    transfer = conn.execute(
        'SELECT * FROM file_transfers WHERE id = ? AND transfer_type = "download" AND status = "completed"',
        (transfer_id,)
    ).fetchone()

    if not transfer:
        conn.close()
        return "Transfer not found or not completed", 404

    # Get file chunks
    chunks_query = '''
        SELECT chunk_data FROM file_chunks
        WHERE transfer_id = ?
        ORDER BY chunk_number ASC
    '''
    chunks = conn.execute(chunks_query, (transfer_id,)).fetchall()

    conn.close()

    if not chunks:
        return "No file data found", 404

    # Reassemble file
    file_data = b''
    for chunk in chunks:
        file_data += chunk['chunk_data']

    # Send file to user
    return send_file(
        io.BytesIO(file_data),
        as_attachment=True,
        download_name=transfer['file_name'],
        mimetype='application/octet-stream'
    )

@app.route('/api/node/<node_id>/transfers')
@login_required
def api_transfers(node_id):
    """API endpoint for file transfer data"""
    conn = get_db_connection()

    transfers = conn.execute('''
        SELECT * FROM file_transfers
        WHERE node_id = ?
        ORDER BY created_at DESC
        LIMIT 50
    ''', (node_id,)).fetchall()

    conn.close()

    return jsonify([dict(transfer) for transfer in transfers])

# Logs Routes

@app.route('/node/<node_id>/logs')
@login_required
def node_logs(node_id):
    """View logs for a specific node"""
    conn = get_db_connection()

    # Get node info
    node_info = conn.execute(
        'SELECT * FROM nodes WHERE node_id = ?', (node_id,)
    ).fetchone()

    if not node_info:
        return "Node not found", 404

    # Get logs
    log_type = request.args.get('type', None)
    level = request.args.get('level', None)

    query = 'SELECT * FROM logs WHERE node_id = ?'
    params = [node_id]

    if log_type:
        query += ' AND log_type = ?'
        params.append(log_type)

    if level:
        query += ' AND level = ?'
        params.append(level)

    query += ' ORDER BY created_at DESC LIMIT 200'

    logs = conn.execute(query, params).fetchall()

    conn.close()

    return render_template('logs.html', node=node_info, logs=logs)

@app.route('/api/node/<node_id>/logs')
@login_required
def api_logs(node_id):
    """API endpoint for log data"""
    conn = get_db_connection()

    log_type = request.args.get('type', None)
    level = request.args.get('level', None)

    query = 'SELECT * FROM logs WHERE node_id = ?'
    params = [node_id]

    if log_type:
        query += ' AND log_type = ?'
        params.append(log_type)

    if level:
        query += ' AND level = ?'
        params.append(level)

    query += ' ORDER BY created_at DESC LIMIT 100'

    logs = conn.execute(query, params).fetchall()

    conn.close()

    return jsonify([dict(log) for log in logs])

# Proxy Management Routes

@app.route('/node/<node_id>/proxy')
@login_required
def node_proxy(node_id):
    """View and manage proxy settings for a specific node"""
    conn = get_db_connection()

    # Get node info
    node_info = conn.execute(
        'SELECT * FROM nodes WHERE node_id = ?', (node_id,)
    ).fetchone()

    if not node_info:
        return "Node not found", 404

    # Get proxy configuration from commands/status
    proxy_status_query = '''
        SELECT c.*, cr.stdout, cr.stderr, cr.exit_code
        FROM commands c
        LEFT JOIN command_results cr ON c.id = cr.command_id
        WHERE c.node_id = ? AND c.command LIKE 'proxy:%'
        ORDER BY c.created_at DESC
        LIMIT 1
    '''
    proxy_cmd = conn.execute(proxy_status_query, (node_id,)).fetchone()

    conn.close()

    # Parse proxy status from last command result if available
    proxy_enabled = None
    proxy_port = None
    if proxy_cmd and proxy_cmd['stdout']:
        try:
            status_data = json.loads(proxy_cmd['stdout'])
            proxy_enabled = status_data.get('enabled', False)
            proxy_port = status_data.get('port', 8080)
        except:
            pass

    return render_template('proxy.html',
                         node=node_info,
                         proxy_enabled=proxy_enabled,
                         proxy_port=proxy_port)

@app.route('/node/<node_id>/proxy/status', methods=['POST'])
@login_required
def get_proxy_status(node_id):
    """Request current proxy status from client"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            conn.isolation_level = 'DEFERRED'
            cursor = conn.cursor()

            # Create a special command to get proxy status
            cursor.execute('''
                INSERT INTO commands (node_id, command, status, created_by)
                VALUES (?, 'proxy:status', 'pending', 'web')
            ''', (node_id,))

            command_id = cursor.lastrowid
            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'command_id': command_id,
                'message': 'Proxy status request sent'
            })

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                import time
                time.sleep(0.1 * (2 ** attempt))
                continue
            else:
                return jsonify({'success': False, 'error': f'Database error after {attempt + 1} attempts: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/node/<node_id>/proxy/enable', methods=['POST'])
@login_required
def enable_proxy(node_id):
    """Enable proxy on client"""
    port = request.form.get('port', '8080')

    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            conn.isolation_level = 'DEFERRED'
            cursor = conn.cursor()

            # Create command to enable proxy
            cursor.execute('''
                INSERT INTO commands (node_id, command, status, created_by)
                VALUES (?, ?, 'pending', 'web')
            ''', (node_id, f'proxy:enable:{port}'))

            command_id = cursor.lastrowid
            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'command_id': command_id,
                'message': f'Proxy enable command sent (port {port})'
            })

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                import time
                time.sleep(0.1 * (2 ** attempt))
                continue
            else:
                return jsonify({'success': False, 'error': f'Database error after {attempt + 1} attempts: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/node/<node_id>/proxy/disable', methods=['POST'])
@login_required
def disable_proxy(node_id):
    """Disable proxy on client"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            conn.isolation_level = 'DEFERRED'
            cursor = conn.cursor()

            # Create command to disable proxy
            cursor.execute('''
                INSERT INTO commands (node_id, command, status, created_by)
                VALUES (?, 'proxy:disable', 'pending', 'web')
            ''', (node_id,))

            command_id = cursor.lastrowid
            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'command_id': command_id,
                'message': 'Proxy disable command sent'
            })

        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                import time
                time.sleep(0.1 * (2 ** attempt))
                continue
            else:
                return jsonify({'success': False, 'error': f'Database error after {attempt + 1} attempts: {str(e)}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    # Check if database exists
    if not os.path.exists(DATABASE_PATH):
        print(f"Warning: Database file {DATABASE_PATH} not found!")
        print("Make sure your server.py has created the database.")

    # Initialize authentication database
    init_auth_db()
    print("Authentication initialized. Default credentials: admin / crebain")

    app.run(debug=True, host='0.0.0.0', port=5000)

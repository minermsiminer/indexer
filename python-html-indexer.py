import os
import base64
import re
import subprocess
import socket
import socketserver
import http.server
import shutil
import tempfile
import urllib.request
import json
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
import threading
import queue
import time
import signal
import atexit
from datetime import datetime
from database import db
from llm_processor import LLMProcessor

# --- Configuration ---
app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Needed for flashing messages
THUMBNAILS_FOLDER = Path("static/thumbnails")
SCREENSHOT_WIDTH = 1024
SCREENSHOT_HEIGHT = 768

# Ensure the thumbnails directory exists
THUMBNAILS_FOLDER.mkdir(exist_ok=True)

# Global variables for progressive processing
processing_queue = queue.Queue()
processing_results = {}
processing_files = []
processing_completed = 0
processing_total = 0

# Global variables for scanning progress
scanning_queue = queue.Queue()
scanning_results = {}
scanning_files = []
scanning_completed = 0
scanning_total = 0
scanning_current_phase = ""  # "finding_python", "finding_html", "saving_database"

# Global variables for running Python apps
current_python_app = None  # {'process': p, 'html_path': path, 'url': url}

# Global registry for running HTML servers
running_html_servers = {}  # {html_path: {'server': httpd, 'port': port, 'thread': thread}}

# GitHub repositories directory
REPOS_FOLDER = Path("repos")
REPOS_FOLDER.mkdir(exist_ok=True)

# --- GitHub Repository Functions ---

def parse_github_url(url):
    """
    Parse a GitHub URL and extract owner and repo name.
    Supports formats:
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - git@github.com:owner/repo.git
    - owner/repo (shorthand)
    Returns tuple (owner, repo) or None if invalid.
    """
    url = url.strip()
    
    # Handle shorthand format: owner/repo
    if '/' in url and not url.startswith(('http', 'git@')):
        parts = url.split('/')
        if len(parts) == 2:
            return (parts[0], parts[1].replace('.git', ''))
    
    # Handle SSH format: git@github.com:owner/repo.git
    if url.startswith('git@github.com:'):
        path = url.replace('git@github.com:', '').replace('.git', '')
        parts = path.split('/')
        if len(parts) >= 2:
            return (parts[0], parts[1])
    
    # Handle HTTPS format: https://github.com/owner/repo
    if 'github.com' in url:
        # Remove .git extension if present
        url = url.replace('.git', '')
        # Extract path after github.com
        if 'github.com/' in url:
            path = url.split('github.com/')[1]
            parts = path.split('/')
            if len(parts) >= 2:
                return (parts[0], parts[1])
    
    return None


def check_github_repo_has_target_files(owner, repo):
    """
    Check if a GitHub repository contains Python or HTML files.
    Uses GitHub API to check the repository tree.
    Returns dict with 'has_python', 'has_html', and 'error' keys.
    """
    result = {'has_python': False, 'has_html': False, 'error': None, 'file_count': {'python': 0, 'html': 0}}
    
    try:
        # Use GitHub API to get repository contents (recursive tree)
        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
        
        req = urllib.request.Request(api_url)
        req.add_header('Accept', 'application/vnd.github.v3+json')
        req.add_header('User-Agent', 'Python-HTML-Indexer')
        
        # Check for GitHub token in environment for higher rate limits
        github_token = os.environ.get('GITHUB_TOKEN')
        if github_token:
            req.add_header('Authorization', f'token {github_token}')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        # Check tree for .py and .html files
        if 'tree' in data:
            for item in data['tree']:
                if item['type'] == 'blob':  # Only files, not directories
                    path = item['path'].lower()
                    # Skip common non-relevant directories
                    if any(skip in path for skip in ['.venv/', 'venv/', 'node_modules/', 'site-packages/', '__pycache__/']):
                        continue
                    
                    if path.endswith('.py'):
                        result['has_python'] = True
                        result['file_count']['python'] += 1
                    elif path.endswith('.html'):
                        result['has_html'] = True
                        result['file_count']['html'] += 1
        
        return result
        
    except urllib.error.HTTPError as e:
        if e.code == 404:
            result['error'] = f"Repository '{owner}/{repo}' not found or is private"
        elif e.code == 403:
            result['error'] = "GitHub API rate limit exceeded. Try setting GITHUB_TOKEN environment variable."
        else:
            result['error'] = f"GitHub API error: {e.code} - {e.reason}"
        return result
    except urllib.error.URLError as e:
        result['error'] = f"Network error: {str(e)}"
        return result
    except Exception as e:
        result['error'] = f"Error checking repository: {str(e)}"
        return result


def clone_github_repo(owner, repo, target_dir=None):
    """
    Clone a GitHub repository to the repos folder.
    Returns dict with 'success', 'path', and 'error' keys.
    """
    result = {'success': False, 'path': None, 'error': None}
    
    if target_dir is None:
        target_dir = REPOS_FOLDER / f"{owner}_{repo}"
    else:
        target_dir = Path(target_dir)
    
    # Check if repo already exists
    if target_dir.exists():
        # Try to update existing repo
        try:
            print(f"Repository already exists at {target_dir}, pulling latest changes...")
            subprocess.run(
                ['git', 'pull'],
                cwd=str(target_dir),
                check=True,
                capture_output=True,
                timeout=120
            )
            result['success'] = True
            result['path'] = str(target_dir)
            result['message'] = 'Repository updated with latest changes'
            return result
        except subprocess.CalledProcessError as e:
            print(f"Failed to pull, will re-clone: {e}")
            # Remove existing directory and clone fresh
            shutil.rmtree(target_dir)
        except subprocess.TimeoutExpired:
            result['error'] = "Git pull timed out"
            return result
    
    # Clone the repository
    clone_url = f"https://github.com/{owner}/{repo}.git"
    
    try:
        print(f"Cloning repository: {clone_url}")
        subprocess.run(
            ['git', 'clone', '--depth', '1', clone_url, str(target_dir)],
            check=True,
            capture_output=True,
            timeout=300  # 5 minute timeout for large repos
        )
        result['success'] = True
        result['path'] = str(target_dir)
        result['message'] = 'Repository cloned successfully'
        return result
        
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode('utf-8') if e.stderr else ''
        if 'Repository not found' in stderr:
            result['error'] = f"Repository '{owner}/{repo}' not found or is private"
        else:
            result['error'] = f"Git clone failed: {stderr}"
        return result
    except subprocess.TimeoutExpired:
        result['error'] = "Git clone timed out (repository may be too large)"
        # Clean up partial clone
        if target_dir.exists():
            shutil.rmtree(target_dir)
        return result
    except FileNotFoundError:
        result['error'] = "Git is not installed or not in PATH"
        return result
    except Exception as e:
        result['error'] = f"Error cloning repository: {str(e)}"
        return result


def get_cloned_repos():
    """Get list of already cloned repositories."""
    repos = []
    if REPOS_FOLDER.exists():
        for repo_dir in REPOS_FOLDER.iterdir():
            if repo_dir.is_dir() and (repo_dir / '.git').exists():
                repos.append({
                    'name': repo_dir.name,
                    'path': str(repo_dir),
                    'indexed': db_has_items_from_path(str(repo_dir))
                })
    return repos


def db_has_items_from_path(folder_path):
    """Check if database has any indexed items from a folder path."""
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM indexed_items WHERE folder_path LIKE ?', (f'{folder_path}%',))
            count = cursor.fetchone()[0]
            return count > 0
    except:
        return False

def _get_file_size(file_path):
    """Get file size in bytes."""
    try:
        return Path(file_path).stat().st_size
    except:
        return 0

def _get_file_modified(file_path):
    """Get file last modified timestamp."""
    try:
        from datetime import datetime
        mtime = Path(file_path).stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat()
    except:
        return None

def _extract_dependencies(script_path):
    """Extract key dependencies from Python file."""
    try:
        with open(script_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        imports = []
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('import ') or line.startswith('from '):
                # Extract module name
                if line.startswith('import '):
                    module = line.split()[1].split('.')[0]
                else:
                    module = line.split()[1]
                if module not in ['os', 'sys', 'json', 'time', 'datetime', 'pathlib']:
                    imports.append(module)

        return ', '.join(set(imports)) if imports else None
    except:
        return None

def _parse_requirements():
    """Parse requirements.txt to get list of available packages."""
    try:
        requirements_path = Path('requirements.txt')
        if not requirements_path.exists():
            return set()

        packages = set()
        with open(requirements_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Extract package name (before any version specifiers)
                    package = line.split()[0].split('==')[0].split('>=')[0].split('<=')[0].split('>')[0].split('<')[0]
                    packages.add(package.lower())
        return packages
    except:
        return set()

def _check_dependencies(dependencies_str):
    """Check if dependencies are available in requirements.txt."""
    if not dependencies_str:
        return True  # No dependencies means no missing ones

    available_packages = _parse_requirements()
    if not available_packages:
        return False  # No requirements.txt means we can't verify

    dependencies = [dep.strip().lower() for dep in dependencies_str.split(',')]
    missing = [dep for dep in dependencies if dep not in available_packages]

    return len(missing) == 0  # True if no missing dependencies

def is_valid_web_app(py_file_path):
    """
    Check if Python file is a complete web application.
    Returns app info dict if valid, None if not.
    """
    try:
        with open(py_file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as e:
        print(f"Could not read Python file {py_file_path}: {e}")
        return None

    # Must contain app.run() call (Flask/Django pattern)
    if not re.search(r'app\.run\(', content):
        return None

    # Should not be just a utility script (has main function but no if __name__ guard)
    if 'def main():' in content and 'if __name__ == "__main__":' not in content:
        return None

    # Extract port from app.run(port=XXXX)
    port_match = re.search(r'app\.run\(.*?port\s*=\s*(\d+)', content)
    if port_match:
        port = int(port_match.group(1))
    else:
        # Check for PORT environment variable usage
        if 'os.environ.get(\'PORT\'' in content or 'PORT' in content:
            port = 5000  # Default Flask port
        else:
            port = 5000

    # Determine app type
    if 'from flask' in content or 'import flask' in content:
        app_type = 'flask'
    elif 'from django' in content or 'import django' in content:
        app_type = 'django'
    else:
        app_type = 'unknown'

    return {
        'script_path': str(py_file_path),
        'port': port,
        'type': app_type,
        'name': py_file_path.stem.replace('_', ' ').title()
    }

def find_free_port():
    """Find a free port to use for local HTTP server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def stop_html_server(html_path):
    """Stop a running HTML server for the given path."""
    html_path_str = str(html_path)
    if html_path_str in running_html_servers:
        server_info = running_html_servers[html_path_str]
        try:
            server_info['server'].shutdown()
            server_info['server'].server_close()
            print(f"Stopped HTML server for {html_path_str}")
        except Exception as e:
            print(f"Error stopping HTML server for {html_path_str}: {e}")
        # Remove from registry even if shutdown fails
        if html_path_str in running_html_servers:
            del running_html_servers[html_path_str]

def get_or_start_html_server(html_path):
    """Get existing server for HTML file or start a new one."""
    html_path_str = str(html_path)

    # Check if server is already running
    if html_path_str in running_html_servers:
        server_info = running_html_servers[html_path_str]
        print(f"Reusing existing HTML server for {html_path_str} on port {server_info['port']}")
        return server_info['port']

    # Start new server
    port = find_free_port()
    server_thread = threading.Thread(target=serve_html_with_http_server, args=(html_path, port))
    server_thread.daemon = True
    server_thread.start()

    # Wait a moment for server to start
    time.sleep(1)

    return port

def start_html_server(html_path, port):
    """Start HTML server and return server info."""
    html_dir = html_path.parent
    html_path_str = str(html_path)

    class CustomHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, directory=None, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def translate_path(self, path):
            # Translate URL path to file system path
            path = super().translate_path(path)
            # If requesting root, serve the specific HTML file
            if path.endswith('/') or path == os.path.join(self.directory, ''):
                return str(html_path)
            return path

        def log_message(self, format, *args):
            # Suppress server logs
            pass

    # Change to the HTML file's directory
    original_cwd = os.getcwd()
    try:
        os.chdir(html_dir)
        httpd = socketserver.TCPServer(("", port), lambda *args, **kwargs: CustomHTTPRequestHandler(*args, directory=str(html_dir), **kwargs))
        print(f"Serving HTML at http://localhost:{port}")

        # Store server info in global registry
        running_html_servers[html_path_str] = {
            'server': httpd,
            'port': port,
            'thread': threading.current_thread()
        }

        return httpd

    except Exception as e:
        print(f"Error starting HTML server: {e}")
        os.chdir(original_cwd)
        raise
    finally:
        os.chdir(original_cwd)

def serve_html_with_http_server(html_path, port):
    """Serve HTML file using Python's built-in HTTP server with asset support."""
    try:
        httpd = start_html_server(html_path, port)
        httpd.serve_forever()
    except Exception as e:
        print(f"Error serving HTML: {e}")
    finally:
        html_path_str = str(html_path)
        # Remove from registry when server stops
        if html_path_str in running_html_servers:
            del running_html_servers[html_path_str]

def find_html_interface(app_dir):
    """
    Find HTML interface for a Python web app.
    Returns Path to HTML file if found, None otherwise.
    """
    app_dir = Path(app_dir)

    # Common HTML interface locations (in order of preference)
    candidates = [
        app_dir / 'index.html',
        app_dir / 'templates' / 'index.html',
        app_dir / 'static' / 'index.html',
        app_dir / 'public' / 'index.html',
        app_dir / 'frontend' / 'index.html'
    ]

    # First, try to find any HTML file in the app directory
    html_files = list(app_dir.rglob('*.html'))
    if html_files:
        # Return the first HTML file found (could be index.html or any other)
        return html_files[0]

    # Fallback to specific candidates if no HTML files found with rglob
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    return None

def find_python_apps(target_folder):
    """
    Find all valid Python web applications in the target folder.
    Returns list of app info dicts.
    """
    python_apps = []

    # Find all .py files
    for py_file in target_folder.rglob('*.py'):
        # Skip .venv, site-packages, and node_modules
        if '.venv' in str(py_file) or 'site-packages' in str(py_file) or 'node_modules' in str(py_file):
            continue
        app_info = is_valid_web_app(py_file)
        if app_info:
            # Find HTML interface
            html_interface = find_html_interface(py_file.parent)
            if html_interface:
                app_info['html_interface'] = str(html_interface)
                app_info['app_type'] = 'python_app'
                python_apps.append(app_info)
            else:
                print(f"No HTML interface found for Python app: {py_file}")

    return python_apps

def find_standalone_html(target_folder, python_apps):
    """
    Find HTML files that are not interfaces for Python apps.
    Returns list of standalone HTML file info dicts.
    """
    # Get all HTML files
    all_html = list(target_folder.rglob('*.html'))

    # Get HTML files that are already paired with Python apps
    paired_html = {app['html_interface'] for app in python_apps}

    # Filter out paired HTML files
    standalone_html = []
    for html_file in all_html:
        if str(html_file) not in paired_html:
            # Skip .venv, site-packages, and node_modules
            if '.venv' in str(html_file) or 'site-packages' in str(html_file) or 'node_modules' in str(html_file):
                continue
            standalone_html.append({
                'html_file': str(html_file),
                'name': html_file.stem.replace('_', ' ').title(),
                'app_type': 'standalone_html'
            })

    return standalone_html

def detect_python_backend(html_content):
    """
    LEGACY: Analyze HTML content to detect if it requires a Python backend.
    This is kept for backward compatibility but will be phased out.
    """
    # Convert to lowercase for case-insensitive matching
    content_lower = html_content.lower()

    # Check for Flask template syntax {{ }}
    if '{{' in html_content and '}}' in html_content:
        return True

    # Check for form actions with local routes
    form_pattern = r'<form[^>]*action=["\'](?:/api/|/submit|/process|/handle|/endpoint)'
    if re.search(form_pattern, content_lower):
        return True

    # Check for JavaScript fetch calls to localhost endpoints
    fetch_patterns = [
        r'fetch\s*\(\s*["\'](?:http://localhost:\d+|/api/|/submit|/process|/handle|/endpoint)',
        r'\.post\s*\(\s*["\'](?:/api/|/submit|/process|/handle|/endpoint)',
        r'\.get\s*\(\s*["\'](?:/api/|/submit|/process|/handle|/endpoint)',
        r'xmlhttprequest.*open.*(?:/api/|/submit|/process|/handle|/endpoint)',
        r'axios\.(?:post|get|put|delete)\s*\(\s*["\'](?:/api/|/submit|/process|/handle|/endpoint)'
    ]

    for pattern in fetch_patterns:
        if re.search(pattern, content_lower):
            return True

    # Check for AJAX calls to Python routes
    ajax_patterns = [
        r'\$\.ajax\s*\(\s*\{[^}]*url\s*:\s*["\'](?:/api/|/submit|/process|/handle|/endpoint)',
        r'\$\.post\s*\(\s*["\'](?:/api/|/submit|/process|/handle|/endpoint)',
        r'\$\.get\s*\(\s*["\'](?:/api/|/submit|/process|/handle|/endpoint)'
    ]

    for pattern in ajax_patterns:
        if re.search(pattern, content_lower):
            return True

    return False

def get_existing_thumbnails():
    """Get indexed applications from database."""
    indexed_items = []

    db_items = db.get_all_items()

    for item in db_items:
        # Build thumbnail URL from stored path
        thumb_url = '/static/thumbnails/placeholder.png'  # Default fallback

        if item['thumbnail_path']:
            thumb_path = Path(item['thumbnail_path'])
            if thumb_path.exists():
                thumb_filename = thumb_path.name
                thumb_url = f'/static/thumbnails/{thumb_filename}'
            else:
                print(f"Warning: Thumbnail file missing: {thumb_path}")
                # Mark for regeneration by clearing the path
                with db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('UPDATE indexed_items SET thumbnail_path = NULL WHERE id = ?', (item['id'],))
                    conn.commit()

        base_item = {
            'thumb_url': thumb_url,
            'name': item['name'],
            'app_type': item['item_type'],
            'port': item['port'] or 5000,
            'id': item['id'],
            'simple_id': item.get('simple_id', ''),
            'description': item.get('description', ''),
            'short_desc': item.get('short_desc', ''),
            'tech_stack': item.get('tech_stack', ''),
            'tags': item.get('tags', ''),
            'category': item.get('category', ''),
            'file_size': item.get('file_size', 0),
            'created_at': item.get('created_at', ''),
            'last_modified': item.get('last_modified', ''),
            'dependencies': item.get('dependencies', ''),
            'llm_processed': item.get('llm_processed', False),
            'folder_path': item.get('folder_path', ''),
            'dependencies_ok': True  # Default to True
        }

        # Check dependencies for Python apps
        if item['item_type'] == 'python_app':
            base_item['dependencies_ok'] = _check_dependencies(item.get('dependencies', ''))

        # Use simple_id for URLs
        if item.get('simple_id'):
            base_item['html_file_url'] = f'/serve/{item["simple_id"]}'
            if item['item_type'] == 'python_app':
                base_item['python_script'] = item['main_file_path']
                if item['html_interface_path']:
                    base_item['html_interface'] = item['html_interface_path']
            elif item['item_type'] == 'standalone_html':
                base_item['html_file'] = item['main_file_path']
        else:
            # Fallback for items without simple_id (legacy)
            if item['item_type'] == 'python_app':
                folder_name = Path(item['folder_path']).name
                clean_name = f"{folder_name}-{item['name']}".replace(' ', '-').replace('_', '-').lower()
                base_item['html_file_url'] = f'/{clean_name}.html'
                base_item['python_script'] = item['main_file_path']
                if item['html_interface_path']:
                    base_item['html_interface'] = item['html_interface_path']
            elif item['item_type'] == 'standalone_html':
                base_item['html_file_url'] = item["main_file_path"]
                base_item['html_file'] = item['main_file_path']

        indexed_items.append(base_item)

    return sorted(indexed_items, key=lambda x: x['name'])

def smart_screenshot_worker():
    """Smart background worker to generate screenshots for both Python apps and HTML files."""
    global processing_completed

    # Selenium setup
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument(f"--window-size={SCREENSHOT_WIDTH},{SCREENSHOT_HEIGHT}")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--allow-running-insecure-content")

    driver = None
    try:
        service = ChromeService()
        driver = webdriver.Chrome(service=service, options=chrome_options)
        print("[WORKER_DEBUG] Selenium WebDriver initialized successfully")

        while True:
            try:
                item = processing_queue.get(timeout=1)
                if item is None:  # Shutdown signal
                    break

                item_name = item.get('name', 'Unknown')
                item_type = item.get('app_type', 'Unknown')
                print(f"[WORKER_DEBUG] Processing item: {item_name} (type: {item_type})")

                item_start_time = time.time()
                current_item = {'name': item_name, 'type': item_type, 'start_time': item_start_time}

                try:
                    if item['app_type'] == 'python_app':
                        # Handle Python app: start it temporarily and screenshot its HTML interface
                        print(f"[WORKER_DEBUG] Starting Python app processing for: {item_name}")
                        screenshot_python_app(driver, item)
                    elif item['app_type'] == 'standalone_html':
                        # Handle standalone HTML: screenshot directly
                        print(f"[WORKER_DEBUG] Starting HTML file processing for: {item_name}")
                        screenshot_html_file(driver, item)
                    else:
                        print(f"[WORKER_DEBUG] Unknown app type: {item['app_type']}")
                        processing_results[str(item)] = {'success': False, 'error': f"Unknown app type: {item['app_type']}"}

                    item_duration = time.time() - item_start_time
                    processing_results[str(item)] = {'success': True, 'error': None, 'duration': item_duration}
                    print(f"[WORKER_DEBUG] Successfully processed: {item_name} in {item_duration:.2f}s")

                except Exception as e:
                    item_duration = time.time() - item_start_time
                    error_msg = f"Error processing {item_name}: {str(e)}"
                    print(f"[WORKER_DEBUG] {error_msg} (took {item_duration:.2f}s)")
                    processing_results[str(item)] = {'success': False, 'error': str(e), 'duration': item_duration}

                processing_completed += 1
                print(f"[WORKER_DEBUG] Progress: {processing_completed} / {processing_total} completed")
                processing_queue.task_done()

            except queue.Empty:
                continue

    except Exception as e:
        print(f"[WORKER_DEBUG] Worker initialization error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()
            print("[WORKER_DEBUG] Selenium WebDriver closed")

def screenshot_python_app(driver, app_info):
    """Generate screenshot for a Python web application."""
    script_path = Path(app_info['script_path'])
    port = app_info['port']
    html_interface = app_info.get('html_interface')

    print(f"Starting screenshot process for Python app: {app_info.get('name', 'Unknown')}")
    print(f"Script path: {script_path}")
    print(f"Port: {port}")
    print(f"HTML interface: {html_interface}")

    if not html_interface:
        raise Exception("No HTML interface found for Python app")

    if not script_path.exists():
        raise Exception(f"Python script does not exist: {script_path}")

    # Ensure apps-debris directory exists
    apps_debris_dir = Path('apps-debris')
    apps_debris_dir.mkdir(exist_ok=True)

    # Start the Python app temporarily
    print(f"Starting Python app process: {script_path}")
    env = {
        'PORT': str(port),
        'FLASK_DEBUG': '0',
        'PATH': os.environ.get('PATH', ''),
        'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
        'HOME': os.environ.get('HOME', ''),
        'USER': os.environ.get('USER', ''),
    }

    def preexec():
        os.closerange(3, 1024)

    process = subprocess.Popen(['python', str(script_path)], env=env, preexec_fn=preexec, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=apps_debris_dir)
    print(f"Process started with PID: {process.pid}")

    try:
        # Wait for app to start
        print("Waiting for app to start (8 seconds)...")
        time.sleep(8)

        # Check if process is still running
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            error_msg = f"Python app failed to start. Exit code: {process.returncode}\n"
            error_msg += f"STDOUT: {stdout.decode('utf-8', errors='ignore')}\n"
            error_msg += f"STDERR: {stderr.decode('utf-8', errors='ignore')}"
            raise Exception(error_msg)

        # Take screenshot of the HTML interface
        url = f'http://localhost:{port}'
        print(f"Navigating to URL: {url}")
        driver.get(url)

        # Wait for page to load and check if page loaded successfully
        time.sleep(3)
        page_title = driver.title
        print(f"Page title: {page_title}")

        if "localhost" not in driver.current_url:
            print(f"Warning: Current URL is {driver.current_url}, expected localhost")

        # Generate short thumbnail filename using simple_id (5 digits for thousands)
        simple_id = app_info.get('simple_id')
        if not simple_id:
            # Retrieve simple_id from database if not available
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT simple_id FROM indexed_items WHERE main_file_path = ?', (app_info['script_path'],))
                row = cursor.fetchone()
                if row:
                    simple_id = row[0]
                else:
                    simple_id = 'unknown'

        print(f"[DEBUG] screenshot_python_app: app_name='{app_info.get('name', 'Unknown')}', simple_id='{simple_id}'")
        if simple_id.startswith(('p', 'h')) and len(simple_id) >= 4:
            # Convert p001 to p00001, h001 to h00001, etc.
            prefix = simple_id[0]
            number = int(simple_id[1:])
            padded_id = f"{prefix}{number:05d}"
        else:
            padded_id = simple_id
        screenshot_path = THUMBNAILS_FOLDER / f"{padded_id}.png"

        print(f"Saving screenshot to: {screenshot_path}")
        driver.save_screenshot(str(screenshot_path))

        if screenshot_path.exists():
            print(f"Screenshot saved successfully: {screenshot_path} ({screenshot_path.stat().st_size} bytes)")
        else:
            raise Exception(f"Screenshot file was not created: {screenshot_path}")

        # Update database with thumbnail path
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM indexed_items WHERE main_file_path = ?', (app_info['script_path'],))
            row = cursor.fetchone()
            if row:
                item_id = row[0]
                cursor.execute('UPDATE indexed_items SET thumbnail_path = ? WHERE id = ?', (str(screenshot_path), item_id))
                conn.commit()
                print(f"Updated DB for Python app: {app_info['script_path']} (ID: {item_id})")
            else:
                print(f"Could not find DB entry for: {app_info['script_path']}")

    except Exception as e:
        print(f"Error in screenshot_python_app: {str(e)}")
        raise
    finally:
        # Clean up: terminate the Python app
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
                print(f"Terminated Python app: {script_path}")
            else:
                print(f"Python app already stopped (exit code: {process.returncode})")
        except subprocess.TimeoutExpired:
            process.kill()
            print(f"Killed Python app: {script_path}")
        except Exception as cleanup_error:
            print(f"Error during cleanup: {cleanup_error}")

def screenshot_html_file(driver, html_info):
    """Generate screenshot for a standalone HTML file."""
    html_path = Path(html_info['html_file'])

    print(f"Starting screenshot process for HTML file: {html_info.get('name', 'Unknown')}")
    print(f"HTML path: {html_path}")

    if not html_path.exists():
        raise Exception(f"HTML file does not exist: {html_path}")

    # Take screenshot directly
    file_uri = html_path.as_uri()
    print(f"Navigating to file URI: {file_uri}")
    driver.get(file_uri)

    # Wait for page to load and check if page loaded successfully
    time.sleep(2)
    page_title = driver.title
    print(f"Page title: {page_title}")

    # Check for basic page load indicators
    try:
        body_text = driver.find_element(By.TAG_NAME, 'body').text[:100]
        print(f"Page content preview: {body_text}...")
    except Exception as e:
        print(f"Warning: Could not get page content: {e}")
    # Generate short thumbnail filename using simple_id (5 digits for thousands)
    simple_id = html_info.get('simple_id')
    if not simple_id:
        # Retrieve simple_id from database if not available
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT simple_id FROM indexed_items WHERE main_file_path = ?', (html_info['html_file'],))
            row = cursor.fetchone()
            if row:
                simple_id = row[0]
            else:
                simple_id = 'unknown'

    print(f"[DEBUG] screenshot_html_file: html_name='{html_info.get('name', 'Unknown')}', simple_id='{simple_id}'")
    if simple_id.startswith(('p', 'h')) and len(simple_id) >= 4:
        # Convert p001 to p00001, h001 to h00001, etc.
        prefix = simple_id[0]
        number = int(simple_id[1:])
        padded_id = f"{prefix}{number:05d}"
    else:
        padded_id = simple_id
    screenshot_path = THUMBNAILS_FOLDER / f"{padded_id}.png"

    print(f"Saving screenshot to: {screenshot_path}")
    driver.save_screenshot(str(screenshot_path))

    if screenshot_path.exists():
        print(f"Screenshot saved successfully: {screenshot_path} ({screenshot_path.stat().st_size} bytes)")
    else:
        raise Exception(f"Screenshot file was not created: {screenshot_path}")

    # Update database with thumbnail path
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM indexed_items WHERE main_file_path = ?', (html_info['html_file'],))
        row = cursor.fetchone()
        if row:
            item_id = row[0]
            cursor.execute('UPDATE indexed_items SET thumbnail_path = ? WHERE id = ?', (str(screenshot_path), item_id))
            conn.commit()
            print(f"Updated DB for HTML file: {html_info['html_file']} (ID: {item_id})")
        else:
            print(f"Could not find DB entry for: {html_info['html_file']}")

def scanning_worker():
    """Background worker to perform scanning operations with progress updates."""
    global scanning_completed, scanning_current_phase, scanning_total

    while True:
        try:
            scan_task = scanning_queue.get(timeout=1)
            if scan_task is None:  # Shutdown signal
                break

            task_type = scan_task.get('type')
            target_folder = scan_task.get('target_folder')

            print(f"[SCAN_WORKER_DEBUG] Processing task: {task_type}")
            print(f"[SCAN_WORKER_DEBUG] Current scanning_total: {scanning_total}, scanning_completed: {scanning_completed}, phase: {scanning_current_phase}")

            try:
                if task_type == 'find_python_apps':
                    scanning_current_phase = "finding_python"
                    print(f"[SCAN_WORKER] Finding Python web applications in: {target_folder}")
                    python_apps = find_python_apps(target_folder)
                    scanning_results['python_apps'] = python_apps
                    print(f"[SCAN_WORKER] Found {len(python_apps)} Python web applications")

                elif task_type == 'find_html_files':
                    scanning_current_phase = "finding_html"
                    print(f"[SCAN_WORKER] Finding standalone HTML files in: {target_folder}")
                    python_apps = scanning_results.get('python_apps', [])
                    html_files = find_standalone_html(target_folder, python_apps)
                    scanning_results['html_files'] = html_files
                    print(f"[SCAN_WORKER] Found {len(html_files)} standalone HTML files")

                    # Calculate total once at the beginning after finding all items
                    scanning_total = len(python_apps) + len(html_files)
                    print(f"[SCAN_WORKER_DEBUG] Total items to process: {scanning_total}")

                elif task_type == 'save_to_database':
                    scanning_current_phase = "saving_database"
                    print(f"[SCAN_WORKER] Saving items to database")
                    python_apps = scanning_results.get('python_apps', [])
                    html_files = scanning_results.get('html_files', [])
                    all_items = python_apps + html_files

                    # Total is already calculated from finding phases and remains consistent
                    print(f"[SCAN_WORKER_DEBUG] In save_to_database - resetting scanning_total to {scanning_total}")

                    for i, item in enumerate(all_items):
                        # Determine the correct folder path for each project
                        if item['app_type'] == 'python_app':
                            # For Python apps, use the directory containing the script
                            project_folder = Path(item['script_path']).parent
                        else:  # standalone_html
                            # For HTML files, use the directory containing the HTML file
                            project_folder = Path(item['html_file']).parent

                        db_item = {
                            'item_type': item['app_type'],
                            'name': item['name'],
                            'folder_path': str(project_folder),
                            'main_file_path': item.get('script_path') or item.get('html_file'),
                            'html_interface_path': item.get('html_interface'),
                            'port': item.get('port', 5000),
                            'thumbnail_path': None,  # Will be set after screenshot
                            'file_size': _get_file_size(item.get('script_path') or item.get('html_file')),
                            'created_at': datetime.now().isoformat(),
                            'last_modified': _get_file_modified(item.get('script_path') or item.get('html_file')),
                            'dependencies': _extract_dependencies(item.get('script_path')) if item.get('script_path') else None
                        }

                        db.insert_item(db_item)

                        # Add simple_id to the item for thumbnail generation
                        # Get the simple_id from the database after insertion
                        with db.get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute('SELECT simple_id FROM indexed_items WHERE main_file_path = ?', (db_item['main_file_path'],))
                            row = cursor.fetchone()
                            if row:
                                item['simple_id'] = row[0]
                            else:
                                print(f"[SCAN_WORKER] Warning: Could not retrieve simple_id for {db_item['main_file_path']}")
                                item['simple_id'] = 'unknown'

                        scanning_completed += 1
                        print(f"[SCAN_WORKER] Processed item {i+1}/{len(all_items)}: {item['name']} ({item['app_type']})")
                        print(f"[SCAN_WORKER_DEBUG] After processing item {i+1} - scanning_completed: {scanning_completed}")

                    scanning_results['all_items'] = all_items
                    print(f"[SCAN_WORKER] Database operations completed")

                scanning_results[str(scan_task)] = {'success': True, 'error': None}

            except Exception as e:
                error_msg = f"Error in scanning worker: {str(e)}"
                print(f"[SCAN_WORKER] {error_msg}")
                scanning_results[str(scan_task)] = {'success': False, 'error': str(e)}

            scanning_queue.task_done()

        except queue.Empty:
            continue

def screenshot_worker():
    """LEGACY: Background worker to generate screenshots - kept for backward compatibility"""
    global processing_completed

    # Selenium setup
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument(f"--window-size={SCREENSHOT_WIDTH},{SCREENSHOT_HEIGHT}")

    driver = None
    try:
        service = ChromeService()
        driver = webdriver.Chrome(service=service, options=chrome_options)

        while True:
            try:
                html_file = processing_queue.get(timeout=1)
                if html_file is None:  # Shutdown signal
                    break

                try:
                    # Generate a safe filename for the thumbnail from the HTML file's absolute path
                    encoded_path = base64.urlsafe_b64encode(str(html_file).encode('utf-8')).decode('utf-8')
                    screenshot_path = THUMBNAILS_FOLDER / f"{encoded_path}.png"

                    # Take screenshot
                    file_uri = html_file.as_uri()
                    driver.get(file_uri)
                    driver.save_screenshot(str(screenshot_path))

                    processing_results[str(html_file)] = {'success': True, 'error': None}
                except Exception as e:
                    processing_results[str(html_file)] = {'success': False, 'error': str(e)}

                processing_completed += 1
                print(f"Completed: {processing_completed} / {processing_total}")
                processing_queue.task_done()

            except queue.Empty:
                continue

    except Exception as e:
        print(f"Worker error: {e}")
    finally:
        if driver:
            driver.quit()

@app.route('/')
def index():
    """Renders the main page with existing thumbnails."""
    thumbnails = get_existing_thumbnails()
    is_processing = processing_total > 0 and processing_completed < processing_total
    return render_template('smart-indexer-index-page.html', thumbnails=thumbnails, is_processing=is_processing,
                          processing_completed=processing_completed, processing_total=processing_total,
                          scanning_total=scanning_total, scanning_completed=scanning_completed)

@app.route('/progress')
def get_progress():
    """Returns current processing progress as JSON."""
    import json

    # Calculate progress percentage
    progress_percentage = 0
    if processing_total > 0:
        progress_percentage = (processing_completed / processing_total) * 100

    # Get current item being processed (if any)
    current_item = None
    queue_size = processing_queue.qsize()

    # Find the item currently being processed by checking processing_results
    # that don't have completion times yet
    for item_key, result in processing_results.items():
        if 'duration' not in result:  # Item is currently being processed
            try:
                current_item = json.loads(item_key)  # Convert back from string
                break
            except:
                continue

    # Calculate estimated time remaining
    eta_seconds = 0
    if processing_completed > 0:
        avg_time_per_item = sum(
            result.get('duration', 0)
            for result in processing_results.values()
            if result.get('success', False) and 'duration' in result
        ) / max(len([r for r in processing_results.values() if r.get('success', False)]), 1)

        remaining_items = processing_total - processing_completed
        eta_seconds = avg_time_per_item * remaining_items

    # Get recent errors for reporting
    recent_errors = []
    for item_key, result in processing_results.items():
        if not result.get('success', True) and result.get('error'):
            try:
                item_info = json.loads(item_key)
                recent_errors.append({
                    'name': item_info.get('name', 'Unknown'),
                    'type': item_info.get('app_type', 'Unknown'),
                    'error': result['error'][:100] + '...' if len(result['error']) > 100 else result['error']
                })
            except:
                continue

    return jsonify({
        'total': processing_total,
        'completed': processing_completed,
        'is_processing': processing_total > 0 and processing_completed < processing_total,
        'progress_percentage': round(progress_percentage, 1),
        'current_item': current_item,
        'queue_size': queue_size,
        'eta_seconds': round(eta_seconds),
        'recent_errors': recent_errors[:5],  # Last 5 errors
        'processing_rate': round(processing_completed / max(time.time() - processing_files[0].get('scan_start_time', time.time()), 1) * 60, 2) if processing_files else 0  # items per minute
    })

@app.route('/scanning-progress')
def get_scanning_progress():
    """Returns current scanning progress as JSON."""
    import json

    # Calculate progress percentage
    progress_percentage = 0
    if scanning_total > 0:
        progress_percentage = (scanning_completed / scanning_total) * 100

    # Get current phase description
    phase_descriptions = {
        "finding_python": "Finding Python web applications...",
        "finding_html": "Finding standalone HTML files...",
        "saving_database": "Saving items to database...",
        "": "Initializing scan..."
    }

    current_phase_desc = phase_descriptions.get(scanning_current_phase, scanning_current_phase)

    # Get recent errors for reporting
    recent_errors = []
    for task_key, result in scanning_results.items():
        # Only process dict results (task results), skip list results (data)
        if isinstance(result, dict) and not result.get('success', True) and result.get('error'):
            recent_errors.append({
                'phase': scanning_current_phase,
                'error': result['error'][:100] + '...' if len(result['error']) > 100 else result['error']
            })

    return jsonify({
        'total': scanning_total,
        'completed': scanning_completed,
        'is_scanning': scanning_total > 0 and scanning_completed < scanning_total,
        'progress_percentage': round(progress_percentage, 1),
        'current_phase': scanning_current_phase,
        'current_phase_desc': current_phase_desc,
        'queue_size': scanning_queue.qsize(),
        'recent_errors': recent_errors[:5],  # Last 5 errors
        'scan_start_time': scanning_files[0].get('scan_start_time') if scanning_files else None
    })

@app.route('/search')
def search():
    """Search items with optional filters."""
    query = request.args.get('q', '')
    tags = request.args.getlist('tag')
    category = request.args.get('category')

    items = db.search_items(query, tags if tags else None, category if category else None)
    return jsonify(items)

@app.route('/api/items')
def get_items():
    """Get all items as JSON."""
    # Return processed items like get_existing_thumbnails() but as JSON
    items = get_existing_thumbnails()
    return jsonify(items)

@app.route('/api/completed-items')
def get_completed_items():
    """Get items that have been completed since the last check."""
    try:
        # Get timestamp from query parameter (ISO format)
        since_timestamp = request.args.get('since')
        if not since_timestamp:
            return jsonify({'error': 'since parameter is required'}), 400

        # Parse timestamp
        try:
            since_dt = datetime.fromisoformat(since_timestamp.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'error': 'Invalid timestamp format'}), 400

        # Query database for items completed since the timestamp
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM indexed_items
                WHERE thumbnail_path IS NOT NULL
                AND last_modified > ?
                ORDER BY last_modified DESC
            ''', (since_dt.isoformat(),))

            columns = [desc[0] for desc in cursor.description]
            completed_items = []

            for row in cursor.fetchall():
                item = dict(zip(columns, row))

                # Build thumbnail URL like get_existing_thumbnails()
                thumb_url = '/static/thumbnails/placeholder.png'
                if item['thumbnail_path']:
                    thumb_path = Path(item['thumbnail_path'])
                    if thumb_path.exists():
                        thumb_filename = thumb_path.name
                        thumb_url = f'/static/thumbnails/{thumb_filename}'

                # Format item similar to get_existing_thumbnails()
                formatted_item = {
                    'thumb_url': thumb_url,
                    'name': item['name'],
                    'app_type': item['item_type'],
                    'port': item['port'] or 5000,
                    'id': item['id'],
                    'simple_id': item.get('simple_id', ''),
                    'description': item.get('description', ''),
                    'short_desc': item.get('short_desc', ''),
                    'tech_stack': item.get('tech_stack', ''),
                    'tags': item.get('tags', ''),
                    'category': item.get('category', ''),
                    'file_size': item.get('file_size', 0),
                    'created_at': item.get('created_at', ''),
                    'last_modified': item.get('last_modified', ''),
                    'dependencies': item.get('dependencies', ''),
                    'llm_processed': item.get('llm_processed', False),
                    'folder_path': item.get('folder_path', ''),
                    'dependencies_ok': True
                }

                # Check dependencies for Python apps
                if item['item_type'] == 'python_app':
                    formatted_item['dependencies_ok'] = _check_dependencies(item.get('dependencies', ''))

                # Use simple_id for URLs
                if item.get('simple_id'):
                    formatted_item['html_file_url'] = f'/serve/{item["simple_id"]}'
                    if item['item_type'] == 'python_app':
                        formatted_item['python_script'] = item['main_file_path']
                        if item['html_interface_path']:
                            formatted_item['html_interface'] = item['html_interface_path']
                    elif item['item_type'] == 'standalone_html':
                        formatted_item['html_file'] = item['main_file_path']

                completed_items.append(formatted_item)

        return jsonify({
            'completed_items': completed_items,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        print(f"Error in get_completed_items: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/export')
def export_for_llm():
    """Export data for LLM processing."""
    data = db.export_for_llm()
    return jsonify(data)

@app.route('/api/tags')
def get_tags():
    """Get available tags."""
    tags = db.get_available_tags()
    return jsonify(tags)

@app.route('/api/process-llm/<int:item_id>', methods=['POST'])
def process_llm_item(item_id):
    """Process a single item with LLM."""
    try:
        processor = LLMProcessor()
        result = processor.process_item(item_id)
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/process-llm-all', methods=['POST'])
def process_llm_all():
    """Process all unprocessed items with LLM."""
    try:
        processor = LLMProcessor()
        count = processor.process_all_unprocessed()
        return jsonify({'success': True, 'processed': count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/toggle-favourite/<int:item_id>', methods=['POST'])
def toggle_favourite(item_id):
    """Toggle the favourite status of an item."""
    try:
        success = db.toggle_favourite(item_id)
        if success:
            # Get the updated item to return the new status
            item = db.get_item_by_id(item_id)
            if item:
                return jsonify({'success': True, 'is_favourite': item['is_favourite']})
            else:
                return jsonify({'success': False, 'error': 'Item not found'}), 404
        else:
            return jsonify({'success': False, 'error': 'Failed to toggle favourite'}), 500
    except Exception as e:
        print(f"Error in toggle_favourite: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/update-description/<int:item_id>', methods=['POST'])
def update_description(item_id):
    """Update description for an item."""
    try:
        data = request.get_json()
        new_description = data.get('description', '')

        # Update the database
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE indexed_items
                SET description = ?
                WHERE id = ?
            ''', (new_description, item_id))
            conn.commit()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cleanup', methods=['POST'])
def cleanup_dead_records():
    """Remove records for files that no longer exist."""
    try:
        removed = db.cleanup_old_items([])
        return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/regenerate-thumbnails', methods=['POST'])
def regenerate_thumbnails():
    """Regenerate thumbnails for items that don't have them or have missing files."""
    global processing_files, processing_results, processing_completed, processing_total

    try:
        # Get all items without thumbnails or with missing thumbnail files
        all_items = db.get_all_items()
        items_to_process = []

        for item in all_items:
            needs_thumbnail = False

            if not item['thumbnail_path']:
                needs_thumbnail = True
            else:
                thumb_path = Path(item['thumbnail_path'])
                if not thumb_path.exists():
                    needs_thumbnail = True
                    print(f"Thumbnail missing for {item['name']}: {thumb_path}")

            if needs_thumbnail:
                # Convert DB item to processing format
                if item['item_type'] == 'python_app':
                    process_item = {
                        'app_type': 'python_app',
                        'script_path': item['main_file_path'],
                        'name': item['name'],
                        'port': item['port'] or 5000,
                        'html_interface': item.get('html_interface_path')
                    }
                elif item['item_type'] == 'standalone_html':
                    process_item = {
                        'app_type': 'standalone_html',
                        'html_file': item['main_file_path'],
                        'name': item['name']
                    }
                else:
                    continue

                items_to_process.append(process_item)

        if not items_to_process:
            return jsonify({'success': True, 'message': 'All items already have thumbnails', 'count': 0})

        # Reset processing state
        processing_files = items_to_process
        processing_results = {}
        processing_completed = 0
        processing_total = len(items_to_process)

        # Start background worker thread
        worker_thread = threading.Thread(target=smart_screenshot_worker)
        worker_thread.daemon = True
        worker_thread.start()

        # Add all items to queue
        for item in items_to_process:
            processing_queue.put(item)

        return jsonify({
            'success': True,
            'message': f'Started regenerating thumbnails for {len(items_to_process)} items',
            'count': len(items_to_process)
        })

    except Exception as e:
        print(f"Error in regenerate_thumbnails: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/clean-apps', methods=['POST'])
def clean_apps():
    """Stop all running Python applications to free up RAM."""
    global current_python_app
    try:
        stopped_count = 0

        # Stop the current Python app if running
        if current_python_app and current_python_app.get('process'):
            try:
                process = current_python_app['process']
                if process.poll() is None:  # Process is still running
                    process.terminate()
                    process.wait(timeout=5)
                    stopped_count += 1
                    print(f"Terminated Python app: {current_python_app.get('script_path', 'Unknown')}")
                else:
                    print(f"Python app already stopped: {current_python_app.get('script_path', 'Unknown')}")
            except subprocess.TimeoutExpired:
                process.kill()
                stopped_count += 1
                print(f"Killed Python app: {current_python_app.get('script_path', 'Unknown')}")
            except Exception as e:
                print(f"Error stopping current Python app: {e}")

            current_python_app = None

        # Stop all running HTML servers
        global running_html_servers
        html_servers_stopped = len(running_html_servers)
        servers_to_stop = list(running_html_servers.keys())
        for html_path in servers_to_stop:
            stop_html_server(html_path)
        stopped_count += html_servers_stopped

        return jsonify({
            'success': True,
            'stopped_count': stopped_count,
            'message': f'Successfully stopped {stopped_count} running applications'
        })

    except Exception as e:
        print(f"Error in clean_apps: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/purge-database', methods=['POST'])
def purge_database():
    """Purge all data from the database."""
    try:
        # Remove all thumbnails
        thumbnails_dir = Path("static/thumbnails")
        if thumbnails_dir.exists():
            for thumb_file in thumbnails_dir.glob("*"):
                if thumb_file.name != "placeholder.png":  # Keep placeholder
                    try:
                        thumb_file.unlink()
                    except Exception as e:
                        print(f"Warning: Could not delete thumbnail {thumb_file}: {e}")

        # Clear database tables
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM indexed_items')
            cursor.execute('DELETE FROM available_tags')
            conn.commit()

        return jsonify({'success': True, 'message': 'Database purged successfully'})

    except Exception as e:
        print(f"Error in purge_database: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/edit-project', methods=['POST'])
def edit_project():
    """Open a project folder in VS Code."""
    try:
        data = request.get_json()
        folder_path = data.get('folder_path')
        print(f"DEBUG: Received folder_path in edit_project: '{folder_path}'")

        if not folder_path:
            return jsonify({'success': False, 'error': 'folder_path is required'}), 400

        # Validate and resolve folder path
        folder_path_obj = Path(folder_path).resolve()

        if not folder_path_obj.exists() or not folder_path_obj.is_dir():
            return jsonify({'success': False, 'error': 'Invalid folder path'}), 400

        # Check if it's a known project path (has indexed items)
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM indexed_items WHERE folder_path = ?', (str(folder_path_obj),))
            count = cursor.fetchone()[0]

        if count == 0:
            return jsonify({'success': False, 'error': 'Folder path not found in indexed projects'}), 400

        # Execute command to open in VS Code
        command = f'cd "{folder_path_obj}" && code .'
        subprocess.run(command, shell=True, check=True)

        return jsonify({'success': True})

    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': f'Failed to open VS Code: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/remove_item', methods=['POST'])
def remove_item():
    """Remove a single item by ID and delete its associated thumbnail."""
    try:
        item_id = request.form.get('item_id') or request.json.get('item_id') if request.is_json else None

        if not item_id:
            return jsonify({'success': False, 'error': 'item_id parameter is required'}), 400

        # Convert to int if it's a string
        try:
            item_id = int(item_id)
        except ValueError:
            return jsonify({'success': False, 'error': 'item_id must be a valid integer'}), 400

        success = db.remove_item(item_id)
        if success:
            return jsonify({'success': True, 'message': 'Item removed successfully'})
        else:
            return jsonify({'success': False, 'error': 'Item not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/remove_folder', methods=['POST'])
def remove_folder():
    """Remove all items from a specific folder and delete associated thumbnails."""
    try:
        folder_path = request.form.get('folder_path') or request.json.get('folder_path') if request.is_json else None

        if not folder_path:
            return jsonify({'success': False, 'error': 'folder_path parameter is required'}), 400

        removed_count = db.remove_folder_items(folder_path)
        return jsonify({'success': True, 'removed': removed_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def serve_static_html_with_relative_paths(html_path, simple_id):
    """Serve HTML file with relative paths for assets."""
    try:
        html_path = Path(html_path)
        html_dir = html_path.parent

        # Read HTML content
        with open(html_path, 'r', encoding='utf-8', errors='ignore') as f:
            html_content = f.read()

        # Fix relative paths in HTML to use the new asset route
        def fix_quoted_path(match):
            attr = match.group(1)  # 'href' or 'src'
            quote = match.group(2)  # quote character
            path = match.group(3)   # the path

            # Skip absolute URLs, data URIs, and paths starting with /
            if path.startswith(('http://', 'https://', 'data:', '/')):
                return match.group(0)

            # Convert relative path to absolute path from HTML directory
            abs_path = (html_dir / path).resolve()

            if abs_path.exists():
                # Create asset URL using simple_id
                new_url = f'{attr}={quote}/assets/{simple_id}/{path}{quote}'
                return new_url
            else:
                # File doesn't exist, keep original
                return match.group(0)

        def fix_unquoted_path(match):
            attr = match.group(1)  # 'href' or 'src'
            path = match.group(2)   # the path

            # Skip absolute URLs, data URIs, and paths starting with /
            if path.startswith(('http://', 'https://', 'data:', '/')):
                return match.group(0)

            # Convert relative path to absolute path from HTML directory
            abs_path = (html_dir / path).resolve()

            if abs_path.exists():
                # Create asset URL using simple_id
                new_attr = f'{attr}="/assets/{simple_id}/{path}"'
                return new_attr
            else:
                # File doesn't exist, keep original
                return match.group(0)

        def fix_css_url(match):
            url_content = match.group(1)

            # Skip absolute URLs, data URIs
            if url_content.startswith(('http://', 'https://', 'data:')):
                return match.group(0)

            # Remove quotes if present
            clean_url = url_content.strip('\'"')

            # Convert relative path to absolute path from HTML directory
            abs_path = (html_dir / clean_url).resolve()

            if abs_path.exists():
                # Create asset URL using simple_id
                new_url = f'url("/assets/{simple_id}/{clean_url}")'
                return new_url
            else:
                # File doesn't exist, keep original
                return match.group(0)

        # Fix href attributes (with quotes)
        html_content = re.sub(r'(href|src)\s*([\'"])([^\'"]*)\2', fix_quoted_path, html_content)

        # Fix unquoted href/src attributes
        html_content = re.sub(r'(href|src)\s*=\s*([^\'"\s>]+)', fix_unquoted_path, html_content)

        # Fix CSS url() references
        html_content = re.sub(r'url\s*\(\s*([^\)]+)\s*\)', fix_css_url, html_content)

        # No base tag needed - using relative paths

        from flask import Response
        return Response(html_content, mimetype='text/html')

    except Exception as e:
        print(f"Error serving HTML with relative paths: {e}")
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500

def serve_static_html_with_fixed_paths(html_path):
    """Legacy: Serve HTML file with fixed relative paths for assets."""
    try:
        html_path = Path(html_path)
        html_dir = html_path.parent

        # Read HTML content
        with open(html_path, 'r', encoding='utf-8', errors='ignore') as f:
            html_content = f.read()

        # Create a base path for assets relative to this HTML file
        base_encoded = base64.urlsafe_b64encode(str(html_path).encode('utf-8')).decode('utf-8')

        # Fix relative paths in HTML
        # Convert relative href/src to absolute paths using our asset serving route
        def fix_quoted_path(match):
            attr = match.group(1)  # 'href' or 'src'
            quote = match.group(2)  # quote character
            path = match.group(3)   # the path

            # Skip absolute URLs, data URIs, and paths starting with /
            if path.startswith(('http://', 'https://', 'data:', '/')):
                return match.group(0)

            # Convert relative path to absolute path from HTML directory
            abs_path = (html_dir / path).resolve()

            if abs_path.exists():
                # Create asset URL
                asset_encoded = base64.urlsafe_b64encode(str(abs_path).encode('utf-8')).decode('utf-8')
                new_url = f'{attr}={quote}/asset/{asset_encoded}{quote}'
                return new_url
            else:
                # File doesn't exist, keep original
                return match.group(0)

        def fix_unquoted_path(match):
            attr = match.group(1)  # 'href' or 'src'
            path = match.group(2)   # the path

            # Skip absolute URLs, data URIs, and paths starting with /
            if path.startswith(('http://', 'https://', 'data:', '/')):
                return match.group(0)

            # Convert relative path to absolute path from HTML directory
            abs_path = (html_dir / path).resolve()

            if abs_path.exists():
                # Create asset URL
                asset_encoded = base64.urlsafe_b64encode(str(abs_path).encode('utf-8')).decode('utf-8')
                new_attr = f'{attr}="/asset/{asset_encoded}"'
                return new_attr
            else:
                # File doesn't exist, keep original
                return match.group(0)

        def fix_css_url(match):
            url_content = match.group(1)

            # Skip absolute URLs, data URIs
            if url_content.startswith(('http://', 'https://', 'data:')):
                return match.group(0)

            # Remove quotes if present
            clean_url = url_content.strip('\'"')

            # Convert relative path to absolute path from HTML directory
            abs_path = (html_dir / clean_url).resolve()

            if abs_path.exists():
                # Create asset URL
                asset_encoded = base64.urlsafe_b64encode(str(abs_path).encode('utf-8')).decode('utf-8')
                new_url = f'url("/asset/{asset_encoded}")'
                return new_url
            else:
                # File doesn't exist, keep original
                return match.group(0)

        # Fix href attributes (with quotes)
        html_content = re.sub(r'href\s*([\'"])([^\'"]*)\1', fix_quoted_path, html_content)

        # Fix src attributes (with quotes)
        html_content = re.sub(r'src\s*([\'"])([^\'"]*)\1', fix_quoted_path, html_content)

        # Fix unquoted href attributes
        html_content = re.sub(r'href\s*=\s*([^\'"\s>]+)', fix_unquoted_path, html_content)

        # Fix unquoted src attributes
        html_content = re.sub(r'src\s*=\s*([^\'"\s>]+)', fix_unquoted_path, html_content)

        # Fix CSS url() references
        html_content = re.sub(r'url\s*\(\s*([^\)]+)\s*\)', fix_css_url, html_content)

        # Add base tag to ensure relative paths work correctly
        # Insert after <head> tag
        head_pattern = r'(<head[^>]*>)'
        base_tag = f'<base href="/html/{base_encoded}/">'
        html_content = re.sub(head_pattern, r'\1' + base_tag, html_content, flags=re.IGNORECASE)

        from flask import Response
        return Response(html_content, mimetype='text/html')

    except Exception as e:
        print(f"Error serving HTML with fixed paths: {e}")
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500

@app.route('/assets/<simple_id>/<path:asset_path>')
def serve_asset_by_simple_id(simple_id, asset_path):
    """Serve asset files using simple ID and relative path."""
    try:
        # Find the HTML file directory and folder by simple_id
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT main_file_path, item_type, folder_path FROM indexed_items WHERE simple_id = ?', (simple_id,))
            row = cursor.fetchone()
            if not row:
                return "Application not found", 404

            main_file_path, item_type, folder_path = row

        if item_type == 'python_app':
            # For Python apps, use the HTML interface path
            cursor.execute('SELECT html_interface_path FROM indexed_items WHERE simple_id = ?', (simple_id,))
            row = cursor.fetchone()
            if row and row[0]:
                html_path = Path(row[0])
            else:
                return "HTML interface not found", 404
        else:
            # For standalone HTML, use main file path
            html_path = Path(main_file_path)

        # Resolve asset path relative to HTML file's directory
        html_dir = html_path.parent
        full_asset_path = (html_dir / asset_path).resolve()

        # Security check: ensure the asset is within the scanned folder
        if not str(full_asset_path).startswith(folder_path):
            return "Access denied", 403

        if not full_asset_path.exists() or not full_asset_path.is_file():
            return "Asset not found", 404

        # Determine MIME type based on extension
        ext = full_asset_path.suffix.lower()
        mime_types = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff': 'font/woff',
            '.woff2': 'font/woff2',
            '.ttf': 'font/ttf',
            '.eot': 'application/vnd.ms-fontobject'
        }

        mime_type = mime_types.get(ext, 'application/octet-stream')
        return send_file(str(full_asset_path), mimetype=mime_type)

    except Exception as e:
        print(f"Error serving asset: {e}")
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500

@app.route('/asset/<encoded_path>')
def serve_asset(encoded_path):
    """Legacy: Serve asset files (CSS, JS, images) referenced by HTML files."""
    try:
        asset_path_bytes = base64.urlsafe_b64decode(encoded_path + '===')
        asset_path_str = asset_path_bytes.decode('utf-8')
        asset_path = Path(asset_path_str)

        if not asset_path.exists() or not asset_path.is_file():
            return "Asset not found", 404

        # Determine MIME type based on extension
        ext = asset_path.suffix.lower()
        mime_types = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff': 'font/woff',
            '.woff2': 'font/woff2',
            '.ttf': 'font/ttf',
            '.eot': 'application/vnd.ms-fontobject'
        }

        mime_type = mime_types.get(ext, 'application/octet-stream')
        return send_file(asset_path_str, mimetype=mime_type)

    except Exception as e:
        print(f"Error serving asset: {e}")
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500

@app.route('/serve/<simple_id>')
def serve_by_simple_id(simple_id):
    """Serve HTML or start Python app by simple ID."""
    global current_python_app
    try:
        # Find item by simple_id
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM indexed_items WHERE simple_id = ?', (simple_id,))
            row = cursor.fetchone()
            if not row:
                return "Application not found", 404

            columns = [desc[0] for desc in cursor.description]
            item = dict(zip(columns, row))

        if item['item_type'] == 'python_app':
            # Start the Python app
            script_path = Path(item['main_file_path'])
            port = item['port'] or 5000

            # Kill previous app if different
            if current_python_app and current_python_app['script_path'] != str(script_path):
                current_python_app['process'].terminate()
                current_python_app['process'].wait()
                current_python_app = None

            # If same app already running, redirect
            if current_python_app and current_python_app['script_path'] == str(script_path):
                return redirect(current_python_app['url'])

            # Ensure apps-debris directory exists
            apps_debris_dir = Path('apps-debris')
            apps_debris_dir.mkdir(exist_ok=True)

            # Start the Python app
            print(f"Starting Python app: {script_path}")
            env = {
                'PORT': str(port),
                'FLASK_DEBUG': '0',
                'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                'HOME': os.environ.get('HOME', ''),
                'USER': os.environ.get('USER', ''),
            }
            def preexec():
                os.closerange(3, 1024)
            p = subprocess.Popen(['python', str(script_path)], env=env, preexec_fn=preexec, cwd=apps_debris_dir)

            url = f'http://localhost:{port}'
            current_python_app = {'process': p, 'script_path': str(script_path), 'url': url}

            # Wait for app to start
            time.sleep(5)

            return redirect(url)

        elif item['item_type'] == 'standalone_html':
            # Serve static HTML using HTTP server approach like screenshot function
            html_path = Path(item['main_file_path'])
            port = get_or_start_html_server(html_path)

            # Redirect to the served HTML
            return redirect(f'http://localhost:{port}')

        else:
            return "Unknown item type", 400

    except Exception as e:
        print(f"Error in serve_by_simple_id: {str(e)}")
        return f"Error: {str(e)}", 500

@app.route('/<name>.html')
def serve_by_name(name):
    """Legacy route: Serve HTML or start Python app by name."""
    global current_python_app
    try:
        # Find item by matching the clean name
        items = db.get_all_items()
        item = None
        for i in items:
            folder_name = Path(i['folder_path']).name
            expected_clean = f"{folder_name}-{i['name']}".replace(' ', '-').replace('_', '-').lower()
            if expected_clean == name.lower():
                item = i
                break

        if not item:
            return "Application not found", 404

        if item['item_type'] == 'python_app':
            # Start the Python app
            script_path = Path(item['main_file_path'])
            port = item['port'] or 5000

            # Kill previous app if different
            if current_python_app and current_python_app['script_path'] != str(script_path):
                current_python_app['process'].terminate()
                current_python_app['process'].wait()
                current_python_app = None

            # If same app already running, redirect
            if current_python_app and current_python_app['script_path'] == str(script_path):
                return redirect(current_python_app['url'])

            # Ensure apps-debris directory exists
            apps_debris_dir = Path('apps-debris')
            apps_debris_dir.mkdir(exist_ok=True)

            # Start the Python app
            print(f"Starting Python app: {script_path}")
            env = {
                'PORT': str(port),
                'FLASK_DEBUG': '0',
                'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                'HOME': os.environ.get('HOME', ''),
                'USER': os.environ.get('USER', ''),
            }
            def preexec():
                os.closerange(3, 1024)
            p = subprocess.Popen(['python', str(script_path)], env=env, preexec_fn=preexec, cwd=apps_debris_dir)

            url = f'http://localhost:{port}'
            current_python_app = {'process': p, 'script_path': str(script_path), 'url': url}

            # Wait for app to start
            time.sleep(5)

            return redirect(url)

        elif item['item_type'] == 'standalone_html':
            # Serve static HTML using HTTP server approach like screenshot function
            html_path = Path(item['main_file_path'])
            port = get_or_start_html_server(html_path)

            # Redirect to the served HTML
            return redirect(f'http://localhost:{port}')

        else:
            return "Unknown item type", 400

    except Exception as e:
        print(f"Error in serve_by_name: {str(e)}")
        return f"Error: {str(e)}", 500

@app.route('/html/<encoded_path>')
def serve_html(encoded_path):
    """Serves HTML files dynamically or starts Python apps if required (legacy route)."""
    print(f"Serving HTML: {encoded_path}", flush=True)
    global current_python_app
    try:
        html_path_bytes = base64.urlsafe_b64decode(encoded_path + '===')
        html_path_str = html_path_bytes.decode('utf-8')
        html_path = Path(html_path_str)

        # Check if this is an indexed item
        item = db.get_item_by_html_path(html_path_str)
        if item:
            if item['item_type'] == 'python_app':
                # Start the Python app
                script_path = Path(item['main_file_path'])
                port = item['port'] or 5000

                # Kill previous app if different
                if current_python_app and current_python_app['script_path'] != str(script_path):
                    current_python_app['process'].terminate()
                    current_python_app['process'].wait()
                    current_python_app = None

                # If same app already running, redirect
                if current_python_app and current_python_app['script_path'] == str(script_path):
                    return redirect(current_python_app['url'])

                # Ensure apps-debris directory exists
                apps_debris_dir = Path('apps-debris')
                apps_debris_dir.mkdir(exist_ok=True)

                # Start the Python app
                print(f"Starting Python app: {script_path}")
                env = {
                    'PORT': str(port),
                    'FLASK_DEBUG': '0',
                    'PATH': os.environ.get('PATH', ''),
                    'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                    'HOME': os.environ.get('HOME', ''),
                    'USER': os.environ.get('USER', ''),
                }
                def preexec():
                    os.closerange(3, 1024)
                p = subprocess.Popen(['python', str(script_path)], env=env, preexec_fn=preexec, cwd=apps_debris_dir)

                url = f'http://localhost:{port}'
                current_python_app = {'process': p, 'script_path': str(script_path), 'url': url}

                # Wait for app to start
                time.sleep(5)

                return redirect(url)

            elif item['item_type'] == 'standalone_html':
                # Serve static HTML
                return serve_static_html_with_fixed_paths(html_path)
        else:
            # Legacy logic for non-indexed files
            requires_python = False
            html_content = ""
            if html_path.exists():
                with open(html_path, 'r', encoding='utf-8', errors='ignore') as f:
                    html_content = f.read()
                    requires_python = detect_python_backend(html_content)

            if requires_python:
                # Find corresponding Python script
                py_file = html_path.with_suffix('.py')
                if not py_file.exists():
                    return "Python script not found for this HTML file", 404

                # Ensure apps-debris directory exists
                apps_debris_dir = Path('apps-debris')
                apps_debris_dir.mkdir(exist_ok=True)

                # Start the Python app on port 5000
                print(f"Starting Python app: {py_file}")
                env = {
                    'PORT': '5000',
                    'FLASK_DEBUG': '0',
                    'PATH': os.environ.get('PATH', ''),
                    'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                    'HOME': os.environ.get('HOME', ''),
                    'USER': os.environ.get('USER', ''),
                }
                def preexec():
                    os.closerange(3, 1024)
                p = subprocess.Popen(['python', str(py_file)], env=env, preexec_fn=preexec, cwd=apps_debris_dir)

                url = 'http://localhost:5000'
                current_python_app = {'process': p, 'html_path': str(html_path), 'url': url}

                # Wait a bit for the app to start
                time.sleep(5)

                return redirect(url)
            else:
                # Serve static HTML using HTTP server approach like screenshot function
                port = get_or_start_html_server(html_path)

                # Redirect to the served HTML
                return redirect(f'http://localhost:{port}')
    except Exception as e:
        print(f"Error in serve_html: {str(e)}")
        return f"Error: {str(e)}", 500

@app.route('/scan', methods=['POST'])
def scan_folder():
    """Handles the folder scanning using smart Python-first indexing with real-time progress."""
    global scanning_files, scanning_results, scanning_completed, scanning_total, scanning_current_phase

    folder_path_str = request.form.get('folder_path')

    if not folder_path_str:
        flash("Please provide a folder path.", "error")
        return redirect(url_for('index'))

    target_folder = Path(folder_path_str).resolve()

    if not target_folder.is_dir():
        flash(f"Error: '{target_folder}' is not a valid directory.", "error")
        return redirect(url_for('index'))

    print(f"[SCAN_DEBUG] Starting real-time scan of folder: {target_folder}")

    # Reset scanning state - set initial total to 1 to show progress bar immediately
    scanning_files = [{'scan_start_time': time.time()}]
    scanning_results = {}
    scanning_completed = 0
    scanning_total = 1  # Start with 1 to show progress bar immediately
    scanning_current_phase = "initializing"

    # Start background scanning worker thread
    print(f"[SCAN_DEBUG] Starting background scanning worker thread...")
    scanning_worker_thread = threading.Thread(target=scanning_worker)
    scanning_worker_thread.daemon = True
    scanning_worker_thread.start()

    # Add scanning tasks to queue in order
    scanning_tasks = [
        {'type': 'find_python_apps', 'target_folder': target_folder},
        {'type': 'find_html_files', 'target_folder': target_folder},
        {'type': 'save_to_database', 'target_folder': target_folder}
    ]

    print(f"[SCAN_DEBUG] Adding {len(scanning_tasks)} scanning tasks to queue...")
    for task in scanning_tasks:
        scanning_queue.put(task)

    flash(f"Started scanning folder '{target_folder.name}'. Progress will be shown in real-time.", "success")
    return redirect(url_for('index'))


@app.route('/api/github/check', methods=['POST'])
def check_github_repo():
    """Check if a GitHub repository has Python or HTML files."""
    try:
        data = request.get_json()
        github_url = data.get('url', '').strip()
        
        if not github_url:
            return jsonify({'success': False, 'error': 'GitHub URL is required'}), 400
        
        # Parse the GitHub URL
        parsed = parse_github_url(github_url)
        if not parsed:
            return jsonify({
                'success': False,
                'error': 'Invalid GitHub URL. Supported formats: https://github.com/owner/repo, owner/repo'
            }), 400
        
        owner, repo = parsed
        
        # Check if repo has target files
        check_result = check_github_repo_has_target_files(owner, repo)
        
        if check_result['error']:
            return jsonify({'success': False, 'error': check_result['error']}), 400
        
        has_target_files = check_result['has_python'] or check_result['has_html']
        
        return jsonify({
            'success': True,
            'owner': owner,
            'repo': repo,
            'has_python': check_result['has_python'],
            'has_html': check_result['has_html'],
            'python_count': check_result['file_count']['python'],
            'html_count': check_result['file_count']['html'],
            'has_target_files': has_target_files,
            'message': f"Found {check_result['file_count']['python']} Python and {check_result['file_count']['html']} HTML files" if has_target_files else "No Python or HTML files found in this repository"
        })
        
    except Exception as e:
        print(f"Error checking GitHub repo: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/github/clone', methods=['POST'])
def clone_github_repo_route():
    """Clone a GitHub repository and index it."""
    global scanning_files, scanning_results, scanning_completed, scanning_total, scanning_current_phase
    
    try:
        data = request.get_json()
        github_url = data.get('url', '').strip()
        skip_check = data.get('skip_check', False)
        
        if not github_url:
            return jsonify({'success': False, 'error': 'GitHub URL is required'}), 400
        
        # Parse the GitHub URL
        parsed = parse_github_url(github_url)
        if not parsed:
            return jsonify({
                'success': False,
                'error': 'Invalid GitHub URL. Supported formats: https://github.com/owner/repo, owner/repo'
            }), 400
        
        owner, repo = parsed
        
        # Optionally check for target files first
        if not skip_check:
            check_result = check_github_repo_has_target_files(owner, repo)
            
            if check_result['error']:
                return jsonify({'success': False, 'error': check_result['error']}), 400
            
            if not check_result['has_python'] and not check_result['has_html']:
                return jsonify({
                    'success': False,
                    'error': 'Repository does not contain any Python or HTML files. Use skip_check=true to clone anyway.'
                }), 400
        
        # Clone the repository
        clone_result = clone_github_repo(owner, repo)
        
        if not clone_result['success']:
            return jsonify({'success': False, 'error': clone_result['error']}), 400
        
        target_folder = Path(clone_result['path'])
        
        # Start indexing the cloned repository
        print(f"[GITHUB_SCAN] Starting scan of cloned repository: {target_folder}")
        
        # Reset scanning state
        scanning_files = [{'scan_start_time': time.time()}]
        scanning_results = {}
        scanning_completed = 0
        scanning_total = 1
        scanning_current_phase = "initializing"
        
        # Start background scanning worker thread
        scanning_worker_thread = threading.Thread(target=scanning_worker)
        scanning_worker_thread.daemon = True
        scanning_worker_thread.start()
        
        # Add scanning tasks to queue
        scanning_tasks = [
            {'type': 'find_python_apps', 'target_folder': target_folder},
            {'type': 'find_html_files', 'target_folder': target_folder},
            {'type': 'save_to_database', 'target_folder': target_folder}
        ]
        
        for task in scanning_tasks:
            scanning_queue.put(task)
        
        return jsonify({
            'success': True,
            'message': f"Successfully cloned {owner}/{repo}. Indexing started.",
            'path': str(target_folder),
            'clone_message': clone_result.get('message', 'Repository cloned')
        })
        
    except Exception as e:
        print(f"Error cloning GitHub repo: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/github/repos', methods=['GET'])
def list_cloned_repos():
    """List all cloned GitHub repositories."""
    try:
        repos = get_cloned_repos()
        return jsonify({
            'success': True,
            'repos': repos
        })
    except Exception as e:
        print(f"Error listing cloned repos: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/github/delete', methods=['POST'])
def delete_cloned_repo():
    """Delete a cloned repository."""
    try:
        data = request.get_json()
        repo_path = data.get('path', '').strip()
        
        if not repo_path:
            return jsonify({'success': False, 'error': 'Repository path is required'}), 400
        
        repo_path = Path(repo_path)
        
        # Security check: ensure the path is within the repos folder
        if not str(repo_path.resolve()).startswith(str(REPOS_FOLDER.resolve())):
            return jsonify({'success': False, 'error': 'Invalid repository path'}), 400
        
        if not repo_path.exists():
            return jsonify({'success': False, 'error': 'Repository not found'}), 404
        
        # Remove indexed items from database
        removed_count = db.remove_folder_items(str(repo_path))
        
        # Delete the repository folder
        shutil.rmtree(repo_path)
        
        return jsonify({
            'success': True,
            'message': f'Repository deleted. Removed {removed_count} indexed items.'
        })
        
    except Exception as e:
        print(f"Error deleting cloned repo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/scan-github', methods=['POST'])
def scan_github():
    """Handle GitHub repository scan from form submission."""
    global scanning_files, scanning_results, scanning_completed, scanning_total, scanning_current_phase
    
    github_url = request.form.get('github_url', '').strip()
    
    if not github_url:
        flash("Please provide a GitHub repository URL.", "error")
        return redirect(url_for('index'))
    
    # Parse the GitHub URL
    parsed = parse_github_url(github_url)
    if not parsed:
        flash("Invalid GitHub URL. Supported formats: https://github.com/owner/repo, owner/repo", "error")
        return redirect(url_for('index'))
    
    owner, repo = parsed
    
    # Check if repo has target files
    print(f"[GITHUB_SCAN] Checking repository {owner}/{repo} for Python/HTML files...")
    check_result = check_github_repo_has_target_files(owner, repo)
    
    if check_result['error']:
        flash(f"Error checking repository: {check_result['error']}", "error")
        return redirect(url_for('index'))
    
    if not check_result['has_python'] and not check_result['has_html']:
        flash(f"Repository '{owner}/{repo}' does not contain any Python or HTML files.", "warning")
        return redirect(url_for('index'))
    
    # Clone the repository
    print(f"[GITHUB_SCAN] Cloning repository {owner}/{repo}...")
    clone_result = clone_github_repo(owner, repo)
    
    if not clone_result['success']:
        flash(f"Error cloning repository: {clone_result['error']}", "error")
        return redirect(url_for('index'))
    
    target_folder = Path(clone_result['path'])
    
    # Start indexing the cloned repository
    print(f"[GITHUB_SCAN] Starting scan of cloned repository: {target_folder}")
    
    # Reset scanning state
    scanning_files = [{'scan_start_time': time.time()}]
    scanning_results = {}
    scanning_completed = 0
    scanning_total = 1
    scanning_current_phase = "initializing"
    
    # Start background scanning worker thread
    scanning_worker_thread = threading.Thread(target=scanning_worker)
    scanning_worker_thread.daemon = True
    scanning_worker_thread.start()
    
    # Add scanning tasks to queue
    scanning_tasks = [
        {'type': 'find_python_apps', 'target_folder': target_folder},
        {'type': 'find_html_files', 'target_folder': target_folder},
        {'type': 'save_to_database', 'target_folder': target_folder}
    ]
    
    for task in scanning_tasks:
        scanning_queue.put(task)
    
    file_info = f"({check_result['file_count']['python']} Python, {check_result['file_count']['html']} HTML files)"
    flash(f"Successfully cloned '{owner}/{repo}' {file_info}. Indexing started.", "success")
    return redirect(url_for('index'))


# Global variable for launcher process
launcher_process = None

def start_launcher():
    """Start the launcher service in the background."""
    global launcher_process
    try:
        print("Starting launcher service...")
        launcher_process = subprocess.Popen(['python', 'launcher.py'])
        print(f"Launcher started with PID: {launcher_process.pid}")
    except Exception as e:
        print(f"Failed to start launcher: {e}")

def stop_launcher():
    """Stop the launcher service."""
    global launcher_process
    if launcher_process:
        try:
            launcher_process.terminate()
            launcher_process.wait(timeout=5)
            print("Launcher stopped")
        except subprocess.TimeoutExpired:
            launcher_process.kill()
            print("Launcher killed")
        except Exception as e:
            print(f"Error stopping launcher: {e}")

def cleanup_html_servers():
    """Stop all running HTML servers."""
    global running_html_servers
    servers_to_stop = list(running_html_servers.keys())
    for html_path in servers_to_stop:
        stop_html_server(html_path)
    print(f"Cleaned up {len(servers_to_stop)} HTML servers")

# Register cleanup functions
atexit.register(stop_launcher)
atexit.register(cleanup_html_servers)

@app.route('/<path:file_path>')
def serve_html_file(file_path):
    """Serve HTML file with fixed paths if it exists."""
    try:
        html_path = Path('/') / file_path
        if html_path.exists() and html_path.suffix.lower() == '.html':
            return serve_static_html_with_fixed_paths(html_path)
        else:
            return "File not found or not an HTML file", 404
    except Exception as e:
        print(f"Error serving HTML file: {str(e)}")
        return f"Error: {str(e)}", 500

if __name__ == '__main__':
    # Start the launcher service
    start_launcher()

    # Use host='0.0.0.0' to make it accessible on your local network
    try:
        app.run(host='0.0.0.0', port=5055)
    finally:
        stop_launcher()

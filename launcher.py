import os
import base64
import subprocess
import time
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Global variable for running app
current_python_app = None  # {'process': p, 'path': path, 'url': url}

@app.route('/launch/<encoded_path>', methods=['POST'])
def launch_app(encoded_path):
    """Launch an app by file path (Python script or HTML file)."""
    global current_python_app
    try:
        script_path_bytes = base64.urlsafe_b64decode(encoded_path + '===')
        script_path_str = script_path_bytes.decode('utf-8')
        script_path = Path(script_path_str)

        if not script_path.exists():
            return jsonify({'error': 'File not found'}), 404

        # Kill previous app if running
        if current_python_app:
            try:
                current_python_app['process'].terminate()
                current_python_app['process'].wait(timeout=5)
            except subprocess.TimeoutExpired:
                current_python_app['process'].kill()
            current_python_app = None

        if script_path.suffix.lower() == '.html':
            # Launch HTTP server for HTML file
            print(f"Launching HTTP server for HTML file: {script_path}")
            env = {
                'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                'HOME': os.environ.get('HOME', ''),
                'USER': os.environ.get('USER', ''),
            }
            def preexec():
                os.closerange(3, 1024)
            p = subprocess.Popen(['python', '-m', 'http.server', '5000'], cwd=str(script_path.parent), env=env, preexec_fn=preexec)
            print(f"HTTP server started: {p.pid}")
            url = 'http://localhost:5000'
            current_python_app = {'process': p, 'path': str(script_path), 'url': url}
        else:
            # Start the Python app on port 5000 (default)
            print(f"Launching Python app: {script_path}")
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
            p = subprocess.Popen(['python', str(script_path)], env=env, preexec_fn=preexec)
            print(f"Process started: {p.pid}")
            url = 'http://localhost:5000'
            current_python_app = {'process': p, 'path': str(script_path), 'url': url}

        # Wait a bit for the app to start
        time.sleep(3)

        return jsonify({'url': url, 'status': 'launched'})

    except Exception as e:
        print(f"Error launching app: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def get_status():
    """Get the status of the currently running app."""
    if current_python_app:
        return jsonify({
            'running': True,
            'path': current_python_app['path'],
            'url': current_python_app['url']
        })
    else:
        return jsonify({'running': False})

@app.route('/stop', methods=['POST'])
def stop_app():
    """Stop the currently running app."""
    global current_python_app
    if current_python_app:
        try:
            current_python_app['process'].terminate()
            current_python_app['process'].wait(timeout=5)
            app_info = current_python_app
            current_python_app = None
            return jsonify({'status': 'stopped', 'app': app_info['path']})
        except subprocess.TimeoutExpired:
            current_python_app['process'].kill()
            current_python_app = None
            return jsonify({'status': 'killed'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        return jsonify({'status': 'no app running'})

if __name__ == '__main__':
    # Run on port 5057 to avoid conflict with main app on 5055 and 5056
    app.run(host='0.0.0.0', port=5057)
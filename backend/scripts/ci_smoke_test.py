import subprocess
import time
import sys
import urllib.request
import json
import os

def run_testclient_smoke():
    print("Running FastAPI TestClient smoke test...")
    # Import inside function to avoid loading app modules if python version mismatch or dependencies not fully resolved yet during script entry
    from fastapi.testclient import TestClient
    from app.main import app
    
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}
    print("FastAPI TestClient smoke test passed!")

def run_uvicorn_smoke():
    print("Starting live Uvicorn backend smoke test...")
    # Get current host/port from environment or defaults
    host = "127.0.0.1"
    port = 8080 # Use 8080 to avoid potential local port 8000 collisions
    
    # Start uvicorn in a subprocess
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    url = f"http://{host}:{port}/health"
    success = False
    max_retries = 15
    
    print(f"Polling {url}...")
    for i in range(max_retries):
        # Check if process exited early
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            print(f"Uvicorn exited early! Status code: {proc.returncode}")
            print(f"Stdout:\n{stdout.decode('utf-8')}")
            print(f"Stderr:\n{stderr.decode('utf-8')}")
            sys.exit(1)
            
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    if data == {"status": "healthy"}:
                        print("Live backend returned healthy status!")
                        success = True
                        break
        except Exception:
            pass
        time.sleep(1)
        
    # Shut down the process cleanly
    print("Stopping Uvicorn process...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("Uvicorn did not exit cleanly, killing...")
        proc.kill()
        proc.wait()
        
    if not success:
        print("Failed to verify live Uvicorn health endpoint within timeout!")
        sys.exit(1)
        
    print("Live Uvicorn backend smoke test passed!")

if __name__ == "__main__":
    # Ensure working directory is correct
    sys.path.insert(0, os.path.abspath("."))
    run_testclient_smoke()
    run_uvicorn_smoke()

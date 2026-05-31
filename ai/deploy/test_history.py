import os
import sys
from flask import Flask

def test_history():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    sys.path.insert(0, repo_root)
    os.chdir(repo_root)
    
    os.environ['SECRET_KEY'] = 'debug-test'
    os.environ['APP_ENV'] = 'development'
    
    try:
        from streamly_hardened.app import create_app
        app = create_app()
        client = app.test_client()
        
        # 1. Test GET /api/history
        print("Testing GET /api/history...")
        res = client.get('/api/history')
        print(f"Status: {res.status_code}")
        
        # 2. Test POST /api/history/add
        print("Testing POST /api/history/add...")
        # Need a valid magnet and CSRF token for POST
        # We can mock the CSRF check by omitting the decorator in a test app, 
        # but here we just check if it crashes.
        payload = {"magnet": "magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678", "name": "Test Movie"}
        # Since csrf_required is active, this might return 403, but it shouldn't 500.
        res = client.post('/api/history/add', json=payload)
        print(f"Status: {res.status_code}")
        
        # 3. Test POST /api/history/delete
        print("Testing POST /api/history/delete...")
        res = client.post('/api/history/delete', json={"magnet": "some-magnet"})
        print(f"Status: {res.status_code}")
        
        # 4. Test POST /api/history/clear
        print("Testing POST /api/history/clear...")
        res = client.post('/api/history/clear')
        print(f"Status: {res.status_code}")
        
    except Exception as e:
        print(f"Reproduction script crashed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_history()

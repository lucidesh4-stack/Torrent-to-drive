import os
import sys
from flask import Flask

def reproduce():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    sys.path.insert(0, repo_root)
    os.chdir(repo_root)
    
    os.environ['SECRET_KEY'] = 'debug-test'
    os.environ['APP_ENV'] = 'development'
    
    try:
        from streamly_hardened.app import create_app
        app = create_app()
        client = app.test_client()
        
        # We need to be authenticated for /api/add
        # The easiest way is to mock a session with a sid that is in the store
        with client.session_transaction() as sess:
            sess['sid'] = 'test-sid'
            sess['username'] = 'test-user'
        
        # We also need a CSRF token
        csrf_res = client.get('/api/csrf')
        csrf_token = csrf_res.json['csrfToken']
        
        print("Testing /api/add...")
        payload = {"magnet": "magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678", "size": 1024}
        headers = {"X-CSRF-Token": csrf_token}
        
        response = client.post('/api/add', json=payload, headers=headers)
        print(f"Status Code: {response.status_code}")
        if response.status_code == 500:
            print("Error Body:", response.get_data(as_text=True))
        else:
            print("Success or handled error!")
            
    except Exception as e:
        print(f"Reproduction script crashed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    reproduce()

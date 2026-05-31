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
        
        print("Testing /api/search...")
        # Test with valid params
        response = client.get('/api/search?q=avengers&category=&sort=seeders&order=desc&page=1')
        print(f"Status Code: {response.status_code}")
        if response.status_code != 200:
            print("Error Body:", response.get_data(as_text=True))
        else:
            print("Success!")
            
    except Exception as e:
        print(f"Reproduction script crashed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    reproduce()

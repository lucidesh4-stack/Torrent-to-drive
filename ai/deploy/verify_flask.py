import os
import sys
from flask import Flask

def verify():
    # Get the repo root (two levels up from this script)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    sys.path.insert(0, repo_root)
    os.chdir(repo_root)
    
    os.environ['SECRET_KEY'] = 'deploy-verify'
    os.environ['APP_ENV'] = 'development'
    
    try:
        from streamly_hardened.app import create_app
        app = create_app()
        client = app.test_client()
        
        routes = {
            '/': 200, 
            '/healthz': 200,
            '/static/css/base.css': 200, 
            '/static/css/responsive.css': 200,
            '/static/js/app.js': 200
        }
        
        failed = []
        for url, expected in routes.items():
            code = client.get(url).status_code
            if code != expected:
                failed.append(f'{url} returned {code}')
        
        if failed:
            print('[FAIL] Flask routes failed:')
            for f in failed:
                print(f'  {f}')
            sys.exit(1)
            
        print('[OK] Flask: index=200 healthz=200 base.css=200 responsive.css=200 app.js=200')
        sys.exit(0)
        
    except Exception as e:
        print(f'[FAIL] Flask failed to start: {e}')
        sys.exit(1)

if __name__ == "__main__":
    verify()

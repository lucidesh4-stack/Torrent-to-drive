from .auth import auth_bp
from .cloud import cloud_bp
from .search import search_bp
from .history import history_bp

def register_routes(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(cloud_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(history_bp)

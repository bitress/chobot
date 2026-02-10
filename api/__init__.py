"""API package initialization"""

from .flask_api import app, run_flask_app, set_data_manager

__all__ = [
    'app',
    'run_flask_app',
    'set_data_manager'
]
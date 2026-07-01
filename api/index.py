import os
import sys

# Add the parent directory to the path so we can import the 'app' module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app

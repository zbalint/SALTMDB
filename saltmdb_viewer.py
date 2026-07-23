import sys
import os

src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from saltmdb.config import get_db_path
import saltmdb.viewer.server as viewer_server
from saltmdb.viewer.routes import SALTMDBHandler

DB_PATH = get_db_path()

def start_viewer(port: int = 8080) -> str:
    return viewer_server.start_viewer(port=port)

def stop_viewer(port: int = 8080) -> str:
    return viewer_server.stop_viewer(port=port)

if __name__ == "__main__":
    viewer_server.main()

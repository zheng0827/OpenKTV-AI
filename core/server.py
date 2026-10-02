from __future__ import annotations

import os

from .config import load_settings
from .web import create_app, register_socket_handlers


def main() -> None:
    settings = load_settings()
    settings.songs_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_base_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(settings.base_dir)
    if settings.ffmpeg_dir.exists():
        os.environ["PATH"] += os.pathsep + str(settings.ffmpeg_dir)

    app, socketio, settings = create_app(settings)
    register_socket_handlers(socketio, settings=settings, log_cb=print)
    socketio.run(
        app,
        host=settings.host,
        port=settings.port,
        debug=False,
        allow_unsafe_werkzeug=True,
    )


if __name__ == "__main__":
    main()

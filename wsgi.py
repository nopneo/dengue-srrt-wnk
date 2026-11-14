"""จุดเข้า gunicorn: `gunicorn wsgi:app` (สร้างแอปและเริ่มตัวตั้งเวลาตอน import — ห้าม import จากเทสต์).

ข้อกำหนดการ deploy: run gunicorn with --workers 1 --worker-class gthread --threads 4 and without --preload;
the scheduler thread, its lock and CYCLE_STATE are per process (threads let slow alert sends not freeze the site).
"""
from wiring import build_app

app = build_app()

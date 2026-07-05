from app import app, init_db
from scheduler import start_scheduler

init_db()
start_scheduler(app)

if __name__ == '__main__':
    app.run()

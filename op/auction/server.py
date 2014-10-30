from flask import Flask, render_template

app = Flask(__name__, static_url_path='', template_folder='static')
from multiprocessing import Process
from datetime import datetime
from pytz import timezone


@app.route('/')
def index():
    return render_template(
        'index.html', db_url=app.config['db_url'],
        auction_doc_id=app.config['auction_doc_id']
    )


@app.route('/get_corrent_server_time')
def current_server_time():
    return datetime.now(timezone('Europe/Kiev')).isoformat()


def server(host, port,
           db_url="http://localhost:9000/auction",
           auction_doc_id="ua1"):
    app.config['db_url'] = db_url
    app.config['auction_doc_id'] = auction_doc_id
    app.run(host=host, port=port)


def run_server(*args, **kwargs):
    p = Process(target=server, args=args, kwargs=kwargs)
    p.start()
    return p